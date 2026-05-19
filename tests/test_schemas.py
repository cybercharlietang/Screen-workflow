"""Schema invariants. Bump SCHEMA_VERSION if you intentionally break these."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from screen_workflow.schemas import (
    SCHEMA_VERSION,
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


T0 = datetime(2026, 5, 19, 10, 0, 0)


def _make_event(**overrides) -> Event:
    defaults = dict(
        event_id="01H_AAA",
        ts=T0,
        app="OUTLOOK.EXE",
        window_title="Inbox - alice@example.com",
        trigger=InputEvent(type=TriggerType.WINDOW_FOCUS),
        screenshot_path="2026/05/19/01H_AAA.png",
    )
    defaults.update(overrides)
    return Event(**defaults)


class TestEvent:
    def test_minimum_fields_construct(self) -> None:
        e = _make_event()
        assert e.schema_version == SCHEMA_VERSION
        assert e.ocr_text == ""
        assert e.ui_elements == []
        assert e.session_id is None

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_event(unexpected="oops")

    def test_trigger_must_be_known_type(self) -> None:
        with pytest.raises(ValidationError):
            Event(
                event_id="x",
                ts=T0,
                app="x",
                window_title="x",
                trigger={"type": "not_a_real_trigger"},
                screenshot_path="x",
            )

    def test_ui_element_bbox_is_four_ints(self) -> None:
        e = _make_event(
            ui_elements=[UIElement(role="Button", label="Approve", bbox=(10, 20, 30, 40))]
        )
        assert e.ui_elements[0].bbox == (10, 20, 30, 40)


class TestSession:
    def test_constructs_with_minimum_fields(self) -> None:
        s = Session(
            session_id="sess-1",
            start_ts=T0,
            end_ts=T0 + timedelta(minutes=5),
            close_reason=SessionCloseReason.IDLE_GAP,
            event_ids=["01H_AAA"],
        )
        assert s.schema_version == SCHEMA_VERSION

    def test_rejects_inverted_timestamps(self) -> None:
        with pytest.raises(ValidationError):
            Session(
                session_id="sess-1",
                start_ts=T0 + timedelta(minutes=5),
                end_ts=T0,
                close_reason=SessionCloseReason.IDLE_GAP,
                event_ids=["01H_AAA"],
            )

    def test_rejects_empty_event_ids(self) -> None:
        with pytest.raises(ValidationError):
            Session(
                session_id="sess-1",
                start_ts=T0,
                end_ts=T0,
                close_reason=SessionCloseReason.IDLE_GAP,
                event_ids=[],
            )


class TestWorkflow:
    def test_empty_workflow_constructs(self) -> None:
        now = datetime.now(timezone.utc)
        w = Workflow(
            workflow_id="wf_1",
            name="PO Approval",
            created_at=now,
            last_updated_at=now,
        )
        assert w.nodes == {}
        assert w.edges == []
        assert w.is_complete is False
        assert w.stability_threshold == 20

    def test_node_validation(self) -> None:
        n = WorkflowNode(
            node_id="open-po-email",
            canonical_name="Open PO email",
            cage_label=CAGELabel.CAPTURE,
            system="Outlook",
            data_object_pattern="PO #<num>",
            estimated_tokens=1200,
        )
        assert n.cage_label is CAGELabel.CAPTURE
        assert n.expected_agent_steps == 1
        assert n.observation_count == 0


class TestObservation:
    def test_requires_evidence(self) -> None:
        with pytest.raises(ValidationError):
            Observation(
                observation_id="o1",
                workflow_id="wf1",
                session_id="s1",
                node_id="n1",
                evidence_frame_ids=[],
                confidence=0.9,
                observed_at=T0,
            )


class TestSchemaVersionStable:
    """If this test fails, you've intentionally introduced a breaking change.
    Update SPEC.md § 4 in the same commit and bump SCHEMA_VERSION."""

    def test_schema_version_is_two(self) -> None:
        assert SCHEMA_VERSION == 2
