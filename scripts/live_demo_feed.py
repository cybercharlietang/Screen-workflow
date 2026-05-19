"""Simulated event feed for the live viewer.

Inserts a fake captured Event into the same DB the live viewer is reading
every few seconds, so you can confirm the auto-refresh chain works end-to-end
without needing the real daemon (which requires a real display)."""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from screen_workflow.schemas import Event, InputEvent, TriggerType
from screen_workflow.storage.db import Store


APPS = [
    ("OUTLOOK.EXE", "Inbox — alice@acme.com"),
    ("CHROME.EXE", "Vendor Portal — Acme Supplies"),
    ("CHROME.EXE", "SAP Web Client — PO Search"),
    ("EXCEL.EXE", "Q2_budget.xlsx — Q2 Marketing line"),
    ("SAP.EXE", "PO Approval — #PO-12345"),
    ("OUTLOOK.EXE", "RE: PO-12345 approval needed"),
    ("CHROME.EXE", "DocuSign — vendor MSA"),
]
TRIGGERS = [
    TriggerType.WINDOW_FOCUS,
    TriggerType.CLICK,
    TriggerType.HEARTBEAT,
    TriggerType.CLICK,
    TriggerType.SUBMIT,
    TriggerType.PASTE,
    TriggerType.SAVE,
]


def _fake_screenshot(path: Path, caption: str, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 720), color=(245, 245, 248))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    d.rectangle([0, 0, 1280, 60], fill=color)
    d.text((20, 16), "Simulated live event — Screen-workflow PoC", fill=(255, 255, 255), font=font)
    d.text((40, 140), caption, fill=(20, 20, 30), font=font)
    d.text((40, 200), f"generated {datetime.now().isoformat(timespec='seconds')}", fill=(80, 80, 80), font=font)
    img.save(path)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="./local_data")
    p.add_argument("--interval", type=float, default=6.0)
    p.add_argument("--seconds", type=float, default=900)  # 15 min
    args = p.parse_args()

    store = Store(Path(args.root))
    start = time.monotonic()
    i = 0
    rng = random.Random(0)

    while time.monotonic() - start < args.seconds:
        app, title = rng.choice(APPS)
        trig = rng.choice(TRIGGERS)
        i += 1
        eid = f"sim_{int(time.time() * 1000)}"
        now = datetime.now(timezone.utc)
        rel = f"{now.year:04d}/{now.month:02d}/{now.day:02d}/{eid}.png"
        color = (
            rng.randint(20, 60),
            rng.randint(20, 60),
            rng.randint(40, 120),
        )
        _fake_screenshot(store.screens_dir / rel, f"{app} — {title}", color)
        store.insert_event(
            Event(
                event_id=eid,
                ts=now,
                app=app,
                window_title=title,
                trigger=InputEvent(
                    type=trig,
                    target_label="Approve" if trig is TriggerType.CLICK else None,
                ),
                screenshot_path=rel,
                ocr_text=f"(simulated OCR for {title})",
            )
        )
        print(f"injected event #{i} {trig.value} {app} {title}")
        time.sleep(args.interval)

    store.close()
    print("feed finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
