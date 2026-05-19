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
    png = store.screens_dir / "2026/05/19/abc.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(_TINY_PNG)
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
            confidence=0.8,
            observed_at=T0,
        )
    )

    out = render(store, tmp_path / "viz")
    contents = out.read_text(encoding="utf-8")

    assert out.name == "index.html"
    assert "<!doctype html>" in contents
    assert "data:image/png;base64," in contents
    assert "Demo" in contents              # workflow name in payload
    assert "Open thing" in contents        # node name


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000156a4fa720000000049454e44"
    "ae426082"
)
