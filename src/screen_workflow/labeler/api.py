"""Per-action routing labeler.

Each cognitive action in a session is independently routed to an existing
workflow, a brand-new workflow, or 'noise'. A single session can therefore
contribute observations to multiple workflows simultaneously. Noise actions
are logged on the relevant workflow but not stored as observations.

Each workflow also gets a goal-oriented summary (goal / resources /
trigger) that Claude populates and refines each call — the goal-level
artifact an automating agent will consume.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from screen_workflow.cost_monitor import CostMonitor

from screen_workflow.labeler.batch import DEFAULT_MAX_IMAGE_PX, build_batches
from screen_workflow.schemas import (
    CAGELabel,
    Event,
    Observation,
    Session,
    Workflow,
    WorkflowEdge,
    WorkflowNode,
)
from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-7"
MAX_TOKENS = 12000


class LabelerError(RuntimeError):
    pass


SYSTEM_PROMPT = """\
You are a procurement-workflow analyst at Fragment. You maintain a **directory
of workflow graphs**. Each workflow models one type of procurement task as
observed across many employee sessions (e.g. "Monitor purchase recommendation",
"Invoice reconciliation").

A single user session may contain:
  * actions from ONE workflow only,
  * actions from MULTIPLE workflows interleaved (vendor research, then a
    Slack interruption that turns into an invoice question, then back),
  * actions that are pure NOISE (lunch break, unrelated browsing, personal email).

You must route each cognitive action **individually**, not the session as a
whole.

For each call you receive:
  1. DIRECTORY OF EXISTING WORKFLOWS — each one's full compact graph
     plus its goal / resources / trigger.
  2. NEW SESSION — event log + screenshots.

Your job:

  A) Identify the cognitive actions (one per "cognitive unit" — five clicks
     filling one form is ONE action, not five). Use chronological proximity
     and same-app continuity as strong hints that consecutive frames belong
     to the same action.

  B) Route each action to one of:
        - an EXISTING workflow (by workflow_id)
        - a NEW workflow (provide name)
        - NOISE (the action is not procurement-relevant or is unidentifiable)

  C) For every workflow you touched in this session (existing or new), emit
     or refine its goal-oriented summary:
        goal       — one sentence: what success looks like
        resources  — list of systems / data sources / tools used
        trigger    — what initiated the workflow

  D) For non-noise actions:
        - Provide CAGE label, system, generic data_object_pattern
          ('PO #<num>', not 'PO #12345').
        - Provide per-action estimated_tokens (input+output of one LLM call
          an agent would make for this action) and expected_agent_steps.
        - Cite real frame_ids in evidence_frame_ids.

CAGE:
  C — Capture: ingesting data (reading email, opening record, downloading)
  A — Analyze: interpreting / comparing / deciding
  G — Generate: producing new content (drafts, replies, reports)
  E — Extract: pulling structured fields from unstructured sources

Token bands (per-observation; revise with evidence):
  Capture / Extract: 500–3,000   Analyze: 2,000–8,000   Generate: 1,500–10,000

Repeated actions across sessions collapse to ONE node (you map them to the
same node_id and we increment observation_count automatically).

Return ONLY this JSON shape (no prose, no markdown fences):

{
  "actions": [
    {
      "target_workflow_kind": "existing" | "new" | "noise",
      "target_workflow_id": "wf_..." | null,
      "target_workflow_name": "Short workflow name" | null,
      "node_id": "short-kebab-id",
      "canonical_name": "Human-friendly action name",
      "is_new_node": true,
      "cage_label": "C|A|G|E",
      "system": "...",
      "data_object_pattern": "PO #<num>",
      "evidence_frame_ids": ["...", "..."],
      "estimated_tokens": 2500,
      "expected_agent_steps": 1,
      "confidence": 0.85,
      "rationale": "one short sentence"
    }
  ],
  "edges": [
    {
      "target_workflow_id": "wf_..." | null,
      "target_workflow_name": "..." | null,
      "from_node": "...",
      "to_node": "..."
    }
  ],
  "workflow_summaries": [
    {
      "target_workflow_id": "wf_..." | null,
      "target_workflow_name": "..." | null,
      "goal": "One sentence: what success looks like.",
      "resources": ["Outlook inbox", "vendor websites", "internal budget tracker"],
      "trigger": "What initiates this workflow."
    }
  ]
}

