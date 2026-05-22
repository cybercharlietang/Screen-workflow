"""Cost monitor for Anthropic API calls made by the labeler.

Tracks per-call token usage in ``events.db``, computes rolling hourly spend
and total run spend, and exposes a soft/hard-threshold state machine so the
labeler thread can pause itself when the per-hour or per-run budget is hit.

Wired into the labeler in PR 2; live.py owns the lifetime in PR 3. PR 1
(this file) is the data + decision layer in isolation, ready to be called.

Pricing as of 2026-05-21. Update the table below when Anthropic publishes
new model prices.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from screen_workflow.storage.db import Store

log = logging.getLogger(__name__)


# Per-million-token prices in USD (input, output). Keys are substring matches
# against the requested model name (lowercased) — first match wins, so list
# more specific keys first. Source: claude-api skill model table, dated
# 2026-04-29. NOTE: LESSONS.md L16 cites Opus at $15/$75 — that is stale
# (old Claude 3 Opus pricing); Opus 4.x is $5/$25.
_PRICING: list[tuple[str, float, float]] = [
    ("opus-4-7", 5.0, 25.0),
    ("opus-4", 5.0, 25.0),
    ("sonnet-4-6", 3.0, 15.0),
    ("sonnet-4-5", 3.0, 15.0),
    ("sonnet-4", 3.0, 15.0),
    ("haiku-4-5", 1.0, 5.0),
    ("haiku-4", 1.0, 5.0),
]


class UnknownModelError(ValueError):
    """Raised when no pricing is configured for the model name.

    We refuse to price-at-zero by default — silent zero-cost would defeat
    the entire monitor. Caller can catch this and fall back to a default
    rate if they explicitly choose to."""


def price_for(model: str) -> tuple[float, float]:
    """Return (usd_per_million_input, usd_per_million_output) for a model."""
    key = model.lower()
    for needle, in_price, out_price in _PRICING:
        if needle in key:
            return in_price, out_price
    raise UnknownModelError(f"no pricing configured for model={model!r}")


def usd_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = price_for(model)
    return (input_tokens / 1_000_000.0) * in_price + (
        output_tokens / 1_000_000.0
    ) * out_price


class CostState(str, Enum):
    OK = "ok"
    SOFT_ALERT = "soft_alert"
    HARD_STOP = "hard_stop"


@dataclass
class CostSnapshot:
    state: CostState
    reason: str | None
    usd_last_hour: float
    usd_total_run: float
    soft_alert_usd_per_hour: float
    hard_stop_usd_per_hour: float
    total_spend_cap_usd: float
    n_calls: int

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "usd_last_hour": round(self.usd_last_hour, 4),
            "usd_total_run": round(self.usd_total_run, 4),
            "soft_alert_usd_per_hour": self.soft_alert_usd_per_hour,
            "hard_stop_usd_per_hour": self.hard_stop_usd_per_hour,
            "total_spend_cap_usd": self.total_spend_cap_usd,
            "n_calls": self.n_calls,
        }


class CostMonitor:
    """Persist per-API-call cost into ``events.db`` and surface state.

    The monitor is process-shared via the Store. Two pipeline runs sharing
    the same root would share the spend bucket — that's deliberate: a fresh
    run gets a fresh root.

    ``run_started_at`` is used to compute *total run spend*; the rolling
    hour window is wall-clock (last 3600 seconds), independent of run start.
    """

    def __init__(
        self,
        store: Store,
        *,
        soft_alert_usd_per_hour: float,
        hard_stop_usd_per_hour: float,
        total_spend_cap_usd: float,
        run_started_at: datetime | None = None,
    ) -> None:
        if hard_stop_usd_per_hour < soft_alert_usd_per_hour:
            raise ValueError(
                "hard_stop_usd_per_hour must be >= soft_alert_usd_per_hour"
            )
        if total_spend_cap_usd <= 0:
            raise ValueError("total_spend_cap_usd must be positive")
        self._store = store
        self._soft = soft_alert_usd_per_hour
        self._hard = hard_stop_usd_per_hour
        self._cap = total_spend_cap_usd
        self._run_started_at = run_started_at or datetime.now(timezone.utc)

    # -- recording ---------------------------------------------------------

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str | None = None,
        ts: datetime | None = None,
    ) -> CostSnapshot:
        """Persist one API call and return the post-write snapshot."""
        ts = ts or datetime.now(timezone.utc)
        cost = usd_cost(model, input_tokens, output_tokens)
        self._store.insert_api_call(
            call_id=f"call_{uuid.uuid4().hex[:12]}",
            ts=ts,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd_cost=cost,
            session_id=session_id,
        )
        snap = self.snapshot(now=ts)
        if snap.state == CostState.HARD_STOP:
            log.warning("cost monitor HARD_STOP: %s", snap.reason)
        elif snap.state == CostState.SOFT_ALERT:
            log.warning("cost monitor SOFT_ALERT: %s", snap.reason)
        return snap

    # -- inspection --------------------------------------------------------

    def snapshot(self, *, now: datetime | None = None) -> CostSnapshot:
        now = now or datetime.now(timezone.utc)
        hour_ago = now - timedelta(hours=1)
        usd_hour = self._store.sum_usd_since(hour_ago)
        # Total spend is bounded to this run's window.
        usd_run = self._store.sum_usd_since(self._run_started_at)
        n = sum(1 for _ in self._store.iter_api_calls())

        state = CostState.OK
        reason: str | None = None
        # Hard stop has priority over soft alert.
        if usd_run >= self._cap:
            state = CostState.HARD_STOP
            reason = (
                f"total run spend ${usd_run:.2f} >= cap ${self._cap:.2f}"
            )
        elif usd_hour >= self._hard:
            state = CostState.HARD_STOP
            reason = (
                f"hourly spend ${usd_hour:.2f} >= hard stop "
                f"${self._hard:.2f}/hr"
            )
        elif usd_hour >= self._soft:
            state = CostState.SOFT_ALERT
            reason = (
                f"hourly spend ${usd_hour:.2f} >= soft alert "
                f"${self._soft:.2f}/hr"
            )

        return CostSnapshot(
            state=state,
            reason=reason,
            usd_last_hour=usd_hour,
            usd_total_run=usd_run,
            soft_alert_usd_per_hour=self._soft,
            hard_stop_usd_per_hour=self._hard,
            total_spend_cap_usd=self._cap,
            n_calls=n,
        )

    def should_pause(self, *, now: datetime | None = None) -> bool:
        """True iff a new API call must NOT be issued right now."""
        return self.snapshot(now=now).state == CostState.HARD_STOP
