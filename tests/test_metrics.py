"""Per-run metrics summary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.analytics.metrics import build_run_summary, write_run_summary
from screen_workflow.schemas import Event, InputEvent, Session, SessionCloseReason, TriggerType
from screen_workflow.storage.db import Store

T0 = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _seed(store: Store) -> None:
    for i in range(4):
        store.insert_event(
            Event(
                event_id=f"e{i}",
                ts=T0 + timedelta(seconds=i * 10),
                app="OUTLOOK.EXE",
                window_title=f"w{i}",
                trigger=InputEvent(type=TriggerType.CLICK),
                screenshot_path=f"x{i}.png",
            )
        )
    store.insert_session(
        Session(
            session_id="s1",
            start_ts=T0,
            end_ts=T0 + timedelta(seconds=30),
            close_reason=SessionCloseReason.BUFFER_FLUSH,
            event_ids=["e0", "e1", "e2", "e3"],
        )
    )
    store.insert_api_call(
        call_id="c1", ts=T0, model="claude-sonnet-4-6", session_id="s1",
        input_tokens=10_000, output_tokens=2_000, usd_cost=0.06,
    )


def _write_audit(audit_dir: Path) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "s1.json").write_text(json.dumps({
        "chunks": [{
            "claude_response": {
                "actions": [
                    {"target_workflow_kind": "new", "confidence": 0.9},
                    {"target_workflow_kind": "noise", "confidence": 0.8},
                ]
            }
        }]
    }), encoding="utf-8")


def test_build_and_write_run_summary(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    _seed(store)
    audit = tmp_path / "data" / "audit"
    _write_audit(audit)

    summary = build_run_summary(
        store=store,
        audit_dir=audit,
        dedup_stats={"mode": "perceptual", "seen": 12, "kept": 4, "keep_rate": 0.33},
        config={"hash_mode": "perceptual", "max_image_px": 1568},
        started_at=T0,
        ended_at=T0 + timedelta(hours=1),
    )
    store.close()

    assert summary["capture"]["frames_kept"] == 4
    assert summary["sessions"]["by_close_reason"] == {"buffer_flush": 1}
    assert summary["labeling"]["api_calls"] == 1
    assert summary["labeling"]["actions_total"] == 2
    assert summary["labeling"]["actions_noise"] == 1
    assert summary["labeling"]["noise_ratio"] == 0.5
    assert summary["labeling"]["avg_confidence"] == 0.85
    assert summary["cost"]["usd_total"] == 0.06
    assert summary["cost"]["usd_per_hour"] == 0.06  # 1-hour run

    runs = tmp_path / "runs"
    path = write_run_summary(summary, runs, stamp="20260609T130000Z")
    assert path.exists()
    line = json.loads((runs / "runs.jsonl").read_text().strip())
    assert line["frames"] == 4 and line["usd"] == 0.06 and line["noise_ratio"] == 0.5
