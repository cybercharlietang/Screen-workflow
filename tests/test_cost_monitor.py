"""Cost monitor — pricing math, rolling-hour window, threshold transitions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from screen_workflow.cost_monitor import (
    CostMonitor,
    CostState,
    UnknownModelError,
    price_for,
    usd_cost,
)
from screen_workflow.storage.db import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "data")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


# -- pricing -----------------------------------------------------------------


def test_pricing_opus_4_7():
    assert price_for("claude-opus-4-7") == (15.0, 75.0)


def test_pricing_sonnet_4_6():
    assert price_for("claude-sonnet-4-6") == (3.0, 15.0)


def test_pricing_haiku_4_5():
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)


def test_pricing_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        price_for("gpt-4o")


def test_usd_cost_opus():
    # 1M input on Opus = $15, 1M output = $75
    assert usd_cost("claude-opus-4-7", 1_000_000, 1_000_000) == pytest.approx(90.0)


def test_usd_cost_sonnet_realistic_session():
    # PoC L16 numbers: ~30K in, ~4K out on a session
    # Opus: 30/1M * 15 + 4/1M * 75 = 0.45 + 0.3 = $0.75 (close to L16's $0.80)
    # Sonnet: 30/1M * 3 + 4/1M * 15 = 0.09 + 0.06 = $0.15 (5× cheaper)
    cost = usd_cost("claude-sonnet-4-6", 30_000, 4_000)
    assert cost == pytest.approx(0.15, abs=0.01)


# -- monitor construction ----------------------------------------------------


def test_construction_requires_hard_ge_soft(store):
    with pytest.raises(ValueError):
        CostMonitor(
            store,
            soft_alert_usd_per_hour=30,
            hard_stop_usd_per_hour=10,
            total_spend_cap_usd=100,
        )


def test_construction_requires_positive_cap(store):
    with pytest.raises(ValueError):
        CostMonitor(
            store,
            soft_alert_usd_per_hour=10,
            hard_stop_usd_per_hour=30,
            total_spend_cap_usd=0,
        )


# -- recording + snapshots ---------------------------------------------------


def _mk(store: Store, run_start: datetime) -> CostMonitor:
    return CostMonitor(
        store,
        soft_alert_usd_per_hour=10,
        hard_stop_usd_per_hour=30,
        total_spend_cap_usd=100,
        run_started_at=run_start,
    )


def test_empty_snapshot_is_ok(store, now):
    cm = _mk(store, now)
    snap = cm.snapshot(now=now)
    assert snap.state == CostState.OK
    assert snap.usd_last_hour == 0
    assert snap.usd_total_run == 0
    assert snap.n_calls == 0


def test_record_persists_and_updates_snapshot(store, now):
    cm = _mk(store, now)
    snap = cm.record(
        model="claude-sonnet-4-6",
        input_tokens=30_000,
        output_tokens=4_000,
        ts=now,
    )
    assert snap.n_calls == 1
    assert snap.usd_last_hour == pytest.approx(0.15, abs=0.01)
    assert snap.state == CostState.OK


def test_soft_alert_fires_at_threshold(store, now):
    cm = _mk(store, now)
    # Hit ~$10 — needs many sonnet calls or a big opus one.
    # Opus: $15/M input, $75/M output. 600K input + 1K output = $9 + $0.075 = ~$9.08 (still OK)
    # Push to $10+ with another small call.
    cm.record(model="claude-opus-4-7", input_tokens=600_000, output_tokens=10_000, ts=now)
    snap = cm.record(
        model="claude-opus-4-7",
        input_tokens=100_000,
        output_tokens=10_000,
        ts=now,
    )
    assert snap.usd_last_hour >= 10
    assert snap.usd_last_hour < 30
    assert snap.state == CostState.SOFT_ALERT
    assert "soft alert" in (snap.reason or "")


def test_hard_stop_fires_when_hourly_exceeds(store, now):
    cm = _mk(store, now)
    # $30 in an hour — easy on Opus.
    snap = cm.record(
        model="claude-opus-4-7",
        input_tokens=2_000_000,
        output_tokens=10_000,
        ts=now,
    )
    assert snap.usd_last_hour >= 30
    assert snap.state == CostState.HARD_STOP
    assert "hard stop" in (snap.reason or "")


def test_hard_stop_fires_when_total_cap_exceeds_even_if_hourly_low(store, now):
    """Spread spend over many hours, never breach hourly, breach total cap."""
    cm = _mk(store, now)
    # Five $25 calls spread out 90 minutes apart — each is under hard hourly
    # but total = $125 > $100 cap.
    for i in range(5):
        cm.record(
            model="claude-opus-4-7",
            input_tokens=1_500_000,
            output_tokens=10_000,
            ts=now + timedelta(minutes=90 * i),
        )
    snap = cm.snapshot(now=now + timedelta(minutes=90 * 5))
    assert snap.usd_total_run >= 100
    assert snap.state == CostState.HARD_STOP
    assert "total run spend" in (snap.reason or "")


def test_rolling_hour_window_excludes_old_calls(store, now):
    cm = _mk(store, now - timedelta(hours=3))
    # Record an expensive call 2 hours ago — should be in total but NOT hourly.
    cm.record(
        model="claude-opus-4-7",
        input_tokens=2_000_000,
        output_tokens=10_000,
        ts=now - timedelta(hours=2),
    )
    snap = cm.snapshot(now=now)
    assert snap.usd_total_run >= 30
    assert snap.usd_last_hour == 0
    # Hourly is OK, but the total cap may have triggered. With $30 < $100 cap, still OK.
    assert snap.state == CostState.OK


def test_should_pause_only_on_hard_stop(store, now):
    cm = _mk(store, now)
    assert not cm.should_pause(now=now)
    # Soft alert: should NOT pause.
    cm.record(model="claude-opus-4-7", input_tokens=700_000, output_tokens=10_000, ts=now)
    snap = cm.snapshot(now=now)
    assert snap.state == CostState.SOFT_ALERT
    assert not cm.should_pause(now=now)
    # Hard stop: should pause.
    cm.record(model="claude-opus-4-7", input_tokens=1_500_000, output_tokens=10_000, ts=now)
    assert cm.should_pause(now=now)


def test_recovery_after_hour_passes(store, now):
    """Spend a lot in hour 0, wait an hour, hourly window should drop."""
    cm = _mk(store, now)
    cm.record(
        model="claude-opus-4-7",
        input_tokens=2_000_000,
        output_tokens=10_000,
        ts=now,
    )
    assert cm.snapshot(now=now).state == CostState.HARD_STOP
    # 65 min later: the old call should have aged out.
    later_snap = cm.snapshot(now=now + timedelta(minutes=65))
    assert later_snap.usd_last_hour == 0
    # Total cap unchanged though — at ~$30.75, still under $100.
    assert later_snap.state == CostState.OK


def test_snapshot_to_dict_shape(store, now):
    cm = _mk(store, now)
    cm.record(model="claude-sonnet-4-6", input_tokens=10_000, output_tokens=2_000, ts=now)
    d = cm.snapshot(now=now).to_dict()
    assert set(d) == {
        "state",
        "reason",
        "usd_last_hour",
        "usd_total_run",
        "soft_alert_usd_per_hour",
        "hard_stop_usd_per_hour",
        "total_spend_cap_usd",
        "n_calls",
    }
    assert d["state"] == "ok"
    assert d["n_calls"] == 1
