"""Call Claude with a session Batch; parse + validate the response into Labels."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from screen_workflow.labeler.batch import Batch, build_batch
from screen_workflow.schemas import CAGELabel, Event, Label, Session
from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-7"
MAX_TOKENS = 8000


class LabelerError(RuntimeError):
    pass


def _build_messages(batch: Batch) -> list[dict]:
    """Return the ``messages`` list for the Anthropic SDK."""
    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                "Here is the event log for one session "
                "(tab-separated, one row per kept frame):\n\n"
                f"{batch.event_log_text}\n\n"
                "The following images are the screenshots, in chronological order, "
                "each preceded by a small text marker giving its frame_id and ts. "
                "Use those ids when citing evidence."
            ),
        },
        *batch.images,
        {
            "type": "text",
            "text": "Now return the JSON object described in the system prompt. JSON only.",
        },
    ]
    return [{"role": "user", "content": user_content}]


def _parse_response_text(text: str) -> list[dict]:
    """Strip code fences, extract the JSON, return ``actions`` list."""
    # Strip triple-backtick fences if present.
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LabelerError(f"no JSON object found in response: {text[:200]!r}")
    raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LabelerError(f"response was not valid JSON: {e}") from e
    if not isinstance(data, dict) or "actions" not in data:
        raise LabelerError("JSON missing 'actions' key")
    actions = data["actions"]
    if not isinstance(actions, list):
        raise LabelerError("'actions' is not a list")
    return actions


def _action_to_label(
    action: dict,
    session: Session,
    events_by_id: dict[str, Event],
) -> Label | None:
    """Convert one of Claude's action dicts into a validated Label."""
    try:
        evidence = action.get("evidence_frame_ids") or []
        if not evidence:
            # Synthesize from start/end if Claude omitted it
            start_id = action.get("start_frame_id")
            end_id = action.get("end_frame_id")
            if start_id:
                evidence.append(start_id)
            if end_id and end_id != start_id:
                evidence.append(end_id)
        evidence = [eid for eid in evidence if eid in events_by_id]
        if not evidence:
            log.warning("dropping action with no valid evidence frame_ids: %s", action)
            return None

        start_event = events_by_id[evidence[0]]
        end_event = events_by_id[evidence[-1]]
        if end_event.ts < start_event.ts:
            start_event, end_event = end_event, start_event

        return Label(
            action_id=action.get("action_id") or f"act_{uuid.uuid4().hex[:8]}",
            session_id=session.session_id,
            cage_label=CAGELabel(action["cage_label"]),
            system=str(action.get("system", "unknown")),
            data_object=str(action.get("data_object", "(unknown)")),
            estimated_tokens=int(action.get("estimated_tokens", 0)),
            expected_agent_steps=int(action.get("expected_agent_steps", 1)),
            start_ts=start_event.ts,
            end_ts=end_event.ts,
            evidence_frame_ids=evidence,
            confidence=float(action.get("confidence", 0.5)),
            rationale=str(action.get("rationale", "")),
        )
    except (KeyError, ValueError, ValidationError) as e:
        log.warning("could not coerce action into Label: %s | action=%s", e, action)
        return None


def label_session(
    session: Session,
    events: list[Event],
    screens_root: Path,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    dry_run: bool = False,
) -> tuple[list[Label], Batch]:
    """Send one session to Claude, return validated Labels + the Batch used."""
    batch = build_batch(events, screens_root)
    log.info(
        "labeling session %s: %d events, %d images selected (%d dropped), ~%d input tokens",
        session.session_id,
        len(events),
        len(batch.selected_frame_ids),
        len(batch.dropped_frame_ids),
        batch.approx_input_tokens,
    )
    if dry_run:
        return [], batch

    try:
        import anthropic
    except ImportError as e:
        raise LabelerError("anthropic SDK not installed (`pip install anthropic`)") from e

    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if client.api_key is None:
        raise LabelerError("ANTHROPIC_API_KEY not set")

    messages = _build_messages(batch)
    log.debug("calling %s with %d content blocks", model, len(messages[0]["content"]))

    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=batch.system,
        messages=messages,
    )
    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()
    log.info(
        "claude returned %d output tokens (stop_reason=%s)",
        getattr(resp.usage, "output_tokens", -1),
        resp.stop_reason,
    )

    actions = _parse_response_text(full_text)
    events_by_id = {e.event_id: e for e in events}
    labels: list[Label] = []
    for a in actions:
        lbl = _action_to_label(a, session, events_by_id)
        if lbl is not None:
            labels.append(lbl)
    log.info("session %s yielded %d valid labels", session.session_id, len(labels))
    return labels, batch


def label_all_unlabeled(
    store: Store,
    *,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> int:
    """Label every session that has zero labels yet. Returns total labels written."""
    labeled_session_ids = {l.session_id for l in store.iter_labels()}
    written = 0
    for session in store.iter_sessions():
        if session.session_id in labeled_session_ids:
            continue
        events = list(store.iter_events(session.session_id))
        if not events:
            continue
        labels, _ = label_session(
            session, events, store.screens_dir, model=model, dry_run=dry_run
        )
        for lbl in labels:
            store.insert_label(lbl)
            written += 1
    return written


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen_workflow.labeler")
    p.add_argument("--root", default="./local_data")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true", help="build batch but skip API call")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    store = Store(Path(args.root))
    n = label_all_unlabeled(store, model=args.model, dry_run=args.dry_run)
    store.close()
    print(f"wrote {n} labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
