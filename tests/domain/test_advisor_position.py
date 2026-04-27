"""Position Advisor decision-table tests.

Verifies priority order: earnings > 80% profit > delta crit > 50%+short DTE >
delta warn+short DTE > hold.
"""
from datetime import date, timedelta

import pytest

from options_tool.domain.advisor_position import (
    SHORT_DTE_THRESHOLD,
    advise_position,
)
from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.settings import AlertsConfig

TODAY = date(2026, 4, 19)


def cfg(**overrides) -> AlertsConfig:
    base = dict(
        profit_take_50=True,
        profit_take_80=True,
        delta_warning=0.40,
        delta_critical=0.50,
        earnings_conflict_dte=7,
    )
    base.update(overrides)
    return AlertsConfig(**base)


def pos(
    *,
    expiry_dte: int = 30,
    open_px: float | None = 1.00,
    mark: float | None = 0.50,
    delta: float | None = -0.20,
    right: str = "P",
) -> ShortPositionSnapshot:
    return ShortPositionSnapshot(
        symbol="ABC",
        right=right,
        strike=50.0,
        expiry=TODAY + timedelta(days=expiry_dte),
        qty=-1,
        avg_open_price=open_px,
        current_mark=mark,
        current_delta=delta,
    )


class TestPriority:
    def test_earnings_conflict_beats_profit_80(self):
        # 90% decay would normally CLOSE, but earnings inside DTE flips to ROLL.
        p = pos(expiry_dte=30, open_px=1.00, mark=0.10)
        earnings = [TODAY + timedelta(days=10)]
        a = advise_position(p, earnings, TODAY, cfg())
        assert a.label == "ROLL"
        assert "财报" in a.reason

    def test_profit_80_beats_delta_crit(self):
        # 90% decay AND delta=0.60 — profit 80 wins by priority.
        p = pos(open_px=1.00, mark=0.10, delta=-0.60)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "CLOSE"
        assert "衰减" in a.reason

    def test_delta_crit_alone(self):
        # No decay (mark equals open), delta breaches crit.
        p = pos(open_px=1.00, mark=1.00, delta=-0.55)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "ROLL"
        assert a.severity == "critical"


