"""Cross-platform capture daemon (PoC).

Event sources:
- ``pynput`` for mouse + keyboard
- ``mss`` for screenshots (primary monitor only)
- Heartbeat thread for periodic captures during idle reading
- Foreground-window polling (1 Hz) for app/title changes

Local OCR is *deferred* per the PoC plan — Claude reads the screenshots
directly. ``Event.ocr_text`` is left empty.

Active-window detection is best-effort; if no platform helper is available
the daemon falls back to ``app="unknown"`` and a blank window title rather
than crashing.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from screen_workflow.capture.filter import RawEvent, classify
from screen_workflow.schemas import Event, InputEvent, TriggerType
from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)


HEARTBEAT_SECONDS = 30.0
ACTIVE_WINDOW_POLL_SECONDS = 1.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_event_id() -> str:
    # ULID would be nicer; uuid4 with a time prefix is good enough for PoC.
    return f"{int(time.time() * 1000):013d}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Active-window probe (best-effort, cross-platform)
# ---------------------------------------------------------------------------


def _make_active_window_probe() -> Callable[[], tuple[str, str]]:
    """Return a function returning (app, window_title) for the active window."""
    try:
        import pywinctl  # type: ignore

        def probe() -> tuple[str, str]:
            try:
                w = pywinctl.getActiveWindow()
                if w is None:
                    return "unknown", ""
                title = getattr(w, "title", "") or ""
                app = getattr(w, "getAppName", lambda: "unknown")()
                return app or "unknown", title
            except Exception:
                return "unknown", ""

        return probe
    except Exception:
        log.warning("pywinctl unavailable; active window will report 'unknown'")
        return lambda: ("unknown", "")


# ---------------------------------------------------------------------------
# Screenshot grabber
# ---------------------------------------------------------------------------


class Screenshotter:
    def __init__(self, screens_root: Path) -> None:
        self.root = screens_root
        try:
            import mss  # noqa: F401

            self._impl = "mss"
        except Exception:
            from PIL import ImageGrab  # noqa: F401

            self._impl = "pil"

    def grab(self, event_id: str) -> str:
        """Save a PNG of the primary monitor, return relative path."""
        d = datetime.now(timezone.utc)
        rel = Path(f"{d.year:04d}/{d.month:02d}/{d.day:02d}/{event_id}.png")
        full = self.root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        if self._impl == "mss":
            import mss
            import mss.tools

            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])  # primary monitor
                mss.tools.to_png(shot.rgb, shot.size, output=str(full))
        else:
            from PIL import ImageGrab

            ImageGrab.grab().save(full)
        return str(rel)


# ---------------------------------------------------------------------------
# Event listeners (pynput)
# ---------------------------------------------------------------------------


def _start_pynput_listeners(out_q: queue.Queue[RawEvent]) -> tuple:
    """Start mouse + keyboard listeners. Returns (mouse_listener, kb_listener)."""
    from pynput import keyboard, mouse

    pressed_mods: set[str] = set()
    MOD_KEYS = {
        keyboard.Key.ctrl,
        keyboard.Key.ctrl_l,
        keyboard.Key.ctrl_r,
        keyboard.Key.shift,
        keyboard.Key.shift_l,
        keyboard.Key.shift_r,
        keyboard.Key.alt,
        keyboard.Key.alt_l,
        keyboard.Key.alt_r,
        keyboard.Key.cmd,
        keyboard.Key.cmd_l,
        keyboard.Key.cmd_r,
    }

    def _mod_name(k) -> str | None:
        s = str(k).lower()
        if "ctrl" in s:
            return "ctrl"
        if "shift" in s:
            return "shift"
        if "alt" in s:
            return "alt"
        if "cmd" in s or "meta" in s:
            return "cmd"
        return None

    def on_press(key) -> None:
        if key in MOD_KEYS:
            name = _mod_name(key)
            if name:
                pressed_mods.add(name)
            return
        # Translate to a string we can match in the filter
        try:
            k = key.char  # type: ignore[attr-defined]
        except AttributeError:
            k = str(key).replace("Key.", "")
        out_q.put(
            RawEvent(
                kind="key",
                key=k,
                is_pressed=True,
                modifiers=frozenset(pressed_mods),
            )
        )

    def on_release(key) -> None:
        if key in MOD_KEYS:
            name = _mod_name(key)
            if name:
                pressed_mods.discard(name)

    def on_click(x, y, button, pressed) -> None:
        out_q.put(
            RawEvent(
                kind="mouse_click",
                button=str(button).split(".")[-1].lower(),
                is_pressed=pressed,
            )
        )

    ml = mouse.Listener(on_click=on_click)
    kl = keyboard.Listener(on_press=on_press, on_release=on_release)
    ml.start()
    kl.start()
    return ml, kl


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Daemon:
    def __init__(self, root: Path) -> None:
        self.store = Store(root)
        self.shotter = Screenshotter(self.store.screens_dir)
        self.probe = _make_active_window_probe()
        self.raw_q: queue.Queue[RawEvent] = queue.Queue()
        self._stop = threading.Event()
        self._last_app_title: tuple[str, str] | None = None

    # -- threads -----------------------------------------------------------

    def _heartbeat_thread(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.raw_q.put(RawEvent(kind="heartbeat"))

    def _window_probe_thread(self) -> None:
        while not self._stop.wait(ACTIVE_WINDOW_POLL_SECONDS):
            app, title = self.probe()
            cur = (app, title)
            if self._last_app_title is not None and cur != self._last_app_title:
                self.raw_q.put(
                    RawEvent(
                        kind="window_focus",
                        target_label=f"{app}: {title}",
                    )
                )
            self._last_app_title = cur

    # -- main loop ---------------------------------------------------------

    def run(self, seconds: float | None = None) -> None:
        log.info("daemon starting, root=%s", self.store.root)
        ml, kl = _start_pynput_listeners(self.raw_q)
        heartbeat = threading.Thread(target=self._heartbeat_thread, daemon=True)
        winprobe = threading.Thread(target=self._window_probe_thread, daemon=True)
        heartbeat.start()
        winprobe.start()
        start = time.monotonic()
        try:
            while not self._stop.is_set():
                if seconds is not None and (time.monotonic() - start) >= seconds:
                    break
                try:
                    raw = self.raw_q.get(timeout=0.25)
                except queue.Empty:
                    continue
                result = classify(raw)
                if not result.keep or result.trigger is None:
                    continue
                self._capture(result.trigger, result.target_label)
        finally:
            self._stop.set()
            ml.stop()
            kl.stop()
            self.store.close()
            log.info("daemon stopped")

    # -- capture -----------------------------------------------------------

    def _capture(self, trigger: TriggerType, target_label: str | None) -> None:
        event_id = _new_event_id()
        app, window_title = self.probe()
        screenshot_path = self.shotter.grab(event_id)
        event = Event(
            event_id=event_id,
            ts=_now(),
            app=app,
            window_title=window_title,
            trigger=InputEvent(type=trigger, target_label=target_label),
            screenshot_path=screenshot_path,
            ocr_text="",
        )
        self.store.insert_event(event)
        log.info("captured %s [%s] %s — %s", event_id, trigger.value, app, window_title)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen-workflowd")
    p.add_argument("--root", default="./local_data", help="storage root dir")
    p.add_argument("--seconds", type=float, default=None, help="run for N seconds then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    Daemon(Path(args.root)).run(seconds=args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
