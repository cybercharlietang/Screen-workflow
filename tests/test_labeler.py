"""Batch builder + response-parsing tests (no API calls)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from screen_workflow.labeler.api import _action_to_label, _parse_response_text
from screen_workflow.labeler.batch import APPROX_TOKENS_PER_IMAGE, build_batch
from screen_workflow.schemas import (
    Event,
    InputEvent,
    Session,
    SessionCloseReason,
    TriggerType,
)


T0 = datetime(2026, 5, 19, 13, 0, 0)


def _event(i: int, trigger: TriggerType = TriggerType.CLICK) -> Event:
    return Event(
        event_id=f"f_{i:03d}",
        ts=T0 + timedelta(seconds=i * 5),
        app="OUTLOOK.EXE",
        window_title=f"Inbox {i}",
        trigger=InputEvent(type=trigger),
        screenshot_path=f"x_{i}.png",
    )


def _png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63000100000005000156a4fa720000000049454e44"
            "ae426082"
        )
    )


def test_batch_includes_event_log_and_all_images_when_under_budget(tmp_path: Path) -> None:
    screens = tmp_path / "screens"
    events = []
    for i in range(5):
        events.append(_event(i))
        _png(screens / f"x_{i}.png")
        events[-1] = events[-1].model_copy(update={"screenshot_path": f"x_{i}.png"})

    batch = build_batch(events, screens, budget_tokens=400_000)
    assert "frame_id\tts\tapp" in batch.event_log_text
    assert len(batch.selected_frame_ids) == 5
    assert batch.dropped_frame_ids == []


def test_batch_downsamples_when_over_budget(tmp_path: Path) -> None:
    screens = tmp_path / "screens"
    events = []
    for i in range(40):
        events.append(_event(i))
        _png(screens / f"x_{i}.png")
        events[-1] = events[-1].model_copy(update={"screenshot_path": f"x_{i}.png"})

    # 40 events, budget that fits ~5 images after overhead
    budget = 5_000 + 5 * APPROX_TOKENS_PER_IMAGE
    batch = build_batch(events, screens, budget_tokens=budget)
    assert len(batch.selected_frame_ids) < 40
    assert len(batch.dropped_frame_ids) > 0
    # first and last always kept
    assert events[0].event_id in batch.selected_frame_ids
    assert events[-1].event_id in batch.selected_frame_ids


def test_must_keep_triggers_preferred(tmp_path: Path) -> None:
    screens = tmp_path / "screens"
    events = []
    for i in range(20):
        trig = TriggerType.SUBMIT if i == 7 else TriggerType.CLICK
        e = _event(i, trig)
        events.append(e.model_copy(update={"screenshot_path": f"x_{i}.png"}))
        _png(screens / f"x_{i}.png")

    budget = 10_000 + 4 * APPROX_TOKENS_PER_IMAGE
    batch = build_batch(events, screens, budget_tokens=budget)
    assert "f_007" in batch.selected_frame_ids  # the SUBMIT one


def test_parse_response_text_extracts_actions() -> None:
    text = """Here you go:
```json
{"actions": [{"action_id": "a1", "cage_label": "C", "system": "Outlook",
"data_object": "PO email", "estimated_tokens": 800,
"expected_agent_steps": 1, "start_frame_id": "f_000",
"end_frame_id": "f_001", "evidence_frame_ids": ["f_000"],
"confidence": 0.9, "rationale": "Read inbound PO email."}]}
```"""
    actions = _parse_response_text(text)
    assert len(actions) == 1
    assert actions[0]["cage_label"] == "C"


def test_action_to_label_validates_and_handles_missing_evidence() -> None:
    session = Session(
        session_id="s1",
        start_ts=T0,
        end_ts=T0 + timedelta(seconds=60),
        close_reason=SessionCloseReason.IDLE_GAP,
        event_ids=["f_000", "f_001"],
    )
    events_by_id = {"f_000": _event(0), "f_001": _event(1)}
    action = {
        "action_id": "a1",
        "cage_label": "C",
        "system": "Outlook",
        "data_object": "PO email",
        "estimated_tokens": 800,
        "expected_agent_steps": 1,
        "start_frame_id": "f_000",
        "end_frame_id": "f_001",
        "evidence_frame_ids": ["f_000", "f_001"],
        "confidence": 0.9,
        "rationale": "Read inbound PO email.",
    }
    label = _action_to_label(action, session, events_by_id)
    assert label is not None
    assert label.cage_label.value == "C"
    assert label.evidence_frame_ids == ["f_000", "f_001"]


def test_action_with_bogus_frame_ids_dropped() -> None:
    session = Session(
        session_id="s1",
        start_ts=T0,
        end_ts=T0 + timedelta(seconds=60),
        close_reason=SessionCloseReason.IDLE_GAP,
        event_ids=["f_000"],
    )
    events_by_id = {"f_000": _event(0)}
    action = {
        "cage_label": "C",
        "system": "Outlook",
        "data_object": "x",
        "estimated_tokens": 500,
        "evidence_frame_ids": ["f_999"],  # doesn't exist
        "confidence": 0.8,
        "rationale": "x",
    }
    assert _action_to_label(action, session, events_by_id) is None
