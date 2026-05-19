"""Per-workflow labeler: each Claude call ingests one session's batch and
incrementally updates the workflow graph.

Flow per session:
    1. Load (or create) the named Workflow.
    2. Build a multimodal batch from the session's events.
    3. Send Claude: current Workflow JSON + new batch + instructions.
    4. Parse response: list of Observations + (possibly) new/updated nodes/edges.
    5. Merge into the Workflow, persist; emit Observation rows.
    6. Detect stability (no new nodes/edges added) and mark complete after
       ``stability_threshold`` consecutive stable updates.
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


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are a procurement-workflow analyst at Fragment. Your job is to maintain
a **workflow graph** — a model of one specific procurement workflow as
observed across many employee sessions.

You will receive on each call:
  1. The CURRENT workflow graph (JSON with nodes + edges + observation counts).
  2. A NEW session's event log + screenshots.

Your job per call: identify the cognitive actions in the new session and
UPDATE the workflow graph by:

  A) Mapping observed actions to EXISTING nodes when they match (an action
     matches a node if the cognitive intent is the same — same CAGE label,
     same system, same generic data_object_pattern). Increment that node's
     ``observation_count``.
  B) Creating NEW nodes only when you genuinely observe an action that does
     not match any existing node.
  C) Adding/updating edges for the transitions observed in this session.

CAGE taxonomy:
  C — Capture: ingesting data (reading email, opening record, downloading PDF)
  A — Analyze: interpreting / comparing / deciding
  G — Generate: producing new content (drafts, comments, reports)
  E — Extract: pulling structured fields from unstructured sources

CRUCIAL guidelines:

1. **Repeated actions collapse to ONE node.** If "Open PO email in Outlook"
   appears 5 times across sessions, it is ONE node with observation_count=5,
   not 5 nodes.
2. **data_object_pattern is generic.** Use "PO #<num>", not "PO #12345".
   Use "<vendor>", not "Acme Corp".
3. **Merge into cognitive units.** Five clicks while filling one form is
   ONE node ("filled out vendor form"), not five.
4. **Ignore noise.** Slack pings, lunch breaks, unrelated browsing — omit.
5. **estimated_tokens** = input + output of ONE LLM call an agent would
   make to perform this action. Typical ranges:
     - Capture / Extract: 500–3,000
     - Analyze: 2,000–8,000
     - Generate: 1,500–10,000
6. **expected_agent_steps** = how many LLM calls an agent needs:
     - 1 for read+act (open and read)
     - 2–3 for compare+decide
     - 4+ only for genuinely multi-step actions
7. **confidence 0.0–1.0**: how sure you are the screenshots support the
   identification. Be honest.
8. **node_id** for new nodes: short kebab-case from canonical_name
   (e.g. "open-po-email").

Return ONLY JSON of this shape:

{
  "added_or_updated_nodes": [
    {
      "node_id": "...",
      "canonical_name": "...",
      "cage_label": "C|A|G|E",
      "system": "...",
      "data_object_pattern": "...",
      "estimated_tokens": 2500,
      "expected_agent_steps": 1,
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
      "confidence": 0.85
    }
  ]
}

- ``is_new=true`` for nodes you are adding for the first time.
- ``is_new=false`` for existing nodes you are merely updating
  (we will increment their observation_count regardless).
- ``edges`` is just the transitions you observed in THIS session — we
  will increment counts on our side.
- ``observations`` is one entry per cognitive action seen in this session
  (so a session typically yields 3–15 observations).
- NO prose outside the JSON.
"""


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def _compact_workflow_for_prompt(workflow: Workflow) -> str:
    """JSON representation passed to Claude — strip fields it doesn't need to
    avoid wasting tokens."""
    return json.dumps(
        {
            "workflow_id": workflow.workflow_id,
            "name": workflow.name,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "canonical_name": n.canonical_name,
                    "cage_label": n.cage_label.value,
                    "system": n.system,
                    "data_object_pattern": n.data_object_pattern,
                    "estimated_tokens": n.estimated_tokens,
                    "expected_agent_steps": n.expected_agent_steps,
                    "observation_count": n.observation_count,
                }
                for n in workflow.nodes.values()
            ],
            "edges": [
                {"from": e.from_node, "to": e.to_node, "count": e.observation_count}
                for e in workflow.edges
            ],
            "sessions_processed": len(workflow.sessions_processed),
            "stable_observations": workflow.stable_observations,
        },
        indent=2,
    )


def _build_messages(workflow: Workflow, batch) -> list[dict]:
    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"WORKFLOW NAME: {workflow.name}\n\n"
                f"CURRENT WORKFLOW STATE:\n```json\n"
                f"{_compact_workflow_for_prompt(workflow)}\n```\n\n"
                "NEW SESSION EVENT LOG (tab-separated, one row per kept frame):\n\n"
                f"{batch.event_log_text}\n\n"
                "Screenshots follow, in chronological order; each is preceded "
                "by a small text marker with its frame_id and ts. Cite those "
                "ids in evidence_frame_ids."
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


