"""Storage round-trip tests — Event / Session / Label."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

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
from screen_workflow.storage.db import Store


T0 = datetime(2026, 5, 19, 10, 0, 0)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data")


def _event(idx: int, **kw) -> Event:
    return Event(
        event_id=f"01H_{idx:03d}",
        ts=T0 + timedelta(seconds=idx),
        app="OUTLOOK.EXE",
        window_title=f"Inbox - {idx}",
        trigger=InputEvent(type=TriggerType.CLICK, target_label="Approve"),
        screenshot_path=f"2026/05/19/{idx:03d}.png",
        ocr_text=f"text {idx}",
        ui_elements=[UIElement(role="Button", label="Approve", bbox=(0, 0, 10, 10))],
        **kw,
    )


def test_event_round_trip(store: Store) -> None:
    e = _event(1)
    store.insert_event(e)
    [round_tripped] = list(store.iter_events())
    assert round_tripped == e


def test_iter_events_ordered_by_ts(store: Store) -> None:
    store.insert_event(_event(3))
    store.insert_event(_event(1))
    store.insert_event(_event(2))
    ids = [e.event_id for e in store.iter_events()]
    assert ids == ["01H_001", "01H_002", "01H_003"]


def test_session_round_trip_assigns_session_id(store: Store) -> None:
    for i in range(3):
        store.insert_event(_event(i))
    s = Session(
        session_id="sess-1",
        start_ts=T0,
        end_ts=T0 + timedelta(seconds=2),
        close_reason=SessionCloseReason.IDLE_GAP,
        event_ids=["01H_000", "01H_001", "01H_002"],
    )
    store.insert_session(s)
    [round_tripped] = list(store.iter_sessions())
    assert round_tripped.session_id == "sess-1"
    assert round_tripped.event_ids == s.event_ids
    # events now carry the session_id
    for e in store.iter_events("sess-1"):
        assert e.session_id == "sess-1"


def test_label_round_trip(store: Store) -> None:
    label = Label(
        action_id="act-1",
        session_id="sess-1",
        cage_label=CAGELabel.ANALYZE,
        system="SAP",
        data_object="PO #12345",
        estimated_tokens=4500,
        start_ts=T0,
        end_ts=T0 + timedelta(seconds=30),
        evidence_frame_ids=["01H_001", "01H_002"],
        confidence=0.83,
        rationale="reconciled invoice lines against PO",
    )
    store.insert_label(label)
    [round_tripped] = list(store.iter_labels())
    assert round_tripped == label