class TestProfitTake:
    def test_50pct_short_dte_closes(self):
        p = pos(expiry_dte=5, open_px=1.00, mark=0.50, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "CLOSE"

    def test_50pct_long_dte_holds(self):
        # 50% profit but 30 DTE remaining — let it ride.
        p = pos(expiry_dte=30, open_px=1.00, mark=0.50, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"

    def test_50pct_disabled_holds(self):
        p = pos(expiry_dte=3, open_px=1.00, mark=0.50, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg(profit_take_50=False))
        assert a.label == "HOLD"

    def test_80pct_long_dte_still_closes(self):
        # 80% threshold isn't gated by DTE — big win is big win.
        p = pos(expiry_dte=40, open_px=1.00, mark=0.10)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "CLOSE"


class TestDelta:
    def test_warn_short_dte_rolls(self):
        p = pos(expiry_dte=5, open_px=1.00, mark=0.95, delta=-0.42)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "ROLL"
        assert a.severity == "warn"

    def test_warn_long_dte_holds(self):
        p = pos(expiry_dte=30, open_px=1.00, mark=0.95, delta=-0.42)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"

    def test_critical_long_dte_still_rolls(self):
        # Critical Delta isn't DTE-gated.
        p = pos(expiry_dte=45, open_px=1.00, mark=0.95, delta=-0.55)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "ROLL"

    def test_call_positive_delta_uses_abs(self):
        # Calls have positive delta; we compare |delta|.
        p = pos(right="C", expiry_dte=5, open_px=1.00, mark=0.95, delta=0.55)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "ROLL"


class TestEarnings:
    def test_earnings_inside_dte(self):
        p = pos(expiry_dte=14)
        earnings = [TODAY + timedelta(days=10)]
        a = advise_position(p, earnings, TODAY, cfg())
        assert a.label == "ROLL"

    def test_earnings_just_after_expiry_within_window(self):
        # Earnings 3 days after expiry, window=7 → still conflict.
        p = pos(expiry_dte=14)
        earnings = [TODAY + timedelta(days=17)]
        a = advise_position(p, earnings, TODAY, cfg())
        assert a.label == "ROLL"

    def test_earnings_far_after_expiry_no_conflict(self):
        p = pos(expiry_dte=10, open_px=1.00, mark=0.95, delta=-0.20)
        earnings = [TODAY + timedelta(days=60)]
        a = advise_position(p, earnings, TODAY, cfg())
        assert a.label == "HOLD"

    def test_no_earnings_no_conflict(self):
        p = pos(expiry_dte=10, open_px=1.00, mark=0.95, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"


class TestHold:
    def test_default_hold_carries_metrics(self):
        p = pos(expiry_dte=30, open_px=1.00, mark=0.80, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"
        assert "DTE 30d" in a.reason
        assert "20%" in a.reason  # decay percentage
        assert "Δ=0.20" in a.reason

    def test_hold_with_missing_mark(self):
        # No mark / delta cached yet → still emits HOLD with what we have.
        p = pos(open_px=1.00, mark=None, delta=None)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"

    def test_decay_zero_holds(self):
        # mark == open exactly: decay 0%, far from any threshold.
        p = pos(expiry_dte=30, open_px=1.00, mark=1.00, delta=-0.10)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"


class TestStopLoss:
    def test_at_2x_returns_stop_loss(self):
        p = pos(open_px=1.00, mark=2.00, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "STOP_LOSS"
        assert a.severity == "warn"
        assert "100%" in a.reason

    def test_above_2x_returns_stop_loss(self):
        p = pos(open_px=1.00, mark=3.00, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "STOP_LOSS"

    def test_under_2x_falls_through(self):
        p = pos(open_px=1.00, mark=1.50, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"

    def test_stop_loss_beats_delta_crit(self):
        # Both fire — STOP_LOSS wins by priority (capital signal > defense).
        p = pos(open_px=1.00, mark=2.50, delta=-0.55)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "STOP_LOSS"

    def test_profit_80_beats_stop_loss(self):
        # Profit 80% would never co-occur with mark=2× in real life, but pin
        # the priority anyway.
        p = pos(open_px=1.00, mark=0.10)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "CLOSE"

    def test_disabled_when_multiplier_zero(self):
        p = pos(open_px=1.00, mark=5.00, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg(stop_loss_multiplier=0))
        assert a.label == "HOLD"

    def test_custom_multiplier(self):
        p = pos(open_px=1.00, mark=2.00, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg(stop_loss_multiplier=2.5))
        assert a.label == "HOLD"  # under 2.5×

    def test_missing_mark_falls_through(self):
        p = pos(open_px=1.00, mark=None, delta=-0.20)
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"


def test_short_dte_threshold_constant():
    # Pin the value — if someone changes it, they must reckon with the test.
    assert SHORT_DTE_THRESHOLD == 7


class TestQuantitativeFields:
    def test_hold_emits_pnl_in_dollars(self):
        # 5 contracts, 1.00 open → 0.80 mark means $100 profit ((1-0.8)*5*100).
        p = ShortPositionSnapshot(
            symbol="ABC", right="P", strike=50, expiry=TODAY + timedelta(days=30),
            qty=-5, avg_open_price=1.00, current_mark=0.80, current_delta=-0.20,
        )
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"
        assert a.contracts == 5
        assert a.premium_open_total == 500.0
        assert a.current_value_total == 400.0
        assert a.pnl_unrealized == 100.0
        assert a.remaining_max_profit == 400.0
        assert a.dte == 30
        assert a.decay_pct is not None and abs(a.decay_pct - 0.20) < 1e-9
        assert a.delta_abs == 0.20

    def test_close_80pct_reason_includes_dollar_remaining(self):
        # 10 contracts, 2.00 open → 0.20 mark → decayed 90%, $200 remaining.
        p = ShortPositionSnapshot(
            symbol="X", right="C", strike=60, expiry=TODAY + timedelta(days=20),
            qty=-10, avg_open_price=2.00, current_mark=0.20, current_delta=0.10,
        )
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "CLOSE"
        assert a.pnl_unrealized == 1800.0  # (2.00 - 0.20) * 10 * 100
        assert "$200" in a.reason          # remaining

    def test_stop_loss_reason_shows_dollar_loss(self):
        p = ShortPositionSnapshot(
            symbol="X", right="P", strike=50, expiry=TODAY + timedelta(days=20),
            qty=-3, avg_open_price=1.00, current_mark=2.50, current_delta=-0.30,
        )
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "STOP_LOSS"
        assert a.pnl_unrealized == -450.0  # (1.00 - 2.50) * 3 * 100
        assert "-$450" in a.reason

    def test_missing_mark_still_carries_premium_context(self):
        p = ShortPositionSnapshot(
            symbol="X", right="P", strike=50, expiry=TODAY + timedelta(days=30),
            qty=-2, avg_open_price=1.25, current_mark=None, current_delta=None,
        )
        a = advise_position(p, [], TODAY, cfg())
        assert a.label == "HOLD"
        assert a.premium_open_total == 250.0
        assert a.current_value_total is None
        assert a.pnl_unrealized is None
