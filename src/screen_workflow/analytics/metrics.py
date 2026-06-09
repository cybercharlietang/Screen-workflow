"""Per-run summary metrics — a compact, comparable record of each live run.

Written on shutdown to a ``runs/`` dir that survives ``--reset`` (which only
wipes ``local_data``), so runs can be diffed across config changes (hash mode,
image px, dedup tweaks). Captures the things we actually want for testing:
timing, frame count + dedup keep-rate, sessions, cost, and an identification
roll-up (how many actions, how much was classified noise, mean confidence).

There is no automatic ground-truth "good/bad" — that's a human call — so we
store the objective proxies plus a ``quality_note`` field to fill in by hand.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from screen_workflow.storage.db import Store


def _audit_rollup(audit_dir: Path) -> dict[str, Any]:
    """Aggregate the per-session audit JSONs into identification metrics."""
    actions = 0
    by_kind: dict[str, int] = {}
    conf_sum = 0.0
    conf_n = 0
    files = sorted(audit_dir.glob("*.json")) if audit_dir.exists() else []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for ch in d.get("chunks", []):
            resp = ch.get("claude_response") or {}
            for a in resp.get("actions", []):
                actions += 1
                k = a.get("target_workflow_kind", "?")
                by_kind[k] = by_kind.get(k, 0) + 1
                c = a.get("confidence")
                if isinstance(c, (int, float)):
                    conf_sum += float(c)
                    conf_n += 1
    return {
        "audit_sessions": len(files),
        "actions_total": actions,
        "actions_by_kind": by_kind,
        "actions_noise": by_kind.get("noise", 0),
        "noise_ratio": round(by_kind.get("noise", 0) / actions, 3) if actions else 0.0,
        "avg_confidence": round(conf_sum / conf_n, 3) if conf_n else None,
    }


def build_run_summary(
    *,
    store: Store,
    audit_dir: Path,
    dedup_stats: dict[str, Any],
    config: dict[str, Any],
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Any]:
    sessions = list(store.iter_sessions())
    by_reason: dict[str, int] = {}
    for s in sessions:
        r = getattr(s.close_reason, "value", str(s.close_reason))
        by_reason[r] = by_reason.get(r, 0) + 1

    n_events = sum(1 for _ in store.iter_events())
    n_workflows = sum(1 for _ in store.iter_workflows())
    calls = list(store.iter_api_calls())
    in_tok = sum(c[4] for c in calls)
    out_tok = sum(c[5] for c in calls)
    usd = sum(c[6] for c in calls)

    dur_min = (ended_at - started_at).total_seconds() / 60.0
    usd_per_hour = (usd / (dur_min / 60.0)) if dur_min > 0 else 0.0

    return {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_minutes": round(dur_min, 1),
        "config": config,
        "capture": {
            "frames_kept": n_events,
            "dedup": dedup_stats,
        },
        "sessions": {"total": len(sessions), "by_close_reason": by_reason},
        "labeling": {
            "api_calls": len(calls),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "workflows_created": n_workflows,
            **_audit_rollup(audit_dir),
        },
        "cost": {
            "usd_total": round(usd, 4),
            "usd_per_hour": round(usd_per_hour, 4),
        },
        "quality_note": None,  # fill in by hand after eyeballing the run
    }


def write_run_summary(summary: dict[str, Any], runs_dir: Path, *, stamp: str) -> Path:
    """Write the full summary as run_<stamp>.json and append a compact line to
    runs.jsonl for quick cross-run comparison."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"run_{stamp}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    line = {
        "stamp": stamp,
        "duration_min": summary["duration_minutes"],
        "frames": summary["capture"]["frames_kept"],
        "keep_rate": summary["capture"]["dedup"].get("keep_rate"),
        "hash_mode": summary["config"].get("hash_mode"),
        "max_image_px": summary["config"].get("max_image_px"),
        "calls": summary["labeling"]["api_calls"],
        "usd": summary["cost"]["usd_total"],
        "usd_per_hour": summary["cost"]["usd_per_hour"],
        "noise_ratio": summary["labeling"].get("noise_ratio"),
        "avg_confidence": summary["labeling"].get("avg_confidence"),
        "workflows": summary["labeling"]["workflows_created"],
    }
    with (runs_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path
