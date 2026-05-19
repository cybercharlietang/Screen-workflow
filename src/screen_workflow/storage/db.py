"""Plain SQLite + filesystem storage for captured events.

PoC scope: no encryption (rely on BitLocker). See TODOS.md § "PoC scope"
for the deferred SQLCipher work.

Layout::

    <root>/
      events.db          -- SQLite, one row per Event
      sessions.db        -- (same file in practice; separate table)
      labels.db          -- (same file; separate table)
      screens/
        YYYY/MM/DD/<event_id>.png
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from screen_workflow.schemas import (
    CAGELabel,
    Event,
    InputEvent,
    Label,
    Session,
    SessionCloseReason,
    TriggerType,
    UIElement,
)

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    session_id      TEXT,
    app             TEXT NOT NULL,
    window_title    TEXT NOT NULL,
    url             TEXT,
    trigger_type    TEXT NOT NULL,
    trigger_target  TEXT,
    screenshot_path TEXT NOT NULL,
    ocr_text        TEXT NOT NULL DEFAULT '',
    ui_elements     TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    start_ts     TEXT NOT NULL,
    end_ts       TEXT NOT NULL,
    close_reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    action_id          TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    cage_label         TEXT NOT NULL,
    system             TEXT NOT NULL,
    data_object        TEXT NOT NULL,
    estimated_tokens   INTEGER NOT NULL,
    start_ts           TEXT NOT NULL,
    end_ts             TEXT NOT NULL,
    evidence_frame_ids TEXT NOT NULL,
    confidence         REAL NOT NULL,
    rationale          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_session ON labels(session_id);
"""


class Store:
    """Thin pydantic-aware wrapper over a single SQLite file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.screens_dir = self.root / "screens"
        self.screens_dir.mkdir(exist_ok=True)
        self.db_path = self.root / "events.db"
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.executescript(_DDL)

    # -- events ------------------------------------------------------------

    def insert_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.ts.isoformat(),
                event.session_id,
                event.app,
                event.window_title,
                event.url,
                event.trigger.type.value,
                event.trigger.target_label,
                event.screenshot_path,
                event.ocr_text,
                json.dumps([e.model_dump() for e in event.ui_elements]),
            ),
        )

    def iter_events(self, session_id: str | None = None) -> Iterator[Event]:
        if session_id is None:
            cur = self._conn.execute("SELECT * FROM events ORDER BY ts")
        else:
            cur = self._conn.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY ts",
                (session_id,),
            )
        for row in cur:
            yield _row_to_event(row)

    def assign_session(self, event_ids: list[str], session_id: str) -> None:
        self._conn.executemany(
            "UPDATE events SET session_id = ? WHERE event_id = ?",
            [(session_id, eid) for eid in event_ids],
        )

    # -- sessions ----------------------------------------------------------

    def insert_session(self, session: Session) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
            (
                session.session_id,
                session.start_ts.isoformat(),
                session.end_ts.isoformat(),
                session.close_reason.value,
            ),
        )
        self.assign_session(session.event_ids, session.session_id)

    def iter_sessions(self) -> Iterator[Session]:
        for row in self._conn.execute("SELECT * FROM sessions ORDER BY start_ts"):
            sid = row[0]
            event_ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT event_id FROM events WHERE session_id = ? ORDER BY ts",
                    (sid,),
                )
            ]
            yield Session(
                session_id=sid,
                start_ts=datetime.fromisoformat(row[1]),
                end_ts=datetime.fromisoformat(row[2]),
                close_reason=SessionCloseReason(row[3]),
                event_ids=event_ids,
            )

    # -- labels ------------------------------------------------------------

    def insert_label(self, label: Label) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO labels VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                label.action_id,
                label.session_id,
                label.cage_label.value,
                label.system,
                label.data_object,
                label.estimated_tokens,
                label.start_ts.isoformat(),
                label.end_ts.isoformat(),
                json.dumps(label.evidence_frame_ids),
                label.confidence,
                label.rationale,
            ),
        )

    def iter_labels(self, session_id: str | None = None) -> Iterator[Label]:
        if session_id is None:
            cur = self._conn.execute("SELECT * FROM labels ORDER BY start_ts")
        else:
            cur = self._conn.execute(
                "SELECT * FROM labels WHERE session_id = ? ORDER BY start_ts",
                (session_id,),
            )
        for row in cur:
            yield Label(
                action_id=row[0],
                session_id=row[1],
                cage_label=CAGELabel(row[2]),
                system=row[3],
                data_object=row[4],
                estimated_tokens=row[5],
                start_ts=datetime.fromisoformat(row[6]),
                end_ts=datetime.fromisoformat(row[7]),
                evidence_frame_ids=json.loads(row[8]),
                confidence=row[9],
                rationale=row[10],
            )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def cursor(self):
        yield self._conn


def _row_to_event(row: tuple) -> Event:
    (
        event_id,
        ts,
        session_id,
        app,
        window_title,
        url,
        trigger_type,
        trigger_target,
        screenshot_path,
        ocr_text,
        ui_elements_json,
    ) = row
    return Event(
        event_id=event_id,
        ts=datetime.fromisoformat(ts),
        session_id=session_id,
        app=app,
        window_title=window_title,
        url=url,
        trigger=InputEvent(
            type=TriggerType(trigger_type),
            target_label=trigger_target,
        ),
        screenshot_path=screenshot_path,
        ocr_text=ocr_text or "",
        ui_elements=[UIElement(**e) for e in json.loads(ui_elements_json)],
    )
