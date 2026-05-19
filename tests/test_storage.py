"""Storage round-trip tests — Event / Session / Workflow / Observation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from screen_workflow.schemas import (
    CAGELabel,
    Event,
    InputEvent,
    Observation,
    Session,
    SessionCloseReason,
    TriggerType,
    UIElement,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from screen_workflow.storage.db import Store


T0 = datetime(2026, 5, 19, 10, 0, 0)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data")


def _event(idx: int) -> Event:
    return Event(
        event_id=f"01H_{idx:03d}",
        ts=T0 + timedelta(seconds=idx),
        app="OUTLOOK.EXE",
        window_title=f"Inbox - {idx}",
        trigger=InputEvent(type=TriggerType.CLICK, target_label="Approve"),
        screenshot_path=f"2026/05/19/{idx:03d}.png",
        ocr_text=f"text {idx}",
        ui_elements=[UIElement(role="Button", label="Approve", bbox=(0, 0, 10, 10))],
    )


def test_event_round_trip(store: Store) -> None:
    e = _event(1)
    store.insert_event(e)
    [round_tripped] = list(store.iter_events())
    assert round_tripped == e


def test_session_round_trip(store: Store) -> None:
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
    assert round_tripped.event_ids == s.event_ids


def test_workflow_round_trip(store: Store) -> None:
    now = datetime.now(timezone.utc)
    wf = Workflow(
        workflow_id="wf_1",
        name="PO Approval",
        nodes={
            "open-po": WorkflowNode(
                node_id="open-po",
                canonical_name="Open PO email",
                cage_label=CAGELabel.CAPTURE,
                system="Outlook",
                data_object_pattern="PO #<num>",
                estimated_tokens=1200,
                observation_count=3,
            ),
        },
        edges=[WorkflowEdge(from_node="open-po", to_node="open-po", observation_count=1)],
        sessions_processed=["sess-1"],
        stable_observations=2,
        is_complete=False,
        created_at=now,
        last_updated_at=now,
    )
    store.upsert_workflow(wf)
    round_tripped = store.find_workflow_by_name("PO Approval")
    assert round_tripped is not None
    assert round_tripped.workflow_id == "wf_1"
    assert "open-po" in round_tripped.nodes
    assert round_tripped.nodes["open-po"].observation_count == 3


def test_observation_round_trip(store: Store) -> None:
    obs = Observation(
        observation_id="obs_1",
        workflow_id="wf_1",
        session_id="sess_1",
        node_id="open-po",
        evidence_frame_ids=["f1", "f2"],
        estimated_tokens=1234,
        expected_agent_steps=2,
        confidence=0.85,
        observed_at=T0,
    )
    store.insert_observation(obs)
    [round_tripped] = list(store.iter_observations())
    assert round_tripped == obs
    assert round_tripped.estimated_tokens == 1234
