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
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path

from screen_workflow.capture.daemon import Daemon
from screen_workflow.storage.db import Store
from screen_workflow.viz.report import render

log = logging.getLogger(__name__)

RERENDER_INTERVAL_SECONDS = 30.0
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

    daemon = None
    daemon_thread = None
    if not args.no_daemon:
        daemon = Daemon(root)
        daemon_thread = threading.Thread(
            target=daemon.run, kwargs={"seconds": args.seconds}, daemon=True
        )
        daemon_thread.start()

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
    print(f"  Re-rendering every:      {RERENDER_INTERVAL_SECONDS}s")
    print(f"  Labeler:                 {'DISABLED (--no-labeler)' if args.no_labeler else args.labeler_model}")
    print(f"  Cost guards:             soft ${args.soft_alert_usd_per_hour}/hr | hard ${args.hard_stop_usd_per_hour}/hr | cap ${args.total_spend_cap_usd}")
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
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
