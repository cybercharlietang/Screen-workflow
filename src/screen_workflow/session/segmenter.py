"""Session segmenter — PoC heuristic.

Reads all unassigned events from the store and groups them into Sessions.
A session closes when any of:

- wall-clock 30 min has elapsed since the session's first event,
- gap between consecutive events exceeds ``idle_gap_seconds`` (default 120),
- session contains ``max_events`` events (safety cap so a single session
  doesn't blow the Claude token budget).

PoC scope: no context-shift detection. That's a refinement once we know
how the labeler handles long sessions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from screen_workflow.schemas import Event, Session, SessionCloseReason
from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)


@dataclass
class SegmenterConfig:
    duration_cap: timedelta = timedelta(minutes=30)
    idle_gap_seconds: float = 120.0
    max_events: int = 300


def _close_reason(
    cfg: SegmenterConfig,
    start: datetime,
    last: datetime,
    nxt: datetime | None,
    n_events: int,
) -> SessionCloseReason | None:
    """Return a close reason if the session should end now, else None."""
    if n_events >= cfg.max_events:
        return SessionCloseReason.CONTEXT_SHIFT  # reuse this enum for "cap hit"
    if (last - start) >= cfg.duration_cap:
        return SessionCloseReason.DURATION_CAP
    if nxt is not None and (nxt - last).total_seconds() > cfg.idle_gap_seconds:
        return SessionCloseReason.IDLE_GAP
    return None


def segment(events: list[Event], cfg: SegmenterConfig | None = None) -> list[Session]:
    """Group a list of (chronologically sorted) events into sessions.

    Events are not mutated; the caller persists them with the new ``session_id``
    via ``Store.assign_session`` or ``Store.insert_session``.
    """
    cfg = cfg or SegmenterConfig()
    if not events:
        return []

    sessions: list[Session] = []
    bucket_event_ids: list[str] = []
    bucket_start = events[0].ts
    bucket_last = events[0].ts

    for i, e in enumerate(events):
        nxt = events[i + 1].ts if i + 1 < len(events) else None
        bucket_event_ids.append(e.event_id)
        bucket_last = e.ts

        reason = _close_reason(
            cfg, bucket_start, bucket_last, nxt, len(bucket_event_ids)
        )
        if reason is not None or nxt is None:
            close_reason = reason or SessionCloseReason.IDLE_GAP
            # If we're closing because there's no next event, but no other
            # reason applies, treat it as IDLE_GAP for now (end of stream).
            sessions.append(
                Session(
                    session_id=f"sess_{uuid.uuid4().hex[:10]}",
                    start_ts=bucket_start,
                    end_ts=bucket_last,
                    close_reason=close_reason,
                    event_ids=list(bucket_event_ids),
                )
            )
            bucket_event_ids = []
            if nxt is not None:
                bucket_start = nxt
                bucket_last = nxt

    return sessions


def segment_and_persist(store: Store, cfg: SegmenterConfig | None = None) -> list[Session]:
    """Read all unassigned events from ``store``, segment, persist sessions."""
    unassigned = [e for e in store.iter_events() if e.session_id is None]
    if not unassigned:
        return []
    sessions = segment(unassigned, cfg)
    for s in sessions:
        store.insert_session(s)
    log.info(
        "segmented %d events into %d session(s)",
        len(unassigned),
        len(sessions),
    )
    return sessions


# ---------------------------------------------------------------------------
# Streaming segmenter — for live mode
# ---------------------------------------------------------------------------


def segment_incremental(
    events: list[Event],
    cfg: SegmenterConfig,
    now: datetime,
    *,
    force: bool = False,
) -> list[Session]:
    """Segment unassigned events, returning only sessions that have *definitely*
    closed.

    Unlike :func:`segment`, the trailing bucket is left OPEN — its events are
    excluded from the result — unless one of:

    - a hard close reason fires (duration cap, max events), or
    - a confirmed idle gap to a later event in the stream, or
    - wall-clock idle: ``now - last_event > idle_gap_seconds``, or
    - ``force=True`` (shutdown flush).

    Leaving the tail open is what makes the streaming segmenter idempotent: a
    bucket that could still grow stays unassigned and is re-evaluated on the
    next tick. ``now`` must be timezone-aware to match captured event
    timestamps.
    """
    if not events:
        return []

    sessions: list[Session] = []
    bucket_ids: list[str] = []
    bucket_start = events[0].ts
    bucket_last = events[0].ts

    for i, e in enumerate(events):
        nxt = events[i + 1].ts if i + 1 < len(events) else None
        bucket_ids.append(e.event_id)
        bucket_last = e.ts

        reason = _close_reason(cfg, bucket_start, bucket_last, nxt, len(bucket_ids))

        if reason is None and nxt is None:
            # Trailing bucket: no later event to confirm an idle gap. Close it
            # only if it has gone idle in wall-clock time, or we are flushing.
            idle = (now - bucket_last).total_seconds() > cfg.idle_gap_seconds
            if force or idle:
                reason = SessionCloseReason.IDLE_GAP
            else:
                break  # leave the trailing bucket open for the next tick

        if reason is not None:
            sessions.append(
                Session(
                    session_id=f"sess_{uuid.uuid4().hex[:10]}",
                    start_ts=bucket_start,
                    end_ts=bucket_last,
                    close_reason=reason,
                    event_ids=list(bucket_ids),
                )
            )
            bucket_ids = []
            if nxt is not None:
                bucket_start = nxt
                bucket_last = nxt

    return sessions


def segment_and_persist_incremental(
    store: Store,
    cfg: SegmenterConfig | None = None,
    *,
    force: bool = False,
) -> list[Session]:
    """One streaming-segmenter tick: segment unassigned events and persist only
    the sessions that have definitely closed.

    Idempotent — safe to call on a schedule against a growing event stream.
    Pass ``force=True`` on shutdown to also close the open trailing bucket.
    """
    cfg = cfg or SegmenterConfig()
    unassigned = [e for e in store.iter_events() if e.session_id is None]
    if not unassigned:
        return []
    sessions = segment_incremental(
        unassigned, cfg, datetime.now(timezone.utc), force=force
    )
    for s in sessions:
        store.insert_session(s)
    if sessions:
        log.info(
            "segmenter: %d unassigned event(s) -> closed %d session(s)%s",
            len(unassigned),
            len(sessions),
            " (forced flush)" if force else "",
        )
    return sessions


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="screen_workflow.session.segmenter")
    p.add_argument("--root", default="./local_data")
    p.add_argument("--idle-gap-seconds", type=float, default=120.0)
    p.add_argument("--duration-cap-minutes", type=float, default=30.0)
    p.add_argument("--max-events", type=int, default=300)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = SegmenterConfig(
        duration_cap=timedelta(minutes=args.duration_cap_minutes),
        idle_gap_seconds=args.idle_gap_seconds,
        max_events=args.max_events,
    )
    store = Store(Path(args.root))
    sessions = segment_and_persist(store, cfg)
    store.close()
    for s in sessions:
        print(
            f"session {s.session_id}  events={len(s.event_ids):4d}  "
            f"start={s.start_ts.isoformat(timespec='seconds')}  "
            f"end={s.end_ts.isoformat(timespec='seconds')}  reason={s.close_reason.value}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
