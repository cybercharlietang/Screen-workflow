"""Fabricate a small Store of synthetic events/sessions/workflow/observations
for testing the visualizer without running the real daemon."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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


T0 = datetime(2026, 5, 19, 10, 0, 0, tzinfo=timezone.utc)


def _fake_screenshot(path: Path, caption: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 720), color=(245, 245, 248))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    d.rectangle([0, 0, 1280, 60], fill=(40, 40, 50))
    d.text((20, 16), "Synthetic screenshot — Screen-workflow PoC", fill=(255, 255, 255), font=font)
    d.text((40, 140), caption, fill=(20, 20, 30), font=font)
    img.save(path)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="./local_data")
    args = p.parse_args()

    store = Store(Path(args.root))

    apps = [
        ("OUTLOOK.EXE", "Inbox — alice@acme.com", TriggerType.WINDOW_FOCUS),
        ("CHROME.EXE", "Vendor Portal — Acme Supplies", TriggerType.CLICK),
        ("EXCEL.EXE", "Q2_budget.xlsx", TriggerType.HEARTBEAT),
        ("SAP.EXE", "PO Approval — #PO-12345", TriggerType.CLICK),
        ("OUTLOOK.EXE", "RE: PO-12345 approval needed", TriggerType.SUBMIT),
    ]

    event_ids: list[str] = []
    for i, (app, title, trig) in enumerate(apps):
        eid = f"demo_{i:03d}"
        rel = f"2026/05/19/{eid}.png"
        _fake_screenshot(store.screens_dir / rel, f"{app} — {title}")
        e = Event(
            event_id=eid,
            ts=T0 + timedelta(minutes=i * 2),
            app=app,
            window_title=title,
            trigger=InputEvent(type=trig, target_label="Approve" if trig is TriggerType.CLICK else None),
            screenshot_path=rel,
            ocr_text=f"(demo OCR placeholder for {title})",
        )
        store.insert_event(e)
        event_ids.append(eid)

    session = Session(
        session_id="demo-sess-1",
        start_ts=T0,
        end_ts=T0 + timedelta(minutes=10),
        close_reason=SessionCloseReason.DURATION_CAP,
        event_ids=event_ids,
    )
    store.insert_session(session)

    # Demo workflow with 3 nodes
    wf = Workflow(
        workflow_id="wf_demo",
        name="PO Approval (demo)",
        nodes={
            "read-po-email": WorkflowNode(
                node_id="read-po-email",
                canonical_name="Read PO approval request email",
                cage_label=CAGELabel.CAPTURE,
                system="Outlook",
                data_object_pattern="PO #<num> approval email",
                estimated_tokens=900,
                expected_agent_steps=1,
                observation_count=1,
                confidence=0.91,
                rationale="User reads the inbound approval request email.",
            ),
            "reconcile-vendor-budget": WorkflowNode(
                node_id="reconcile-vendor-budget",
                canonical_name="Reconcile vendor contract against budget",
                cage_label=CAGELabel.ANALYZE,
                system="Chrome + Excel",
                data_object_pattern="vendor contract vs. Q<n> budget",
                estimated_tokens=4200,
                expected_agent_steps=3,
                observation_count=1,
                confidence=0.78,
                rationale="User cross-checks vendor contract against budget line.",
            ),
            "draft-approval-reply": WorkflowNode(
                node_id="draft-approval-reply",
                canonical_name="Draft and send approval reply",
                cage_label=CAGELabel.GENERATE,
                system="Outlook",
                data_object_pattern="approval reply for PO #<num>",
                estimated_tokens=2100,
                expected_agent_steps=2,
                observation_count=1,
                confidence=0.85,
                rationale="User drafts and sends the approval response.",
            ),
        },
        edges=[
            WorkflowEdge(from_node="read-po-email", to_node="reconcile-vendor-budget", observation_count=1),
            WorkflowEdge(from_node="reconcile-vendor-budget", to_node="draft-approval-reply", observation_count=1),
        ],
        sessions_processed=["demo-sess-1"],
        stable_observations=0,
        stability_threshold=20,
        is_complete=False,
        created_at=T0,
        last_updated_at=T0,
    )
    store.upsert_workflow(wf)

    for i, (node_id, frames) in enumerate(
        [
            ("read-po-email", ["demo_000"]),
            ("reconcile-vendor-budget", ["demo_001", "demo_002"]),
            ("draft-approval-reply", ["demo_004"]),
        ]
    ):
        store.insert_observation(
            Observation(
                observation_id=f"obs_demo_{i}",
                workflow_id="wf_demo",
                session_id="demo-sess-1",
                node_id=node_id,
                evidence_frame_ids=frames,
                confidence=0.85,
                observed_at=T0 + timedelta(minutes=i * 2),
            )
        )

    store.close()
    print(f"seeded demo data into {Path(args.root).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
