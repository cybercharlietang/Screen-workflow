"""Auto-routing per-workflow labeler.

For each session: Claude sees the directory of existing workflows (compact
summaries) and the new session. It either updates an existing workflow or
creates a new one, and emits per-observation token estimates that get
aggregated into the node's mean estimated_tokens.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from screen_workflow.labeler.batch import build_batch
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
MAX_TOKENS = 8000


class LabelerError(RuntimeError):
    pass


SYSTEM_PROMPT = """\
You are a procurement-workflow analyst at Fragment. You maintain a **directory
of workflow graphs**. Each workflow models one type of procurement task as
observed across many employee sessions (e.g. "Monitor purchase",
"Invoice reconciliation"). The output is the workflow graph itself —
unique abstract actions with observation counts and agent-token cost
estimates.

For each call you receive:
  1. DIRECTORY OF EXISTING WORKFLOWS — each one with its full graph
     (nodes, edges, observation_counts).
  2. NEW SESSION — event log + screenshots.

YOU MUST:

  A) Decide whether the new session belongs to one of the existing
     workflows or is a NEW one. Match on cognitive task, not surface
     details. If the directory is empty, treat the session as starting
     a new workflow.

  B) For the chosen / new workflow, identify the cognitive actions in
     the new session and produce:
       - any new or updated nodes
       - any new or updated edges
       - one Observation per cognitive action you see in THIS session
         (each Observation maps to one node, with its OWN per-instance
         estimated_tokens — these aggregate across sessions to give a
         mean per node).

  C) Repeated actions across sessions collapse to ONE node. If a session
     shows the user "Open PO email" five times, that's ONE node with
     observation_count=5 across sessions, not five nodes.

CAGE taxonomy:
  C — Capture: ingesting data (reading email, opening record, downloading)
  A — Analyze: interpreting / comparing / deciding
  G — Generate: producing new content (drafts, replies, reports)
  E — Extract: pulling structured fields from unstructured sources

Guidelines:
  * data_object_pattern is GENERIC: "PO #<num>" not "PO #12345".
  * Merge into cognitive units: 5 clicks in one form = 1 node, not 5.
  * Ignore noise: Slack pings, lunch, unrelated browsing — omit.
  * estimated_tokens per observation = input + output of ONE LLM call an
    agent would make to do this action this time. Ranges:
      Capture / Extract: 500–3,000   |   Analyze: 2,000–8,000
      Generate: 1,500–10,000
  * expected_agent_steps per observation: 1 (read+act), 2–3 (compare+decide),
    4+ (genuinely multi-step).
  * confidence 0.0–1.0 — be honest.
  * node_id for new nodes: short kebab-case from canonical_name.

Return ONLY this JSON shape (no prose, no markdown fences):

{
  "target_workflow": {
    "kind": "existing" | "new",
    "existing_id": "wf_..." | null,
    "new_name": "Short workflow name (only if kind=new)" | null,
    "rationale": "one short sentence on why this workflow"
  },
  "added_or_updated_nodes": [
    {
      "node_id": "...",
      "canonical_name": "...",
      "cage_label": "C|A|G|E",
      "system": "...",
      "data_object_pattern": "...",
      "confidence": 0.82,
      "rationale": "one short sentence",
      "is_new": true
    }
  ],
  "added_or_updated_edges": [
    {"from_node": "...", "to_node": "..."}
  ],
  "observations": [
    {
      "node_id": "...",
      "evidence_frame_ids": ["...", "..."],
      "estimated_tokens": 2500,
      "expected_agent_steps": 1,
      "confidence": 0.85
    }
  ]
}

Rules:
  * If target_workflow.kind == "existing", existing_id MUST be one of the
    ids in the directory. new_name is null.
  * If kind == "new", new_name MUST be non-empty. existing_id is null.
  * Every observation.node_id must match either a node already in the
    chosen workflow OR a node you are introducing in added_or_updated_nodes.
  * Every edge endpoint must be a known node id.
