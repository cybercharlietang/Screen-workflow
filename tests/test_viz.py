"""Smoke test the static HTML report generator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.schemas import (
    CAGELabel,
    Event,
    InputEvent,
    Observation,
    Session,
    SessionCloseReason,
    TriggerType,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from screen_workflow.storage.db import Store
from screen_workflow.viz.report import render


T0 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)


def test_render_produces_self_contained_html(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    from PIL import Image as _PIL
    png = store.screens_dir / "2026/05/19/abc.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    _PIL.new("RGB", (32, 32), color=(120, 80, 60)).save(png)
    store.insert_event(
        Event(
            event_id="abc",
            ts=T0,
            app="OUTLOOK.EXE",
            window_title="Inbox",
            trigger=InputEvent(type=TriggerType.CLICK, target_label="Approve"),
            screenshot_path="2026/05/19/abc.png",
        )
    )
    store.insert_session(
        Session(
            session_id="s1",
            start_ts=T0,
            end_ts=T0 + timedelta(minutes=1),
            close_reason=SessionCloseReason.IDLE_GAP,
            event_ids=["abc"],
        )
    )
    store.upsert_workflow(
        Workflow(
            workflow_id="wf",
            name="Demo",
            nodes={
                "n": WorkflowNode(
                    node_id="n",
                    canonical_name="Open thing",
                    cage_label=CAGELabel.CAPTURE,
                    system="Outlook",
                    data_object_pattern="x",
                    estimated_tokens=1234,
                    observation_count=1,
                )
            },
            edges=[],
            sessions_processed=["s1"],
            created_at=T0,
            last_updated_at=T0,
        )
    )
    store.insert_observation(
        Observation(
            observation_id="obs1",
            workflow_id="wf",
            session_id="s1",
            node_id="n",
            evidence_frame_ids=["abc"],
            estimated_tokens=1234,
            expected_agent_steps=1,
            confidence=0.8,
            observed_at=T0,
        )
    )

    out = render(store, tmp_path / "viz")
    contents = out.read_text(encoding="utf-8")

    assert out.name == "index.html"
    assert "<!doctype html>" in contents
    # Data is now in a sibling data.json file fetched by JS.
    data_json = (tmp_path / "viz" / "data.json").read_text(encoding="utf-8")
    assert "data:image/jpeg;base64," in data_json
    assert "Demo" in data_json
    assert "Open thing" in data_json


def test_render_includes_cost_monitor_payload(tmp_path: Path) -> None:
    """The viz payload carries a cost-monitor snapshot + per-call history."""
    import json

    store = Store(tmp_path / "data")
    # Two API calls — one Sonnet, one Opus.
    store.insert_api_call(
        call_id="c1",
        ts=T0,
        model="claude-sonnet-4-6",
        input_tokens=30_000,
        output_tokens=4_000,
        usd_cost=0.15,
        session_id="s1",
    )
    store.insert_api_call(
        call_id="c2",
        ts=T0 + timedelta(minutes=1),
        model="claude-opus-4-7",
        input_tokens=30_000,
        output_tokens=4_000,
        usd_cost=0.75,
        session_id="s2",
    )

    render(store, tmp_path / "viz")
    data = json.loads((tmp_path / "viz" / "data.json").read_text(encoding="utf-8"))

    cm = data["cost_monitor"]
    assert cm["snapshot"] is not None
    assert cm["snapshot"]["n_calls"] == 2
    # Total run spend = 0.15 + 0.75 = 0.90
    assert cm["snapshot"]["usd_total_run"] == 0.9
    assert cm["calls_total"] == 2
    # Newest call first.
    assert cm["calls"][0]["model"] == "claude-opus-4-7"
    assert cm["calls"][1]["model"] == "claude-sonnet-4-6"


def test_render_cost_monitor_empty_when_no_calls(tmp_path: Path) -> None:
    """No api_calls -> a valid snapshot with zero spend, no crash."""
    import json

    store = Store(tmp_path / "data")
    render(store, tmp_path / "viz")
    data = json.loads((tmp_path / "viz" / "data.json").read_text(encoding="utf-8"))

    cm = data["cost_monitor"]
    assert cm["snapshot"]["n_calls"] == 0
    assert cm["snapshot"]["usd_total_run"] == 0
    assert cm["calls"] == []


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000156a4fa720000000049454e44"
    "ae426082"
)
