"""Schema invariants. Bump SCHEMA_VERSION if you intentionally break these."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from screen_workflow.schemas import (
    SCHEMA_VERSION,
    ActionUnit,
    CAGELabel,
    Event,
    InputEvent,
    Label,
    Session,
    SessionCloseReason,
    TriggerType,
    UIElement,
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


class TestLabel:
    def _make_label(self, **overrides) -> Label:
        defaults = dict(
            action_id="act-1",
            session_id="sess-1",
            cage_label=CAGELabel.ANALYZE,
            system="SAP",
            data_object="PO #12345",
            estimated_tokens=4500,
            start_ts=T0,
            end_ts=T0 + timedelta(seconds=45),
            evidence_frame_ids=["01H_AAA"],
            confidence=0.82,
            rationale="User reconciled line items against the PO.",
        )
        defaults.update(overrides)
        return Label(**defaults)

    def test_constructs(self) -> None:
        assert self._make_label().cage_label is CAGELabel.ANALYZE

    @pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
    def test_confidence_bounded(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            self._make_label(confidence=bad)

    def test_rejects_inverted_timestamps(self) -> None:
        with pytest.raises(ValidationError):
            self._make_label(start_ts=T0 + timedelta(seconds=10), end_ts=T0)

    def test_rejects_empty_evidence(self) -> None:
        with pytest.raises(ValidationError):
            self._make_label(evidence_frame_ids=[])


class TestActionUnit:
    def test_constructs(self) -> None:
        u = ActionUnit(
            action_id="act-1",
            start_frame_id="01H_AAA",
            end_frame_id="01H_BBB",
            description="Open PO #12345 in SAP",
        )
        assert u.target_data_hint is None


class TestSchemaVersionStable:
    """If this test fails, you've intentionally introduced a breaking change.
    Update SPEC.md § 4 in the same commit and bump SCHEMA_VERSION."""

    def test_schema_version_is_one(self) -> None:
        assert SCHEMA_VERSION == 1