"""


# ---------------------------------------------------------------------------
# Directory + prompt construction
# ---------------------------------------------------------------------------


def _workflow_directory(store: Store) -> list[dict]:
    """Compact representation of every existing workflow, suitable for prompting."""
    directory = []
    for wf in store.iter_workflows():
        directory.append(
            {
                "workflow_id": wf.workflow_id,
                "name": wf.name,
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
                        "expected_agent_steps": n.expected_agent_steps,
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
    return directory


def _build_messages(directory: list[dict], batch) -> list[dict]:
    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                "DIRECTORY OF EXISTING WORKFLOWS "
                f"({'empty — this session starts a new workflow' if not directory else f'{len(directory)} workflow(s)'}):\n"
                "```json\n"
                f"{json.dumps(directory, indent=2)}\n"
                "```\n\n"
                "NEW SESSION EVENT LOG (tab-separated, one row per kept frame):\n\n"
                f"{batch.event_log_text}\n\n"
                "Screenshots follow in chronological order. Each is preceded by a "
                "text marker giving its frame_id and ts."
            ),
        },
        *batch.images,
        {
            "type": "text",
            "text": "Return the JSON object described in the system prompt. JSON only.",
        },
    ]
    return [{"role": "user", "content": user_content}]


# ---------------------------------------------------------------------------
# Response parsing + merging
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LabelerError(f"no JSON object in response: {text[:200]!r}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LabelerError(f"response not valid JSON: {e}") from e


def _select_or_create_workflow(
    store: Store, response: dict, force_name: str | None = None
) -> Workflow:
    """Choose the target workflow per Claude's verdict, or create a new one."""
    tw = response.get("target_workflow") or {}
    kind = tw.get("kind")
    now = datetime.now(timezone.utc)

    if force_name:
        existing = store.find_workflow_by_name(force_name)
        if existing is not None:
            return existing
        return _new_workflow(force_name)

    if kind == "existing":
        eid = tw.get("existing_id")
        if eid:
            wf = store.get_workflow(eid)
            if wf is not None:
                return wf
            log.warning(
                "Claude routed to workflow_id=%s but it was not found; creating new",
                eid,
            )
    name = tw.get("new_name") or "Untitled workflow"
    return _new_workflow(name)


def _new_workflow(workflow_name: str) -> Workflow:
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=f"wf_{uuid.uuid4().hex[:10]}",
        name=workflow_name,
        nodes={},
        edges=[],
        sessions_processed=[],
        stable_observations=0,
        is_complete=False,
        created_at=now,
        last_updated_at=now,
    )


