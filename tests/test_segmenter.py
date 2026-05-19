"""Session segmenter behavior."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from screen_workflow.schemas import Event, InputEvent, SessionCloseReason, TriggerType
from screen_workflow.session.segmenter import (
    SegmenterConfig,
    segment,
    segment_and_persist,
)
from screen_workflow.storage.db import Store


T0 = datetime(2026, 5, 19, 13, 0, 0)


def _e(i: int, secs_from_start: float) -> Event:
    return Event(
        event_id=f"e_{i:03d}",
        ts=T0 + timedelta(seconds=secs_from_start),
        app="OUTLOOK.EXE",
        window_title=f"Inbox {i}",
        trigger=InputEvent(type=TriggerType.CLICK),
        screenshot_path=f"x{i}.png",
    )


def test_empty_input_yields_no_sessions() -> None:
    assert segment([]) == []


def test_single_event_closes_as_end_of_stream() -> None:
    sessions = segment([_e(0, 0)])
    assert len(sessions) == 1
    assert sessions[0].event_ids == ["e_000"]


def test_idle_gap_splits_sessions() -> None:
    events = [_e(0, 0), _e(1, 30), _e(2, 200)]  # 200-30 = 170s > 120
    sessions = segment(events, SegmenterConfig(idle_gap_seconds=120))
    assert [s.event_ids for s in sessions] == [["e_000", "e_001"], ["e_002"]]
    assert sessions[0].close_reason is SessionCloseReason.IDLE_GAP


def test_duration_cap_closes_session() -> None:
    # Events 60s apart for >30 min total — no idle gap fires, only duration cap.
    events = [_e(i, i * 60.0) for i in range(40)]
    sessions = segment(
        events,
        SegmenterConfig(duration_cap=timedelta(minutes=30), idle_gap_seconds=120),
    )
    assert sessions[0].close_reason is SessionCloseReason.DURATION_CAP
    assert len(sessions) >= 2


def test_max_events_cap() -> None:
    events = [_e(i, i * 1.0) for i in range(7)]  # all 1s apart, no idle gap
    sessions = segment(events, SegmenterConfig(max_events=3))
    # 7 events, cap 3 -> sessions of [3, 3, 1]
    assert [len(s.event_ids) for s in sessions] == [3, 3, 1]


def test_segment_and_persist_assigns_session_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    for i in range(3):
        store.insert_event(_e(i, i * 30.0))
    sessions = segment_and_persist(store, SegmenterConfig(idle_gap_seconds=300))
    assert len(sessions) == 1
    # events now carry the session_id
    for e in store.iter_events(sessions[0].session_id):
        assert e.session_id == sessions[0].session_id
