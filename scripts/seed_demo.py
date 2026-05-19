"""Fabricate a small Store of synthetic events/sessions/labels for testing
the visualizer without running the real daemon.

Usage:
    python scripts/seed_demo.py [--root ./local_data]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
        ("OUTLOOK.EXE", "Inbox — alice@acme.com"),
        ("CHROME.EXE", "Vendor Portal — Acme Supplies"),
        ("EXCEL.EXE", "Q2_budget.xlsx"),
        ("SAP.EXE", "PO Approval — #PO-12345"),
        ("OUTLOOK.EXE", "RE: PO-12345 approval needed"),
    ]
    triggers = [
        TriggerType.WINDOW_FOCUS,
        TriggerType.CLICK,
        TriggerType.HEARTBEAT,
        TriggerType.CLICK,
        TriggerType.SUBMIT,
    ]

    event_ids: list[str] = []
    for i, ((app, title), trig) in enumerate(zip(apps, triggers, strict=True)):
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

    labels = [
        Label(
            action_id="act-1",
            session_id="demo-sess-1",
            cage_label=CAGELabel.CAPTURE,
            system="Outlook",
            data_object="PO #12345 approval email",
            estimated_tokens=900,
            start_ts=T0,
            end_ts=T0 + timedelta(minutes=2),
            evidence_frame_ids=["demo_000"],
            confidence=0.91,
            rationale="User reads the inbound approval request email.",
        ),
        Label(
            action_id="act-2",
            session_id="demo-sess-1",
            cage_label=CAGELabel.ANALYZE,
            system="Chrome+Excel",
            data_object="vendor contract vs. Q2 budget",
            estimated_tokens=4200,
            start_ts=T0 + timedelta(minutes=2),
            end_ts=T0 + timedelta(minutes=6),
            evidence_frame_ids=["demo_001", "demo_002"],
            confidence=0.78,
            rationale="User cross-checks vendor contract against budget line.",
        ),
        Label(
            action_id="act-3",
            session_id="demo-sess-1",
            cage_label=CAGELabel.GENERATE,
            system="Outlook",
            data_object="approval reply",
            estimated_tokens=2100,
            start_ts=T0 + timedelta(minutes=8),
            end_ts=T0 + timedelta(minutes=10),
            evidence_frame_ids=["demo_004"],
            confidence=0.85,
            rationale="User drafts and sends the approval response.",
        ),
    ]
    for lbl in labels:
        store.insert_label(lbl)

    store.close()
    print(f"seeded demo data into {Path(args.root).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