def _merge_into_workflow(
    workflow: Workflow,
    response: dict,
    session: Session,
    events_by_id: dict[str, Event],
    store: Store,
) -> tuple[Workflow, list[Observation], bool]:
    """Apply Claude's response to the workflow + insert observations.

    The per-node ``estimated_tokens`` becomes the mean across the node's
    observations (recomputed after each merge).
    """
    now = datetime.now(timezone.utc)
    structurally_changed = False

    # Nodes (create-or-touch)
    for raw in response.get("added_or_updated_nodes", []) or []:
        try:
            node_id = str(raw["node_id"])
            if node_id not in workflow.nodes:
                workflow.nodes[node_id] = WorkflowNode(
                    node_id=node_id,
                    canonical_name=str(raw["canonical_name"]),
                    cage_label=CAGELabel(raw["cage_label"]),
                    system=str(raw["system"]),
                    data_object_pattern=str(raw["data_object_pattern"]),
                    estimated_tokens=0,  # mean will be computed from observations
                    expected_agent_steps=1,
                    observation_count=0,
                    confidence=float(raw.get("confidence", 0.5)),
                    rationale=str(raw.get("rationale", "")),
                )
                structurally_changed = True
                log.info("new node: %s — %s", node_id, raw["canonical_name"])
            else:
                # Refine confidence upward only
                n = workflow.nodes[node_id]
                n.confidence = max(n.confidence, float(raw.get("confidence", n.confidence)))
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed node: %s | %r", e, raw)

    # Edges
    existing_edge_keys = {(e.from_node, e.to_node) for e in workflow.edges}
    for raw in response.get("added_or_updated_edges", []) or []:
        try:
            f = str(raw["from_node"])
            t = str(raw["to_node"])
            if f not in workflow.nodes or t not in workflow.nodes:
                continue
            if (f, t) not in existing_edge_keys:
                workflow.edges.append(WorkflowEdge(from_node=f, to_node=t, observation_count=1))
                existing_edge_keys.add((f, t))
                structurally_changed = True
            else:
                for e in workflow.edges:
                    if (e.from_node, e.to_node) == (f, t):
                        e.observation_count += 1
                        break
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed edge: %s | %r", e, raw)

    # Observations
    observations: list[Observation] = []
    for raw in response.get("observations", []) or []:
        try:
            node_id = str(raw["node_id"])
            if node_id not in workflow.nodes:
                continue
            evidence = [
                eid
                for eid in raw.get("evidence_frame_ids", [])
                if eid in events_by_id
            ]
            if not evidence:
                continue
            obs = Observation(
                observation_id=f"obs_{uuid.uuid4().hex[:10]}",
                workflow_id=workflow.workflow_id,
                session_id=session.session_id,
                node_id=node_id,
                evidence_frame_ids=evidence,
                estimated_tokens=int(raw.get("estimated_tokens", 0)),
                expected_agent_steps=int(raw.get("expected_agent_steps", 1)),
                confidence=float(raw.get("confidence", 0.7)),
                observed_at=now,
            )
            workflow.nodes[node_id].observation_count += 1
            observations.append(obs)
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed observation: %s | %r", e, raw)

    # Persist new observations now so the mean computation sees them
    for obs in observations:
        store.insert_observation(obs)

    # Recompute mean estimates per touched node from ALL observations of that node
    touched_node_ids = {o.node_id for o in observations}
    for nid in touched_node_ids:
        all_obs = list(store.iter_observations(workflow_id=workflow.workflow_id))
        for_node = [o for o in all_obs if o.node_id == nid]
        if for_node:
            workflow.nodes[nid].estimated_tokens = int(
                sum(o.estimated_tokens for o in for_node) / len(for_node)
            )
            workflow.nodes[nid].expected_agent_steps = max(
                1, round(sum(o.expected_agent_steps for o in for_node) / len(for_node))
            )

    # Bookkeeping
    if session.session_id not in workflow.sessions_processed:
        workflow.sessions_processed.append(session.session_id)
    if structurally_changed:
        workflow.stable_observations = 0
    else:
        workflow.stable_observations += 1
    if workflow.stable_observations >= workflow.stability_threshold:
        workflow.is_complete = True
    workflow.last_updated_at = now

    return workflow, observations, structurally_changed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def update_with_session(
    store: Store,
    session: Session,
    *,
    force_workflow_name: str | None = None,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    dry_run: bool = False,
) -> tuple[Workflow, list[Observation]]:
    """Auto-route a session to a workflow (or create new) and update it."""
    events = list(store.iter_events(session.session_id))
    if not events:
        return _new_workflow("(empty)"), []
    events_by_id = {e.event_id: e for e in events}

    directory = _workflow_directory(store)
    batch = build_batch(events, store.screens_dir)
    log.info(
        "labeling session %s: directory has %d workflows, %d events in session, %d images selected",
        session.session_id,
        len(directory),
        len(events),
        len(batch.selected_frame_ids),
    )
    if dry_run:
        return _new_workflow("dry-run"), []

    try:
        import anthropic
    except ImportError as e:
        raise LabelerError("anthropic SDK not installed (`pip install anthropic`)") from e

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if client.api_key is None:
        raise LabelerError("ANTHROPIC_API_KEY not set")

    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=_build_messages(directory, batch),
    )
    text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    log.info(
        "claude returned %d output tokens (stop_reason=%s)",
        getattr(resp.usage, "output_tokens", -1),
        resp.stop_reason,
    )

    response = _extract_json(text)
    workflow = _select_or_create_workflow(store, response, force_name=force_workflow_name)
    if session.session_id in workflow.sessions_processed:
        log.info("session %s already in workflow %s; skipping", session.session_id, workflow.name)
        return workflow, []

    workflow, observations, changed = _merge_into_workflow(
        workflow, response, session, events_by_id, store
    )
    store.upsert_workflow(workflow)

    log.info(
        "workflow '%s' (%s): %d nodes, %d edges, %d new observations, "
        "structural change=%s, stable=%d/%d, complete=%s",
        workflow.name,
        workflow.workflow_id,
        len(workflow.nodes),
        len(workflow.edges),
        len(observations),
        changed,
        workflow.stable_observations,
        workflow.stability_threshold,
        workflow.is_complete,
    )
    return workflow, observations


def process_all_unprocessed_sessions(
    store: Store,
    *,
    force_workflow_name: str | None = None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> int:
    """Auto-route every session not yet recorded in any workflow."""
    seen_session_ids: set[str] = set()
    for wf in store.iter_workflows():
        seen_session_ids.update(wf.sessions_processed)
    n_processed = 0
    for session in store.iter_sessions():
        if session.session_id in seen_session_ids:
            continue
        update_with_session(
            store,
            session,
            force_workflow_name=force_workflow_name,
            model=model,
            dry_run=dry_run,
        )
        n_processed += 1
    return n_processed


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen_workflow.labeler")
    p.add_argument("--root", default="./local_data")
    p.add_argument(
        "--workflow",
        default=None,
        help="OPTIONAL force-name override. If unset, Claude decides which workflow.",
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    store = Store(Path(args.root))
    n = process_all_unprocessed_sessions(
        store,
        force_workflow_name=args.workflow,
        model=args.model,
        dry_run=args.dry_run,
    )
    store.close()
    print(f"processed {n} new session(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
