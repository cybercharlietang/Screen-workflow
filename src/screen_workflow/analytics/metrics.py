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


LOW_CONFIDENCE = 0.7  # actions below this are HITL-review candidates


def _audit_rollup(audit_dir: Path) -> dict[str, Any]:
    """Aggregate the per-session audit JSONs into identification metrics."""
    actions = 0
    by_kind: dict[str, int] = {}
    conf_sum = 0.0
    conf_n = 0
    low_conf = 0
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
                    if c < LOW_CONFIDENCE:
                        low_conf += 1
    # Reuse rate = how often an action mapped to an existing workflow node vs a
    # new one — the convergence/maturity signal (rises as the directory matures).
    new_n = by_kind.get("new", 0)
    existing_n = by_kind.get("existing", 0)
    reuse_den = new_n + existing_n
    return {
        "audit_sessions": len(files),
        "actions_total": actions,
        "actions_by_kind": by_kind,
        "actions_noise": by_kind.get("noise", 0),
        "noise_ratio": round(by_kind.get("noise", 0) / actions, 3) if actions else 0.0,
        "avg_confidence": round(conf_sum / conf_n, 3) if conf_n else None,
        "low_confidence_actions": low_conf,
        "low_confidence_ratio": round(low_conf / conf_n, 3) if conf_n else None,
        "reuse_rate": round(existing_n / reuse_den, 3) if reuse_den else None,
    }


def _frames_per_trigger(store: Store) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in store.iter_events():
        t = getattr(e.trigger.type, "value", str(e.trigger.type))
        out[t] = out.get(t, 0) + 1
    return out


def _labeling_latency(store: Store, sessions: list) -> dict[str, Any]:
    """Seconds from a session closing (end_ts) to it being labeled."""
    times = store.labeled_session_times()
    lat: list[float] = []
    for s in sessions:
        t = times.get(s.session_id)
        if t is not None:
            lat.append((t - s.end_ts).total_seconds())
    if not lat:
        return {"labeled": 0, "mean_s": None, "median_s": None, "max_s": None}
    lat.sort()
    return {
        "labeled": len(lat),
        "mean_s": round(sum(lat) / len(lat), 1),
        "median_s": round(lat[len(lat) // 2], 1),
        "max_s": round(lat[-1], 1),
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
            "frames_by_trigger": _frames_per_trigger(store),
            "dedup": dedup_stats,
        },
        "sessions": {
            "total": len(sessions),
            "by_close_reason": by_reason,
            "labeling_latency": _labeling_latency(store, sessions),
        },
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

    lab = summary["labeling"]
    line = {
        "stamp": stamp,
        "duration_min": summary["duration_minutes"],
        "frames": summary["capture"]["frames_kept"],
        "keep_rate": summary["capture"]["dedup"].get("keep_rate"),
        "hash_mode": summary["config"].get("hash_mode"),
        "max_image_px": summary["config"].get("max_image_px"),
        "calls": lab["api_calls"],
        "usd": summary["cost"]["usd_total"],
        "usd_per_hour": summary["cost"]["usd_per_hour"],
        "noise_ratio": lab.get("noise_ratio"),
        "reuse_rate": lab.get("reuse_rate"),
        "avg_confidence": lab.get("avg_confidence"),
        "low_conf_ratio": lab.get("low_confidence_ratio"),
        "label_latency_s": summary["sessions"]["labeling_latency"].get("median_s"),
        "workflows": lab["workflows_created"],
    }
    with (runs_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path


# ---------------------------------------------------------------------------
# Post-hoc CLI: recompute a summary from a local_data dir, and/or print the
# cross-run comparison table from runs.jsonl.
# ---------------------------------------------------------------------------


def summarize_root(root: Path) -> dict[str, Any]:
    """Rebuild a run summary from an existing local_data dir (no re-run).

    Dedup keep-rate is read from ``_status.json`` if the daemon persisted it;
    older runs that predate that show a note. Timing comes from _status.json,
    falling back to the first/last event timestamp."""
    status: dict[str, Any] = {}
    sp = root / "_status.json"
    if sp.exists():
        try:
            status = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    dedup = status.get("dedup") or {"note": "unavailable (run predates dedup persistence)"}

    store = Store(root)
    try:
        events = list(store.iter_events())
        started = _parse_dt(status.get("started_at")) or (events[0].ts if events else None)
        ended = _parse_dt(status.get("heartbeat_ts")) or (events[-1].ts if events else None)
        if started is None or ended is None:
            started = ended = datetime.now().astimezone()
        return build_run_summary(
            store=store,
            audit_dir=root / "audit",
            dedup_stats=dedup,
            config={"source": "post-hoc", "root": str(root)},
            started_at=started,
            ended_at=ended,
        )
    finally:
        store.close()


def _parse_dt(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


_TABLE_COLS = [
    ("stamp", 17), ("duration_min", 8), ("frames", 7), ("keep_rate", 9),
    ("calls", 6), ("usd", 8), ("usd_per_hour", 8), ("noise_ratio", 6),
    ("reuse_rate", 6), ("low_conf_ratio", 7), ("label_latency_s", 8),
]


def format_runs_table(runs_dir: Path) -> str:
    p = runs_dir / "runs.jsonl"
    if not p.exists():
        return f"(no runs.jsonl in {runs_dir})"
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not rows:
        return "(runs.jsonl is empty)"
    out = [" ".join(f"{name:>{w}}" for name, w in _TABLE_COLS)]
    for r in rows:
        out.append(" ".join(f"{str(r.get(name, '-')):>{w}}" for name, w in _TABLE_COLS))
    return "\n".join(out)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen-workflow-metrics")
    p.add_argument("--root", default="./local_data", help="recompute a summary from this run dir")
    p.add_argument("--runs", default="./runs", help="runs/ dir holding runs.jsonl")
    p.add_argument("--no-summary", action="store_true", help="only print the runs table")
    args = p.parse_args()

    if not args.no_summary:
        root = Path(args.root)
        if (root / "events.db").exists():
            print(json.dumps(summarize_root(root), indent=2))
        else:
            print(f"(no events.db under {root}; skipping summary)")
    print("\n=== runs.jsonl ===")
    print(format_runs_table(Path(args.runs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