def _merge_into_workflow(
    workflow: Workflow,
    response: dict,
    session: Session,
    events_by_id: dict[str, Event],
) -> tuple[Workflow, list[Observation], bool]:
    """Apply Claude's response to the workflow. Return (updated_workflow,
    observations, structurally_changed)."""
    now = datetime.now(timezone.utc)
    structurally_changed = False

    # Nodes
    for raw in response.get("added_or_updated_nodes", []) or []:
        try:
            node_id = str(raw["node_id"])
            existing = workflow.nodes.get(node_id)
            if existing is None:
                workflow.nodes[node_id] = WorkflowNode(
                    node_id=node_id,
                    canonical_name=str(raw["canonical_name"]),
                    cage_label=CAGELabel(raw["cage_label"]),
                    system=str(raw["system"]),
                    data_object_pattern=str(raw["data_object_pattern"]),
                    estimated_tokens=int(raw.get("estimated_tokens", 0)),
                    expected_agent_steps=int(raw.get("expected_agent_steps", 1)),
                    observation_count=1,
                    confidence=float(raw.get("confidence", 0.5)),
                    rationale=str(raw.get("rationale", "")),
                )
                structurally_changed = True
                log.info("new node: %s — %s", node_id, raw["canonical_name"])
            else:
                # Update — Claude may refine token estimates as it sees more samples
                existing.estimated_tokens = max(
                    existing.estimated_tokens, int(raw.get("estimated_tokens", existing.estimated_tokens))
                )
                existing.confidence = max(
                    existing.confidence, float(raw.get("confidence", existing.confidence))
                )
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed node: %s | %r", e, raw)

    # Edges (added if new pair)
    existing_edge_keys = {(e.from_node, e.to_node) for e in workflow.edges}
    for raw in response.get("added_or_updated_edges", []) or []:
        try:
            f = str(raw["from_node"])
            t = str(raw["to_node"])
            if f not in workflow.nodes or t not in workflow.nodes:
                continue
            key = (f, t)
            if key not in existing_edge_keys:
                workflow.edges.append(WorkflowEdge(from_node=f, to_node=t, observation_count=1))
                existing_edge_keys.add(key)
                structurally_changed = True
            else:
                for e in workflow.edges:
                    if (e.from_node, e.to_node) == key:
                        e.observation_count += 1
                        break
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed edge: %s | %r", e, raw)

    # Observations — and increment node observation_count
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
            workflow.nodes[node_id].observation_count += 1
            observations.append(
                Observation(
                    observation_id=f"obs_{uuid.uuid4().hex[:10]}",
                    workflow_id=workflow.workflow_id,
                    session_id=session.session_id,
                    node_id=node_id,
                    evidence_frame_ids=evidence,
                    confidence=float(raw.get("confidence", 0.7)),
                    observed_at=now,
                )
            )
        except (KeyError, ValueError, ValidationError) as e:
            log.warning("skipping malformed observation: %s | %r", e, raw)

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


def update_workflow_with_session(
    store: Store,
    workflow_name: str,
    session: Session,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    dry_run: bool = False,
) -> tuple[Workflow, list[Observation]]:
    """Process one session against the named workflow.

    Returns ``(updated_workflow, new_observations)``.
    """
    workflow = store.find_workflow_by_name(workflow_name) or _new_workflow(workflow_name)
    if session.session_id in workflow.sessions_processed:
        log.info(
            "session %s already processed for workflow %s; skipping",
            session.session_id,
            workflow_name,
        )
        return workflow, []
    if workflow.is_complete:
        log.info("workflow %s is marked complete; skipping", workflow_name)
        return workflow, []

    events = list(store.iter_events(session.session_id))
    if not events:
        return workflow, []
    events_by_id = {e.event_id: e for e in events}

    batch = build_batch(events, store.screens_dir)
    log.info(
        "updating workflow '%s' with session %s: %d events, %d images selected",
        workflow_name,
        session.session_id,
        len(events),
        len(batch.selected_frame_ids),
    )
    if dry_run:
        return workflow, []

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
        messages=_build_messages(workflow, batch),
    )
    text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    log.info(
        "claude returned %d output tokens (stop_reason=%s)",
        getattr(resp.usage, "output_tokens", -1),
        resp.stop_reason,
    )

    response = _extract_json(text)
    workflow, observations, changed = _merge_into_workflow(
        workflow, response, session, events_by_id
    )
    store.upsert_workflow(workflow)
    for obs in observations:
        store.insert_observation(obs)

    log.info(
        "workflow '%s': %d nodes, %d edges, %d observations this call, "
        "structural change=%s, stable=%d/%d, complete=%s",
        workflow.name,
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
    workflow_name: str,
    *,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> int:
    """Update the named workflow with every session not yet seen by it."""
    workflow = store.find_workflow_by_name(workflow_name) or _new_workflow(workflow_name)
    already = set(workflow.sessions_processed)
    n_processed = 0
    for session in store.iter_sessions():
        if session.session_id in already:
            continue
        update_workflow_with_session(
            store, workflow_name, session, model=model, dry_run=dry_run
        )
        n_processed += 1
    return n_processed


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen_workflow.labeler")
    p.add_argument("--root", default="./local_data")
    p.add_argument(
        "--workflow",
        required=True,
        help="Workflow name to update (PoC: caller picks). Created if missing.",
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
        store, args.workflow, model=args.model, dry_run=args.dry_run
    )
    store.close()
    print(f"processed {n} new sessions into workflow '{args.workflow}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
