"""Regression: cross-thread Store usage must not raise sqlite3.ProgrammingError.

Reproduces the live-mode setup: Store created on main thread, writes happen
on a worker thread (the daemon).
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from screen_workflow.schemas import Event, InputEvent, TriggerType
from screen_workflow.storage.db import Store


def test_insert_event_from_other_thread(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    err: list[BaseException] = []

    def worker() -> None:
        try:
            store.insert_event(
                Event(
                    event_id="x",
                    ts=datetime(2026, 5, 19, 13, 0, 0),
                    app="OUTLOOK.EXE",
                    window_title="Inbox",
                    trigger=InputEvent(type=TriggerType.CLICK),
                    screenshot_path="x.png",
                )
            )
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert err == [], f"insert_event from worker thread raised {err[0]!r}"
    [e] = list(store.iter_events())
    assert e.event_id == "x"
