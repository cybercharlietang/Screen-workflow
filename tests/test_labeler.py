"""Per-action routing labeler: batch builder + merge_response (no API calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.labeler.api import _extract_json, _new_workflow, merge_response
from screen_workflow.labeler.batch import APPROX_TOKENS_PER_IMAGE, build_batch
from screen_workflow.schemas import (
    Event,
    InputEvent,
    Session,
    SessionCloseReason,
    TriggerType,
)
from screen_workflow.storage.db import Store


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


def _session() -> Session:
    return Session(
        session_id="sess_1",
        start_ts=T0,
        end_ts=T0 + timedelta(minutes=5),
        close_reason=SessionCloseReason.IDLE_GAP,
        event_ids=["f_000", "f_001", "f_002"],
    )


def _events_by_id() -> dict:
    return {"f_000": _event(0), "f_001": _event(1), "f_002": _event(2)}


def _png(path: Path) -> None:
    from PIL import Image as _PIL
    path.parent.mkdir(parents=True, exist_ok=True)
    _PIL.new("RGB", (32, 32), color=(120, 80, 60)).save(path)


class TestBatch:
    def test_under_budget_keeps_all(self, tmp_path: Path) -> None:
        screens = tmp_path / "screens"
        events = []
        for i in range(5):
            events.append(_event(i))
            _png(screens / f"x_{i}.png")
        batch = build_batch(events, screens, budget_tokens=400_000)
        assert "frame_id\tts\tapp" in batch.event_log_text
        assert len(batch.selected_frame_ids) == 5

    def test_build_batches_splits_into_chunks(self, tmp_path: Path) -> None:
        from screen_workflow.labeler.batch import (
            MAX_IMAGES_PER_CHUNK,
            build_batches,
        )
        screens = tmp_path / "screens"
        events = []
        for i in range(MAX_IMAGES_PER_CHUNK * 2 + 5):
            events.append(_event(i))
            _png(screens / f"x_{i}.png")
        batches = build_batches(events, screens)
        assert len(batches) >= 3
        all_ids = []
        for b in batches:
            all_ids.extend(b.selected_frame_ids)
        assert sorted(all_ids) == sorted(e.event_id for e in events)
        for b in batches:
            assert len(b.selected_frame_ids) <= MAX_IMAGES_PER_CHUNK


class TestMergeResponse:
    def test_new_workflow_created_from_actions(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "data")
        response = {
            "actions": [
                {
                    "target_workflow_kind": "new",
                    "target_workflow_id": None,
                    "target_workflow_name": "Monitor purchase",
                    "node_id": "read-monitor-email",
                    "canonical_name": "Read monitor request email",
                    "is_new_node": True,
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "monitor request email",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 800,
                    "expected_agent_steps": 1,
                    "confidence": 0.9,
                    "rationale": "Initial email read.",
                }
            ],
            "edges": [],
            "workflow_summaries": [
                {
                    "target_workflow_id": None,
                    "target_workflow_name": "Monitor purchase",
                    "goal": "Recommend a monitor under £400 for a new hire.",
                    "resources": ["Outlook", "vendor websites"],
                    "trigger": "Email arrives requesting a monitor recommendation.",
                }
            ],
        }
        touched, obs, noise = merge_response(response, _session(), _events_by_id(), store)
        assert len(touched) == 1
        wf = list(touched.values())[0]
        assert wf.name == "Monitor purchase"
        assert wf.goal.startswith("Recommend a monitor")
        assert "Outlook" in wf.resources
        assert "read-monitor-email" in wf.nodes
        assert wf.nodes["read-monitor-email"].observation_count == 1
        assert wf.nodes["read-monitor-email"].estimated_tokens == 800
        assert len(obs) == 1
        assert noise == 0

    def test_noise_actions_counted_but_not_persisted(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "data")
        response = {
            "actions": [
                {
                    "target_workflow_kind": "new",
                    "target_workflow_id": None,
                    "target_workflow_name": "Monitor purchase",
                    "node_id": "read-monitor-email",
                    "canonical_name": "Read monitor request email",
                    "is_new_node": True,
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "monitor request email",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 800,
                    "expected_agent_steps": 1,
                    "confidence": 0.9,
                    "rationale": "",
                },
                {
                    "target_workflow_kind": "noise",
                    "target_workflow_id": None,
                    "target_workflow_name": None,
                    "node_id": "slack-ping",
                    "canonical_name": "Coworker Slack ping",
                    "is_new_node": True,
                    "cage_label": "C",
                    "system": "Slack",
                    "data_object_pattern": "",
                    "evidence_frame_ids": ["f_001"],
                    "estimated_tokens": 0,
                    "expected_agent_steps": 1,
                    "confidence": 0.9,
                    "rationale": "Unrelated message.",
                },
            ],
            "edges": [],
            "workflow_summaries": [],
        }
        touched, obs, noise = merge_response(response, _session(), _events_by_id(), store)
        assert noise == 1
        assert len(obs) == 1
        wf = list(touched.values())[0]
        assert "slack-ping" not in wf.nodes  # noise node not stored

    def test_multi_workflow_session(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "data")
        response = {
            "actions": [
                {
                    "target_workflow_kind": "new",
                    "target_workflow_id": None,
                    "target_workflow_name": "Monitor purchase",
                    "node_id": "read-email",
                    "canonical_name": "Read email",
                    "is_new_node": True,
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "email",
                    "evidence_frame_ids": ["f_000"],
                    "estimated_tokens": 800,
                    "expected_agent_steps": 1,
                    "confidence": 0.9,
                    "rationale": "",
                },
                {
                    "target_workflow_kind": "new",
                    "target_workflow_id": None,
                    "target_workflow_name": "Invoice check",
                    "node_id": "open-invoice",
                    "canonical_name": "Open invoice",
                    "is_new_node": True,
                    "cage_label": "C",
                    "system": "Outlook",
                    "data_object_pattern": "invoice",
                    "evidence_frame_ids": ["f_002"],
                    "estimated_tokens": 1200,
                    "expected_agent_steps": 1,
                    "confidence": 0.85,
                    "rationale": "",
                },
            ],
            "edges": [],
            "workflow_summaries": [],
        }
        touched, obs, _ = merge_response(response, _session(), _events_by_id(), store)
        assert len(touched) == 2
        names = {wf.name for wf in touched.values()}
        assert names == {"Monitor purchase", "Invoice check"}
        assert len(obs) == 2


class TestExtractJson:
    def test_strips_code_fences(self) -> None:
        text = '```json\n{"foo": 1}\n```'
        assert _extract_json(text) == {"foo": 1}

    def test_extracts_when_prose_around(self) -> None:
        text = 'Here you go: {"a": [1,2]}\nThanks!'
        assert _extract_json(text) == {"a": [1, 2]}
