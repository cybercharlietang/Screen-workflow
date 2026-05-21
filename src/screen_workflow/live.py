"""Live mode: daemon + auto-rerender + HTTP server, one command.

Usage::

    python -m screen_workflow.live --root ./local_data --port 8765

Runs the capture daemon in a thread, periodically re-renders the static
HTML report into ``viz_output/``, and serves that directory on localhost.
The user opens ``http://localhost:8765/`` and watches events appear as
they work — the browser tab refreshes itself every few seconds.
"""

from __future__ import annotations

import argparse
import http.server
import logging
import os
import socketserver
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from screen_workflow.capture.daemon import Daemon
from screen_workflow.cost_monitor import CostMonitor
from screen_workflow.labeler.api import process_session
from screen_workflow.session.segmenter import segment_and_persist_incremental
from screen_workflow.storage.db import Store
from screen_workflow.viz.report import render

log = logging.getLogger(__name__)

RERENDER_INTERVAL_SECONDS = 30.0
SEGMENTER_INTERVAL_SECONDS = 30.0
LABELER_INTERVAL_SECONDS = 60.0
DEFAULT_LABELER_MODEL = "claude-sonnet-4-6"


def _rerender_loop(root: Path, out_dir: Path, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            store = Store(root)
            render(store, out_dir)
            store.close()
        except Exception:  # noqa: BLE001 — keep the loop alive on transient errors
            log.exception("rerender failed")
        stop.wait(RERENDER_INTERVAL_SECONDS)


def _segmenter_loop(root: Path, stop: threading.Event) -> None:
    """Streaming segmenter: every tick, close any session that has definitely
    ended. On shutdown, force-flush the open trailing session so it is not
    lost."""
    store = Store(root)
    try:
        while not stop.wait(SEGMENTER_INTERVAL_SECONDS):
            try:
                segment_and_persist_incremental(store)
            except Exception:  # noqa: BLE001 — keep the loop alive
                log.exception("segmenter tick failed")
    finally:
        try:
            flushed = segment_and_persist_incremental(store, force=True)
            if flushed:
                log.info("segmenter: flushed %d open session(s) on shutdown", len(flushed))
        except Exception:  # noqa: BLE001
            log.exception("segmenter final flush failed")
        store.close()


def _unprocessed_sessions(store: Store) -> list:
    """Sessions that have not yet been through the labeler."""
    labeled = store.labeled_session_ids()
    return [s for s in store.iter_sessions() if s.session_id not in labeled]


def _labeler_loop(
    root: Path,
    *,
    model: str,
    soft: float,
    hard: float,
    cap: float,
    run_started_at: datetime,
    stop: threading.Event,
) -> None:
    """Streaming labeler: every tick, label any closed-and-unlabeled session.

    Single-flight by construction — one thread, one session at a time. Before
    each session the cost monitor is consulted; on HARD_STOP the labeler stops
    issuing calls (capture keeps running) until hourly spend ages out.
    """
    store = Store(root)
    monitor = CostMonitor(
        store,
        soft_alert_usd_per_hour=soft,
        hard_stop_usd_per_hour=hard,
        total_spend_cap_usd=cap,
        run_started_at=run_started_at,
    )
    while not stop.wait(LABELER_INTERVAL_SECONDS):
        try:
            for session in _unprocessed_sessions(store):
                if stop.is_set():
                    break
                if monitor.should_pause():
                    snap = monitor.snapshot()
                    log.warning(
                        "labeler paused (capture continues): %s", snap.reason
                    )
                    break
                log.info("labeler: processing session %s", session.session_id)
                process_session(
                    store, session, model=model, cost_monitor=monitor
                )
                store.mark_session_labeled(session.session_id)
        except Exception:  # noqa: BLE001 — keep the loop alive on API/transient errors
            log.exception("labeler tick failed")
    store.close()


def _serve(out_dir: Path, screens_dir: Path, port: int, stop: threading.Event) -> None:
    """Local HTTP server.

    Routes:
      /              -> out_dir (index.html, data.json)
      /screens/...   -> screens_dir (full-resolution PNGs, lazy-loaded by
                        the page's event detail view)
    """

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kw):
            super().__init__(*args, directory=str(out_dir), **kw)

        def translate_path(self, path: str) -> str:
            # Route /screens/... to the captures directory.
            if path.startswith("/screens/"):
                rel = path[len("/screens/") :].split("?", 1)[0].split("#", 1)[0]
                # Strip any leading slashes/backslashes for safety
                rel = rel.lstrip("/\\")
                return str(screens_dir / rel)
            return super().translate_path(path)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, max-age=0")
            super().end_headers()

        def log_message(self, fmt, *args):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        httpd.timeout = 0.5
        while not stop.is_set():
            httpd.handle_request()


