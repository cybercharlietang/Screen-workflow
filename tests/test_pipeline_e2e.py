"""End-to-end pipeline integration test (no API calls).

Wires synthetic Events through: storage write -> segmenter -> batch builder
-> a hand-crafted "Claude response" -> merge_response -> store. Verifies
the workflow graph ends up shaped as expected. Catches regressions across
stages that unit tests miss.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from screen_workflow.labeler.api import merge_response
from screen_workflow.labeler.batch import build_batch
from screen_workflow.schemas import Event, InputEvent, TriggerType
from screen_workflow.session.segmenter import SegmenterConfig, segment_and_persist
from screen_workflow.storage.db import Store


T0 = datetime(2026, 5, 19, 13, 0, 0, tzinfo=timezone.utc)


def _make_png(path: Path, color=(100, 100, 100)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=color).save(path)


def _seed_session(store: Store, n_events: int = 6) -> list[Event]:
    """Insert n synthetic events all within one session window."""
    events = []
    for i in range(n_events):
        eid = f"sim_{i:03d}"
        rel = f"2026/05/19/{eid}.png"
        _make_png(store.screens_dir / rel, color=(20 + i * 20, 80, 120))
        ev = Event(
            event_id=eid,
            ts=T0 + timedelta(seconds=i * 30),  # 30s apart, well within idle gap
            app="OUTLOOK.EXE" if i < 3 else "CHROME.EXE",
            window_title=f"window {i}",
            trigger=InputEvent(type=TriggerType.CLICK),
            screenshot_path=rel,
        )
        store.insert_event(ev)
        events.append(ev)
    return events


def test_segment_batch_merge_end_to_end(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")

    # 1) Seed events
    events = _seed_session(store, n_events=6)
    assert len(list(store.iter_events())) == 6

    # 2) Segment
    sessions = segment_and_persist(store, SegmenterConfig(idle_gap_seconds=120))
    assert len(sessions) == 1
    session = sessions[0]
    assert len(session.event_ids) == 6

    # 3) Build batch
    batch = build_batch(events, store.screens_dir, budget_tokens=400_000)
    assert len(batch.selected_frame_ids) == 6
    assert "frame_id\tts\tapp" in batch.event_log_text

    # 4) Hand-craft a Claude response that demonstrates the multi-workflow
    #    + noise behavior we want to validate.
    claude_response = {
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
                "evidence_frame_ids": ["sim_000", "sim_001"],
                "estimated_tokens": 900,
                "expected_agent_steps": 1,
                "confidence": 0.92,
                "rationale": "User reads the incoming hardware request.",
            },
            {
                "target_workflow_kind": "noise",
                "target_workflow_id": None,
                "target_workflow_name": None,
                "node_id": "slack-ping",
                "canonical_name": "Slack distraction",
                "is_new_node": True,
                "cage_label": "C",
                "system": "Slack",
                "data_object_pattern": "",
                "evidence_frame_ids": ["sim_002"],
                "estimated_tokens": 0,
                "expected_agent_steps": 1,
                "confidence": 0.9,
                "rationale": "Brief Slack ping, not procurement-relevant.",
            },
            {
                "target_workflow_kind": "new",
                "target_workflow_id": None,
                "target_workflow_name": "Monitor purchase",
                "node_id": "search-monitor-options",
                "canonical_name": "Search vendor sites for monitors",
                "is_new_node": True,
                "cage_label": "C",
                "system": "Chrome",
                "data_object_pattern": "vendor listings",
                "evidence_frame_ids": ["sim_003", "sim_004", "sim_005"],
                "estimated_tokens": 2400,
                "expected_agent_steps": 2,
                "confidence": 0.85,
                "rationale": "User browses 2-3 vendor product pages.",
            },
        ],
        "edges": [
            {
                "target_workflow_id": None,
                "target_workflow_name": "Monitor purchase",
                "from_node": "read-monitor-email",
                "to_node": "search-monitor-options",
            }
        ],
        "workflow_summaries": [
            {
                "target_workflow_id": None,
                "target_workflow_name": "Monitor purchase",
                "goal": "Recommend a monitor under £400 that matches the requested specs.",
                "resources": ["Outlook inbox", "vendor websites"],
                "trigger": "Manager emails requesting a hardware recommendation.",
            }
        ],
    }

    events_by_id = {e.event_id: e for e in events}
    touched, observations, noise_count = merge_response(
        claude_response, session, events_by_id, store
    )

    # 5) Verify the workflow graph is shaped correctly
    assert len(touched) == 1
    wf = list(touched.values())[0]
    assert wf.name == "Monitor purchase"
    assert wf.goal.startswith("Recommend a monitor")
    assert "Outlook inbox" in wf.resources
    assert wf.trigger.startswith("Manager")
    assert set(wf.nodes.keys()) == {"read-monitor-email", "search-monitor-options"}
    assert wf.nodes["read-monitor-email"].estimated_tokens == 900
    assert wf.nodes["search-monitor-options"].estimated_tokens == 2400
    assert len(wf.edges) == 1
    assert noise_count == 1
    assert len(observations) == 2  # noise wasn't persisted

    # 6) Persisting the workflow round-trips
    for w in touched.values():
        store.upsert_workflow(w)
    fetched = store.find_workflow_by_name("Monitor purchase")
    assert fetched is not None
    assert fetched.goal.startswith("Recommend a monitor")


def test_repeat_session_collapses_to_existing_nodes(tmp_path: Path) -> None:
    """Second session with the same actions should merge into existing nodes,
    not create new ones. Validates the 'unique actions' invariant."""
    store = Store(tmp_path / "data")

    # Two seeded sessions, each with 3 events.
    for run in range(2):
        for i in range(3):
            eid = f"r{run}_e{i}"
            rel = f"r{run}/{eid}.png"
            _make_png(store.screens_dir / rel)
            store.insert_event(
                Event(
                    event_id=eid,
                    ts=T0 + timedelta(hours=run, seconds=i * 30),
                    app="OUTLOOK.EXE",
                    window_title=f"w{i}",
                    trigger=InputEvent(type=TriggerType.CLICK),
                    screenshot_path=rel,
                )
            )
    sessions = segment_and_persist(store, SegmenterConfig(idle_gap_seconds=120))
    assert len(sessions) == 2

    # First session creates the workflow + a node.
    response_1 = {
        "actions": [
            {
                "target_workflow_kind": "new",
                "target_workflow_id": None,
                "target_workflow_name": "Foo",
                "node_id": "do-the-thing",
                "canonical_name": "Do the thing",
                "is_new_node": True,
                "cage_label": "C",
                "system": "Outlook",
                "data_object_pattern": "x",
                "evidence_frame_ids": ["r0_e0"],
                "estimated_tokens": 1000,
                "expected_agent_steps": 1,
                "confidence": 0.9,
                "rationale": "",
            }
        ],
        "edges": [],
        "workflow_summaries": [
            {
                "target_workflow_id": None,
                "target_workflow_name": "Foo",
                "goal": "g",
                "resources": ["o"],
                "trigger": "t",
            }
        ],
    }
    events_by_id_1 = {e.event_id: e for e in store.iter_events(sessions[0].session_id)}
    touched_1, _, _ = merge_response(response_1, sessions[0], events_by_id_1, store)
    for wf in touched_1.values():
        store.upsert_workflow(wf)
    wf_id = list(touched_1.values())[0].workflow_id

    # Second session refers to the existing workflow by id + node by id.
    response_2 = {
        "actions": [
            {
                "target_workflow_kind": "existing",
                "target_workflow_id": wf_id,
                "target_workflow_name": None,
                "node_id": "do-the-thing",
                "canonical_name": "Do the thing",
                "is_new_node": False,
                "cage_label": "C",
                "system": "Outlook",
                "data_object_pattern": "x",
                "evidence_frame_ids": ["r1_e0"],
                "estimated_tokens": 1400,  # different estimate
                "expected_agent_steps": 1,
                "confidence": 0.95,
                "rationale": "",
            }
        ],
        "edges": [],
        "workflow_summaries": [],
    }
    events_by_id_2 = {e.event_id: e for e in store.iter_events(sessions[1].session_id)}
    touched_2, _, _ = merge_response(response_2, sessions[1], events_by_id_2, store)
    for wf in touched_2.values():
        store.upsert_workflow(wf)

    fetched = store.get_workflow(wf_id)
    assert fetched is not None
    assert len(fetched.nodes) == 1                                # no duplicate node
    assert fetched.nodes["do-the-thing"].observation_count == 2   # incremented
    assert fetched.nodes["do-the-thing"].estimated_tokens == 1200 # mean of 1000, 1400
