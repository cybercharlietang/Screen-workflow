"""Session segmenter behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.schemas import Event, InputEvent, SessionCloseReason, TriggerType
from screen_workflow.session.segmenter import (
    SegmenterConfig,
    segment,
    segment_and_persist,
    segment_and_persist_incremental,
    segment_incremental,
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


# ---------------------------------------------------------------------------
# Streaming (incremental) segmenter
# ---------------------------------------------------------------------------

CFG = SegmenterConfig(duration_cap=timedelta(minutes=30), idle_gap_seconds=120)


def test_incremental_trailing_bucket_left_open() -> None:
    """A recent trailing bucket is NOT closed — it could still grow."""
    events = [_e(0, 0), _e(1, 30), _e(2, 60)]
    now = T0 + timedelta(seconds=90)  # only 30s since last event < 120s idle
    sessions = segment_incremental(events, CFG, now)
    assert sessions == []


def test_incremental_trailing_bucket_closes_on_wall_clock_idle() -> None:
    """A trailing bucket idle longer than idle_gap closes even with no later
    event to confirm the gap."""
    events = [_e(0, 0), _e(1, 30), _e(2, 60)]
    now = T0 + timedelta(seconds=60 + 200)  # 200s since last event > 120s
    sessions = segment_incremental(events, CFG, now)
    assert len(sessions) == 1
    assert sessions[0].event_ids == ["e_000", "e_001", "e_002"]
    assert sessions[0].close_reason is SessionCloseReason.IDLE_GAP


def test_incremental_confirmed_gap_closes_but_tail_stays_open() -> None:
    """A mid-stream idle gap closes the first session; the recent tail after
    the gap is left open."""
    events = [_e(0, 0), _e(1, 30), _e(2, 300), _e(3, 320)]  # gap 30->300 = 270s
    now = T0 + timedelta(seconds=320 + 10)  # tail is recent
    sessions = segment_incremental(events, CFG, now)
    assert len(sessions) == 1
    assert sessions[0].event_ids == ["e_000", "e_001"]
    assert sessions[0].close_reason is SessionCloseReason.IDLE_GAP


def test_incremental_duration_cap_closes_recent_bucket() -> None:
    """Duration cap fires regardless of how recent the trailing event is."""
    events = [_e(i, i * 60.0) for i in range(40)]  # 39 min span, 60s apart
    now = events[-1].ts + timedelta(seconds=5)  # trailing event very recent
    sessions = segment_incremental(events, CFG, now)
    assert len(sessions) >= 1
    assert sessions[0].close_reason is SessionCloseReason.DURATION_CAP


def test_incremental_max_events_closes_recent_bucket() -> None:
    events = [_e(i, i * 1.0) for i in range(7)]  # 1s apart, no idle gap
    now = events[-1].ts + timedelta(seconds=1)  # trailing event very recent
    sessions = segment_incremental(events, SegmenterConfig(max_events=3), now)
    # caps at 3 -> two closed sessions of 3; the final 1 stays open
    assert [len(s.event_ids) for s in sessions] == [3, 3]


def test_incremental_force_flushes_open_trailing_bucket() -> None:
    events = [_e(0, 0), _e(1, 30), _e(2, 60)]
    now = T0 + timedelta(seconds=90)  # recent — would normally stay open
    sessions = segment_incremental(events, CFG, now, force=True)
    assert len(sessions) == 1
    assert sessions[0].event_ids == ["e_000", "e_001", "e_002"]


def test_incremental_empty_input() -> None:
    assert segment_incremental([], CFG, T0) == []


def test_incremental_persist_is_idempotent(tmp_path: Path) -> None:
    """Once a session is persisted its events are assigned, so a second tick
    finds nothing to do."""
    store = Store(tmp_path / "data")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(5):
        ev = Event(
            event_id=f"e_{i:03d}",
            ts=base + timedelta(seconds=i * 10),  # 10s apart, cluster is ~1h old
            app="OUTLOOK.EXE",
            window_title=f"Inbox {i}",
            trigger=InputEvent(type=TriggerType.CLICK),
            screenshot_path=f"x{i}.png",
        )
        store.insert_event(ev)

    first = segment_and_persist_incremental(store, CFG)
    assert len(first) == 1  # whole cluster is idle -> one closed session
    second = segment_and_persist_incremental(store, CFG)
    assert second == []  # nothing left unassigned