def _write_run_metadata(root: Path, args: argparse.Namespace) -> None:
    """Persist the run's config to local_data/run_metadata.json.

    Future-you looking at a captured run wants to know which knobs produced
    it without grepping shell history. Cheap, big debuggability win.
    """
    import json
    import subprocess
    from datetime import datetime, timezone

    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip() or None
    except Exception:  # noqa: BLE001
        sha = None

    payload = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": sha,
        "args": {k: v for k, v in vars(args).items()},
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "run_metadata.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
    except OSError:
        log.exception("failed to write run_metadata.json; continuing")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="screen_workflow.live")
    p.add_argument("--root", default="./local_data", help="capture storage dir")
    p.add_argument("--out", default="./viz_output", help="HTML output dir")
    p.add_argument("--port", type=int, default=8765, help="HTTP server port")
    p.add_argument("--seconds", type=float, default=None, help="auto-stop after N seconds")
    p.add_argument("--no-browser", action="store_true", help="don't auto-open browser")
    p.add_argument(
        "--no-daemon",
        action="store_true",
        help="viz-only mode: run renderer + HTTP server without capturing",
    )
    # Cost guards. The labeler thread (PR 2) reads these via CostMonitor.
    p.add_argument(
        "--soft-alert-usd-per-hour",
        type=float,
        default=10.0,
        help="warn when rolling hourly spend hits this",
    )
    p.add_argument(
        "--hard-stop-usd-per-hour",
        type=float,
        default=30.0,
        help="pause labeling when rolling hourly spend hits this; capture continues",
    )
    p.add_argument(
        "--total-spend-cap-usd",
        type=float,
        default=100.0,
        help="pause labeling when cumulative run spend hits this",
    )
    # Labeler controls.
    p.add_argument(
        "--labeler-model",
        default=DEFAULT_LABELER_MODEL,
        help="Anthropic model id for the labeler (sonnet for stress runs, opus for delivery)",
    )
    p.add_argument(
        "--no-labeler",
        action="store_true",
        help="capture + segment only; do not call Claude (free dry runs)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Silence chatty third-party loggers even at DEBUG
    for noisy in ("PIL", "PIL.PngImagePlugin", "PIL.Image", "PIL.TiffImagePlugin"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root = Path(args.root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    _write_run_metadata(root, args)

    # Make sure there's at least an empty report so the browser doesn't 404.
    store = Store(root)
    render(store, out_dir)
    store.close()

    stop = threading.Event()
    rerender_thread = threading.Thread(
        target=_rerender_loop, args=(root, out_dir, stop), daemon=True
    )
    screens_dir = root / "screens"
    server_thread = threading.Thread(
        target=_serve, args=(out_dir, screens_dir, args.port, stop), daemon=True
    )
    rerender_thread.start()
    server_thread.start()

    run_started_at = datetime.now(timezone.utc)

    # Decide whether the labeler thread runs. The segmenter is free; the
    # labeler costs money and needs a key, so it has more ways to be off.
    labeler_enabled = not args.no_daemon and not args.no_labeler
    labeler_status = args.labeler_model
    if args.no_daemon:
        labeler_status = "DISABLED (view-only mode)"
    elif args.no_labeler:
        labeler_status = "DISABLED (--no-labeler)"
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        labeler_enabled = False
        labeler_status = "DISABLED (ANTHROPIC_API_KEY not set)"
        log.warning(
            "ANTHROPIC_API_KEY is not set — labeler disabled; capture + "
            "segmentation will still run. Set the key and restart to label."
        )

    daemon = None
    daemon_thread = None
    pipeline_threads: list[threading.Thread] = []
    if not args.no_daemon:
        daemon = Daemon(root)
        daemon_thread = threading.Thread(
            target=daemon.run, kwargs={"seconds": args.seconds}, daemon=True
        )
        daemon_thread.start()

        seg_thread = threading.Thread(
            target=_segmenter_loop, args=(root, stop), daemon=True
        )
        seg_thread.start()
        pipeline_threads.append(seg_thread)

        if labeler_enabled:
            lab_thread = threading.Thread(
                target=_labeler_loop,
                args=(root,),
                kwargs={
                    "model": args.labeler_model,
                    "soft": args.soft_alert_usd_per_hour,
                    "hard": args.hard_stop_usd_per_hour,
                    "cap": args.total_spend_cap_usd,
                    "run_started_at": run_started_at,
                    "stop": stop,
                },
                daemon=True,
            )
            lab_thread.start()
            pipeline_threads.append(lab_thread)

    url = f"http://localhost:{args.port}/"
    mode = "view-only" if args.no_daemon else "live"
    print()
    print("=" * 60)
    print(f"  Screen-workflow {mode}  ->  {url}")
    print(f"  Reading from:            {root.resolve()}")
    if args.no_daemon:
        print(f"  Capture daemon:          DISABLED (--no-daemon)")
    else:
        print(f"  Capturing to:            {root.resolve()}")
        print(f"  Segmenter:               every {SEGMENTER_INTERVAL_SECONDS:.0f}s")
    print(f"  Re-rendering every:      {RERENDER_INTERVAL_SECONDS:.0f}s")
    print(f"  Labeler:                 {labeler_status}")
    if labeler_enabled:
        print(f"  Cost guards:             soft ${args.soft_alert_usd_per_hour:g}/hr | "
              f"hard ${args.hard_stop_usd_per_hour:g}/hr | cap ${args.total_spend_cap_usd:g}")
    print("  Press Ctrl+C to stop.")
    print("=" * 60)
    print()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        if daemon_thread is not None:
            while daemon_thread.is_alive():
                daemon_thread.join(timeout=0.5)
        else:
            # view-only: wait until Ctrl+C
            while not stop.is_set():
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping...")
        if daemon is not None:
            daemon._stop.set()  # noqa: SLF001
    finally:
        stop.set()
        # Give the pipeline threads a moment to wind down — the segmenter
        # flushes its open trailing session in its shutdown path.
        for t in pipeline_threads:
            t.join(timeout=5.0)
        time.sleep(0.5)
        pending = _shutdown_pending_count(root)
        if pending:
            print(
                f"  {pending} session(s) captured but not labeled. "
                f"Run `screen-workflow-label --root {args.root}` to process them."
            )
    return 0


def _shutdown_pending_count(root: Path) -> int:
    """Count closed-but-unlabeled sessions, for the shutdown hint."""
    try:
        store = Store(root)
        try:
            return len(_unprocessed_sessions(store))
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return 0


if __name__ == "__main__":
    sys.exit(main())