Rules:
  * For target_workflow_kind="existing": target_workflow_id must match an
    id in the directory; target_workflow_name = null.
  * For "new": target_workflow_name required; target_workflow_id = null.
    Use the SAME new name across multiple actions belonging to that new
    workflow in this call.
  * For "noise": both ids/names = null; node fields can be minimal
    (canonical_name only — used for the noise log).
  * Every edge must have its workflow specified (same convention).
  * Provide a workflow_summaries entry for EVERY workflow you touched.
"""


# ---------------------------------------------------------------------------
# Directory + prompt construction
# ---------------------------------------------------------------------------


def _workflow_directory(store: Store) -> list[dict]:
    """Compact directory of existing workflows for the prompt."""
    out = []
    for wf in store.iter_workflows():
        out.append(
            {
                "workflow_id": wf.workflow_id,
                "name": wf.name,
                "goal": wf.goal,
                "resources": wf.resources,
                "trigger": wf.trigger,
                "is_complete": wf.is_complete,
                "sessions_processed": len(wf.sessions_processed),
                "nodes": [
                    {
                        "node_id": n.node_id,
                        "canonical_name": n.canonical_name,
                        "cage_label": n.cage_label.value,
                        "system": n.system,
                        "data_object_pattern": n.data_object_pattern,
                        "estimated_tokens_mean": n.estimated_tokens,
                        "observation_count": n.observation_count,
                    }
                    for n in wf.nodes.values()
                ],
                "edges": [
                    {"from": e.from_node, "to": e.to_node, "count": e.observation_count}
                    for e in wf.edges
                ],
            }
        )
    return out


def _build_messages(directory: list[dict], batch) -> list[dict]:
    dir_note = (
        "empty — every action will start a new workflow"
        if not directory
        else f"{len(directory)} workflow(s)"
    )
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"DIRECTORY OF EXISTING WORKFLOWS ({dir_note}):\n```json\n"
                        f"{json.dumps(directory, indent=2)}\n```\n\n"
                        "NEW SESSION EVENT LOG (tab-separated, one row per kept frame):\n\n"
                        f"{batch.event_log_text}\n\n"
                        "Screenshots follow in chronological order. Each is preceded by "
                        "a text marker giving its frame_id and ts."
                    ),
                },
                *batch.images,
                {
                    "type": "text",
                    "text": "Return the JSON object described in the system prompt. JSON only.",
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LabelerError(f"no JSON object in response: {text[:200]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LabelerError(f"response not valid JSON: {e}") from e


def _write_audit(
    root: Path,
    *,
    session: Session,
    chunk_audits: list[dict],
    touched_workflows: dict,
    observations: list,
    noise_count: int,
    model: str,
) -> None:
    """Persist what Claude returned across all chunks for this session.

    Saved at ``local_data/audit/<session_id>.json`` with one entry per chunk
    plus a session-level summary. Lets you see exactly what Claude saw and
    said for each API call.
    """
    try:
        audit_dir = root / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        total_input = sum(c.get("input_tokens") or 0 for c in chunk_audits)
        total_output = sum(c.get("output_tokens") or 0 for c in chunk_audits)
        payload = {
            "session_id": session.session_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "n_chunks": len(chunk_audits),
            "chunks": chunk_audits,
            "summary": {
                "workflows_touched": [
                    {"workflow_id": wf.workflow_id, "name": wf.name}
                    for wf in touched_workflows.values()
                ],
                "observations_inserted": len(observations),
                "noise_actions_count": noise_count,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
            },
        }
        (audit_dir / f"{session.session_id}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to write audit log; continuing")


# ---------------------------------------------------------------------------
# Merge: route per-action, create-or-update workflows, persist observations
# ---------------------------------------------------------------------------


def _new_workflow(name: str) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=f"wf_{uuid.uuid4().hex[:10]}",
        name=name,
        nodes={},
        edges=[],
        sessions_processed=[],
        stable_observations=0,
        is_complete=False,
        created_at=now,
        last_updated_at=now,
    )


def _resolve_workflow(
    *,
    store: Store,
    in_flight: dict[str, Workflow],
    kind: str | None,
    wf_id: str | None,
    wf_name: str | None,
) -> Workflow | None:
    """Resolve the target workflow for an action / edge / summary.

    in_flight: workflows touched this call so far, indexed by both id and
    (lowercased) name so multiple actions referencing the same new workflow
    share one Workflow object.
    """
    if kind == "noise":
        return None
    if kind == "existing":
        if wf_id and wf_id in in_flight:
            return in_flight[wf_id]
        if wf_id:
            existing = store.get_workflow(wf_id)
            if existing is not None:
                in_flight[wf_id] = existing
                return existing
        log.warning("claimed existing workflow_id=%s not found; falling back to new", wf_id)
        kind = "new"
        wf_name = wf_name or "Untitled workflow"
    if kind == "new":
        if not wf_name:
            return None
        key_by_name = f"name::{wf_name.lower()}"
        if key_by_name in in_flight:
            return in_flight[key_by_name]
        # Maybe a workflow with this exact name already exists in the store
        existing = store.find_workflow_by_name(wf_name)
        if existing is not None:
            in_flight[existing.workflow_id] = existing
            in_flight[key_by_name] = existing
            return existing
        wf = _new_workflow(wf_name)
        in_flight[wf.workflow_id] = wf
        in_flight[key_by_name] = wf
        return wf
    return None


def _apply_summary(wf: Workflow, summary: dict) -> None:
    goal = summary.get("goal")
    resources = summary.get("resources")
    trigger = summary.get("trigger")
    if isinstance(goal, str) and goal.strip():
        wf.goal = goal.strip()
    if isinstance(resources, list):
        wf.resources = [str(r) for r in resources if str(r).strip()]
    if isinstance(trigger, str) and trigger.strip():
        wf.trigger = trigger.strip()


def merge_response(
    response: dict,
    session: Session,
    events_by_id: dict[str, Event],
    store: Store,
) -> tuple[dict[str, Workflow], list[Observation], int]:
    """Apply Claude's response across many workflows.

    Returns (touched_workflows_by_id, observations_inserted, noise_count).
    Caller persists the workflows; observations are inserted by this function
    (so the node-level mean can be recomputed from store-truth).
    """
    in_flight: dict[str, Workflow] = {}
    structural_changes_by_wf: dict[str, bool] = {}
    observations: list[Observation] = []
    noise_count = 0
    now = datetime.now(timezone.utc)

    # 1) Actions — create nodes + observations on their target workflows
    for raw in response.get("actions", []) or []:
        try:
            kind = str(raw.get("target_workflow_kind", "")).lower()
            wf_id = raw.get("target_workflow_id")
            wf_name = raw.get("target_workflow_name")

            if kind == "noise":
                noise_count += 1
                log.info(
                    "  noise: %s (frames=%s) — %s",
                    raw.get("canonical_name", "?"),
                    raw.get("evidence_frame_ids", []),
                    raw.get("rationale", ""),
                )
                # Optionally attribute noise to a workflow if Claude gave one
                continue

            wf = _resolve_workflow(
                store=store, in_flight=in_flight, kind=kind, wf_id=wf_id, wf_name=wf_name
            )
            if wf is None:
                continue

            node_id = str(raw["node_id"])
            is_new = bool(raw.get("is_new_node", node_id not in wf.nodes))
            if node_id not in wf.nodes:
                wf.nodes[node_id] = WorkflowNode(
                    node_id=node_id,
                    canonical_name=str(raw["canonical_name"]),
                    cage_label=CAGELabel(raw["cage_label"]),
                    system=str(raw.get("system", "unknown")),
                    data_object_pattern=str(raw.get("data_object_pattern", "")),
                    estimated_tokens=0,
                    expected_agent_steps=1,
                    observation_count=0,
                    confidence=float(raw.get("confidence", 0.5)),
                    rationale=str(raw.get("rationale", "")),
                )
                structural_changes_by_wf[wf.workflow_id] = True
                log.info("  new node in %s: %s — %s", wf.name, node_id, raw["canonical_name"])

            evidence = [
                eid
                for eid in raw.get("evidence_frame_ids", []) or []
                if eid in events_by_id
            ]
            if not evidence:
                continue
            obs = Observation(
                observation_id=f"obs_{uuid.uuid4().hex[:10]}",
                workflow_id=wf.workflow_id,
                session_id=session.session_id,
                node_id=node_id,
                evidence_frame_ids=evidence,
                estimated_tokens=int(raw.get("estimated_tokens", 0)),
                expected_agent_steps=int(raw.get("expected_agent_steps", 1)),
                confidence=float(raw.get("confidence", 0.7)),
                observed_at=now,
            )
            wf.nodes[node_id].observation_count += 1
            observations.append(obs)

        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed action: %s | %r", e, raw)

    # 2) Edges — attach to their workflow if endpoints are known
    for raw in response.get("edges", []) or []:
        try:
            wf = _resolve_workflow(
                store=store,
                in_flight=in_flight,
                kind="existing" if raw.get("target_workflow_id") else "new",
                wf_id=raw.get("target_workflow_id"),
                wf_name=raw.get("target_workflow_name"),
            )
            if wf is None:
                continue
            f = str(raw["from_node"])
            t = str(raw["to_node"])
            if f not in wf.nodes or t not in wf.nodes:
                continue
            existing_keys = {(e.from_node, e.to_node) for e in wf.edges}
            if (f, t) not in existing_keys:
                wf.edges.append(WorkflowEdge(from_node=f, to_node=t, observation_count=1))
                structural_changes_by_wf[wf.workflow_id] = True
            else:
                for e in wf.edges:
                    if (e.from_node, e.to_node) == (f, t):
                        e.observation_count += 1
                        break
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed edge: %s | %r", e, raw)

    # 3) Workflow summaries — refine goal/resources/trigger on touched workflows
    for raw in response.get("workflow_summaries", []) or []:
        try:
            wf = _resolve_workflow(
                store=store,
                in_flight=in_flight,
                kind="existing" if raw.get("target_workflow_id") else "new",
                wf_id=raw.get("target_workflow_id"),
                wf_name=raw.get("target_workflow_name"),
            )
            if wf is None:
                continue
            _apply_summary(wf, raw)
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed summary: %s | %r", e, raw)

    # 4) Persist observations now so the mean computation is store-truthy
    for obs in observations:
        store.insert_observation(obs)

    # 5) Per-touched-workflow: recompute node-level token means, update
    #    bookkeeping fields, set is_complete if stable
    touched_workflows = {
        wf.workflow_id: wf
        for key, wf in in_flight.items()
        if key.startswith("wf_") or (not key.startswith("name::") and len(key) > 10)
    }
    for wf in touched_workflows.values():
        # Recompute means for nodes that have observations
        all_obs = list(store.iter_observations(workflow_id=wf.workflow_id))
        for node_id, node in wf.nodes.items():
            for_node = [o for o in all_obs if o.node_id == node_id]
            if for_node:
                node.estimated_tokens = int(
                    sum(o.estimated_tokens for o in for_node) / len(for_node)
                )
                node.expected_agent_steps = max(
                    1, round(sum(o.expected_agent_steps for o in for_node) / len(for_node))
                )

        # Stability bookkeeping
        if structural_changes_by_wf.get(wf.workflow_id):
            wf.stable_observations = 0
        else:
            wf.stable_observations += 1
        if wf.stable_observations >= wf.stability_threshold:
            wf.is_complete = True

        if session.session_id not in wf.sessions_processed:
            wf.sessions_processed.append(session.session_id)
        wf.noise_actions_count = wf.noise_actions_count  # (kept for future per-wf attribution)
        wf.last_updated_at = now

    return touched_workflows, observations, noise_count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_session(
    store: Store,
    session: Session,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    dry_run: bool = False,
    cost_monitor: "CostMonitor | None" = None,
    max_image_px: int = DEFAULT_MAX_IMAGE_PX,
) -> tuple[dict[str, Workflow], list[Observation], int]:
    """Run Claude on one session, splitting into multiple chunks if needed.

    Each chunk = one Anthropic call. The workflow graph in the store gets
    updated after each chunk, so the next chunk's directory reflects the
    state Claude built up so far — building the mental model incrementally.

    If ``cost_monitor`` is given, each chunk's token usage is recorded to it.
    The monitor is not consulted *here* to pause mid-session — the caller
    gates whole sessions via ``CostMonitor.should_pause()`` so a session is
    never left half-labeled; one session's overshoot is negligible.
    """
    events = list(store.iter_events(session.session_id))
    if not events:
        return {}, [], 0
    events_by_id = {e.event_id: e for e in events}

    batches = build_batches(events, store.screens_dir, max_image_px=max_image_px)
    log.info(
        "labeling session %s in %d chunk(s); total %d events",
        session.session_id,
        len(batches),
        len(events),
    )
    if dry_run:
        return {}, [], 0

    try:
        import anthropic
    except ImportError as e:
        raise LabelerError("anthropic SDK not installed (`pip install anthropic`)") from e

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if client.api_key is None:
        raise LabelerError("ANTHROPIC_API_KEY not set")

    all_touched: dict[str, Workflow] = {}
    all_observations: list[Observation] = []
    total_noise = 0
    chunk_audits: list[dict] = []

    for i, batch in enumerate(batches, start=1):
        # Re-read the directory each chunk so Claude sees the workflow state
        # produced by previous chunks of this same session.
        directory = _workflow_directory(store)
        log.info(
            "  chunk %d/%d: directory has %d workflow(s), %d images",
            i,
            len(batches),
            len(directory),
            len(batch.selected_frame_ids),
        )

        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=_build_messages(directory, batch),
        )
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        in_toks = getattr(resp.usage, "input_tokens", 0) or 0
        out_toks = getattr(resp.usage, "output_tokens", 0) or 0
        log.info(
            "  chunk %d: claude returned %d output tokens (stop_reason=%s)",
            i,
            out_toks,
            resp.stop_reason,
        )
        if cost_monitor is not None:
            snap = cost_monitor.record(
                model=model,
                input_tokens=in_toks,
                output_tokens=out_toks,
                session_id=session.session_id,
            )
            log.info(
                "  chunk %d: cost so far $%.2f this hour / $%.2f this run (%s)",
                i,
                snap.usd_last_hour,
                snap.usd_total_run,
                snap.state.value,
            )

        response = _extract_json(text)
        touched, observations, noise_count = merge_response(
            response, session, events_by_id, store
        )
        for wf in touched.values():
            store.upsert_workflow(wf)
            all_touched[wf.workflow_id] = wf
        all_observations.extend(observations)
        total_noise += noise_count
        chunk_audits.append({
            "chunk_index": i,
            "n_chunks": len(batches),
            "batch": {
                "selected_frame_ids": batch.selected_frame_ids,
                "approx_input_tokens": batch.approx_input_tokens,
            },
            "claude_response": response,
            "output_tokens": getattr(resp.usage, "output_tokens", None),
            "input_tokens": getattr(resp.usage, "input_tokens", None),
        })

    # Audit log: persist what happened across ALL chunks for this session.
    if chunk_audits:
        _write_audit(
            store.root,
            session=session,
            chunk_audits=chunk_audits,
            touched_workflows=all_touched,
            observations=all_observations,
            noise_count=total_noise,
            model=model,
        )
    touched = all_touched
    observations = all_observations
    noise_count = total_noise

    log.info(
        "session %s -> %d workflow(s) touched, %d observations, %d noise actions",
        session.session_id,
        len(touched),
        len(observations),
        noise_count,
    )
    for wf in touched.values():
        log.info(
            "  workflow '%s' (%s): %d nodes, %d edges, stable=%d/%d, complete=%s",
            wf.name,
            wf.workflow_id,
            len(wf.nodes),
            len(wf.edges),
            wf.stable_observations,
            wf.stability_threshold,
            wf.is_complete,
        )
    return touched, observations, noise_count


def process_all_unprocessed_sessions(
    store: Store,
    *,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    cost_monitor: "CostMonitor | None" = None,
    max_image_px: int = DEFAULT_MAX_IMAGE_PX,
) -> int:
    """Process every session not yet sent through the labeler.

    Dedup uses ``labeled_sessions`` (see ``Store.mark_session_labeled``), so
    noise-only sessions — which touch no workflow — are not re-labeled.
    """
    labeled = store.labeled_session_ids()
    n = 0
    for session in store.iter_sessions():
        if session.session_id in labeled:
            continue
        process_session(
            store, session, model=model, dry_run=dry_run,
            cost_monitor=cost_monitor, max_image_px=max_image_px,
        )
        if not dry_run:
            store.mark_session_labeled(session.session_id)
        n += 1
    return n


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen_workflow.labeler")
    p.add_argument("--root", default="./local_data")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-image-px", type=int, default=DEFAULT_MAX_IMAGE_PX,
        help="downscale screenshots to this long-edge px before sending",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    store = Store(Path(args.root))
    n = process_all_unprocessed_sessions(
        store, model=args.model, dry_run=args.dry_run, max_image_px=args.max_image_px
    )
    store.close()
    print(f"processed {n} new session(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
