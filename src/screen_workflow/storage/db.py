"""Plain SQLite + filesystem storage for captured events.

PoC scope: no encryption (rely on BitLocker). See TODOS.md § "PoC scope"
for the deferred SQLCipher work.

Layout::

    <root>/
      events.db          -- SQLite, one row per Event / Session / Workflow / Observation
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
    Event,
    InputEvent,
    Observation,
    Session,
    SessionCloseReason,
    TriggerType,
    UIElement,
    Workflow,
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

CREATE TABLE IF NOT EXISTS workflows (
    workflow_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    json_blob        TEXT NOT NULL,
    is_complete      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id       TEXT PRIMARY KEY,
    workflow_id          TEXT NOT NULL,
    session_id           TEXT NOT NULL,
    node_id              TEXT NOT NULL,
    evidence_frame_ids   TEXT NOT NULL,
    estimated_tokens     INTEGER NOT NULL DEFAULT 0,
    expected_agent_steps INTEGER NOT NULL DEFAULT 1,
    confidence           REAL NOT NULL,
    observed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_workflow ON observations(workflow_id);
CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);

CREATE TABLE IF NOT EXISTS api_calls (
    call_id        TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    model          TEXT NOT NULL,
    session_id     TEXT,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    usd_cost       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts ON api_calls(ts);
"""


class Store:
    """Thin pydantic-aware wrapper over a single SQLite file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.screens_dir = self.root / "screens"
        self.screens_dir.mkdir(exist_ok=True)
        self.db_path = self.root / "events.db"
        # check_same_thread=False so the daemon worker thread can write while
        # the live-mode renderer reads. Python's sqlite3 serializes ops on a
        # single connection internally. WAL allows readers during writes.
        self._conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
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

    # -- workflows ---------------------------------------------------------

    def upsert_workflow(self, workflow: Workflow) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO workflows VALUES (?,?,?,?,?,?)",
            (
                workflow.workflow_id,
                workflow.name,
                workflow.model_dump_json(),
                int(workflow.is_complete),
                workflow.created_at.isoformat(),
                workflow.last_updated_at.isoformat(),
            ),
        )

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        row = self._conn.execute(
            "SELECT json_blob FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        return Workflow.model_validate_json(row[0])

    def find_workflow_by_name(self, name: str) -> Workflow | None:
        row = self._conn.execute(
            "SELECT json_blob FROM workflows WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return Workflow.model_validate_json(row[0])

    def iter_workflows(self) -> Iterator[Workflow]:
        for row in self._conn.execute("SELECT json_blob FROM workflows ORDER BY name"):
            yield Workflow.model_validate_json(row[0])

    # -- observations ------------------------------------------------------

    def insert_observation(self, obs: Observation) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
            (
                obs.observation_id,
                obs.workflow_id,
                obs.session_id,
                obs.node_id,
                json.dumps(obs.evidence_frame_ids),
                obs.estimated_tokens,
                obs.expected_agent_steps,
                obs.confidence,
                obs.observed_at.isoformat(),
            ),
        )

    def iter_observations(
        self,
        workflow_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Observation]:
        clauses, params = [], []
        if workflow_id is not None:
            clauses.append("workflow_id = ?"); params.append(workflow_id)
        if session_id is not None:
            clauses.append("session_id = ?"); params.append(session_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(
            f"SELECT * FROM observations{where} ORDER BY observed_at", params
        )
        for row in cur:
            yield Observation(
                observation_id=row[0],
                workflow_id=row[1],
                session_id=row[2],
                node_id=row[3],
                evidence_frame_ids=json.loads(row[4]),
                estimated_tokens=row[5],
                expected_agent_steps=row[6],
                confidence=row[7],
                observed_at=datetime.fromisoformat(row[8]),
            )

    # -- api calls (cost tracking) -----------------------------------------

    def insert_api_call(
        self,
        *,
        call_id: str,
        ts: datetime,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd_cost: float,
        session_id: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO api_calls VALUES (?,?,?,?,?,?,?)",
            (
                call_id,
                ts.isoformat(),
                model,
                session_id,
                input_tokens,
                output_tokens,
                usd_cost,
            ),
        )

    def sum_usd_since(self, since_ts: datetime) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(usd_cost), 0) FROM api_calls WHERE ts >= ?",
            (since_ts.isoformat(),),
        ).fetchone()
        return float(row[0])

    def sum_usd_total(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(usd_cost), 0) FROM api_calls"
        ).fetchone()
        return float(row[0])

    def iter_api_calls(self) -> Iterator[tuple]:
        """Yield (call_id, ts, model, session_id, in_toks, out_toks, usd) tuples
        in chronological order. Light shape — viz only needs the columns."""
        for row in self._conn.execute("SELECT * FROM api_calls ORDER BY ts"):
            yield (
                row[0],
                datetime.fromisoformat(row[1]),
                row[2],
                row[3],
                int(row[4]),
                int(row[5]),
                float(row[6]),
            )

    # -- lifecycle ---------------------------------------------------------

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
