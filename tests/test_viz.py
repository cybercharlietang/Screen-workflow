"""Smoke test the static HTML report generator."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from screen_workflow.schemas import (
    CAGELabel,
    Event,
    InputEvent,
    Label,
    Session,
    SessionCloseReason,
    TriggerType,
)
from screen_workflow.storage.db import Store
from screen_workflow.viz.report import render


T0 = datetime(2026, 5, 19, 10, 0, 0)


def test_render_produces_self_contained_html(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    # one event with a tiny real PNG file so base64-inlining has something to do
    png = store.screens_dir / "2026/05/19/abc.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(_TINY_PNG)
    e = Event(
        event_id="abc",
        ts=T0,
        app="OUTLOOK.EXE",
        window_title="Inbox",
        trigger=InputEvent(type=TriggerType.CLICK, target_label="Approve"),
        screenshot_path="2026/05/19/abc.png",
    )
    store.insert_event(e)
    s = Session(
        session_id="s1",
        start_ts=T0,
        end_ts=T0 + timedelta(minutes=1),
        close_reason=SessionCloseReason.IDLE_GAP,
        event_ids=["abc"],
    )
    store.insert_session(s)
    store.insert_label(
        Label(
            action_id="a1",
            session_id="s1",
            cage_label=CAGELabel.ANALYZE,
            system="SAP",
            data_object="PO 1",
            estimated_tokens=1234,
            start_ts=T0,
            end_ts=T0 + timedelta(seconds=30),
            evidence_frame_ids=["abc"],
            confidence=0.8,
            rationale="r",
        )
    )

    out = render(store, tmp_path / "viz")
    contents = out.read_text(encoding="utf-8")

    assert out.name == "index.html"
    assert "<!doctype html>" in contents
    assert "data:image/png;base64," in contents  # inlined image
    assert '"event_id": "abc"' in contents      # inlined JSON payload
    assert "estimated_tokens" in contents


# 1x1 transparent PNG.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63000100000005000156a4fa720000000049454e44"
    "ae426082"
)
