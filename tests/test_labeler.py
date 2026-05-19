"""Workflow updater: batch builder + response merging (no API calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.labeler.api import (
    _extract_json,
    _merge_into_workflow,
    _new_workflow,
    _select_or_create_workflow,
)
from screen_workflow.labeler.batch import APPROX_TOKENS_PER_IMAGE, build_batch
from screen_workflow.schemas import (
    Event,
    InputEvent,
    Session,
    SessionCloseReason,
    TriggerType,
)


T0 = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)


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


class TestBatch:
    def test_includes_event_log_and_all_images_when_under_budget(self, tmp_path: Path) -> None:
        screens = tmp_path / "screens"
        events = []
        for i in range(5):
            events.append(_event(i))
            _png(screens / f"x_{i}.png")
        batch = build_batch(events, screens, budget_tokens=400_000)
        assert "frame_id\tts\tapp" in batch.event_log_text
        assert len(batch.selected_frame_ids) == 5
        assert batch.dropped_frame_ids == []

    def test_downsamples_when_over_budget(self, tmp_path: Path) -> None:
        screens = tmp_path / "screens"
        events = []
        for i in range(40):
            events.append(_event(i))
            _png(screens / f"x_{i}.png")
        budget = 5_000 + 5 * APPROX_TOKENS_PER_IMAGE
        batch = build_batch(events, screens, budget_tokens=budget)
        assert len(batch.selected_frame_ids) < 40
        assert events[0].event_id in batch.selected_frame_ids
        assert events[-1].event_id in batch.selected_frame_ids


class TestMerge:
    def _session(self) -> Session:
        return Session(
            session_id="sess_1",
            start_ts=T0,
            end_ts=T0 + timedelta(minutes=5),
            close_reason=SessionCloseReason.IDLE_GAP,
            event_ids=["f_000", "f_001"],
        )

    def _events_by_id(self) -> dict:
        return {"f_000": _event(0), "f_001": _event(1)}

    def test_first_call_creates_nodes_and_resets_stable(self, tmp_path) -> None:
        from screen_workflow.storage.db import Store
        store = Store(tmp_path / "data")
        wf = _new_workflow("PO Approval")
        response = {
            "added_or_updated_nodes": [
                {
                    "node_id": "open-po-email",
                    "canonical_name": "Open PO email",
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "PO #<num>",
                    "confidence": 0.9,
                    "rationale": "User opened approval email.",
                    "is_new": True,
                }
            ],
            "added_or_updated_edges": [],
            "observations": [
                {
                    "node_id": "open-po-email",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 900,
                    "expected_agent_steps": 1,
                    "confidence": 0.9,
                }
            ],
        }
        wf, obs, changed = _merge_into_workflow(wf, response, self._session(), self._events_by_id(), store)
        assert "open-po-email" in wf.nodes
        assert wf.nodes["open-po-email"].observation_count == 1
        assert wf.nodes["open-po-email"].estimated_tokens == 900  # mean of one obs
        assert len(obs) == 1
        assert changed is True
        assert wf.stable_observations == 0

    def test_repeat_observation_increments_count_no_structural_change(self, tmp_path) -> None:
        from screen_workflow.storage.db import Store
        store = Store(tmp_path / "data")
        wf = _new_workflow("PO Approval")
        response_1 = {
            "added_or_updated_nodes": [
                {
                    "node_id": "n",
                    "canonical_name": "X",
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "p",
                    "confidence": 0.8,
                    "rationale": "",
                    "is_new": True,
                }
            ],
            "added_or_updated_edges": [],
            "observations": [
                {
                    "node_id": "n",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 500,
                    "expected_agent_steps": 1,
                    "confidence": 0.8,
                }
            ],
        }
        wf, _, _ = _merge_into_workflow(wf, response_1, self._session(), self._events_by_id(), store)

        # second call: same node, no new structure, different token estimate
        sess2 = Session(
            session_id="sess_2",
            start_ts=T0 + timedelta(hours=1),
            end_ts=T0 + timedelta(hours=1, minutes=1),
            close_reason=SessionCloseReason.IDLE_GAP,
            event_ids=["f_000"],
        )
        response_2 = {
            "added_or_updated_nodes": [],
            "added_or_updated_edges": [],
            "observations": [
                {
                    "node_id": "n",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 700,
                    "expected_agent_steps": 1,
                    "confidence": 0.85,
                }
            ],
        }
        wf, obs, changed = _merge_into_workflow(wf, response_2, sess2, self._events_by_id(), store)
        assert changed is False
        assert wf.stable_observations == 1
        assert wf.nodes["n"].observation_count == 2  # obs(1) + obs(1)
        # mean of 500 and 700
        assert wf.nodes["n"].estimated_tokens == 600

    def test_stability_threshold_marks_complete(self, tmp_path) -> None:
        from screen_workflow.schemas import CAGELabel, WorkflowNode
        from screen_workflow.storage.db import Store
        store = Store(tmp_path / "data")
        wf = _new_workflow("PO Approval")
        wf.stability_threshold = 3
        wf.nodes = {
            "n": WorkflowNode(
                node_id="n",
                canonical_name="x",
                cage_label=CAGELabel.CAPTURE,
                system="o",
                data_object_pattern="p",
                estimated_tokens=100,
            )
        }
        no_op = {"added_or_updated_nodes": [], "added_or_updated_edges": [], "observations": []}
        for i in range(3):
            sess = Session(
                session_id=f"s_{i}",
                start_ts=T0,
                end_ts=T0 + timedelta(seconds=1),
                close_reason=SessionCloseReason.IDLE_GAP,
                event_ids=["f_000"],
            )
            wf, _, _ = _merge_into_workflow(wf, no_op, sess, self._events_by_id(), store)
        assert wf.is_complete is True


class TestExtractJson:
    def test_strips_code_fences(self) -> None:
        text = '```json\n{"foo": 1}\n```'
        assert _extract_json(text) == {"foo": 1}

    def test_extracts_when_prose_around(self) -> None:
        text = 'Here you go: {"a": [1,2]}\nThanks!'
        assert _extract_json(text) == {"a": [1, 2]}
