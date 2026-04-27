"""Intent filter tests."""
from datetime import date, timedelta

import pytest

from options_tool.domain.intents import (
    ALL_INTENTS,
    SCANNABLE_INTENTS,
    FilterableQuote,
    filter_chain,
    is_scannable,
)
from options_tool.settings import IntentPreset


TODAY = date(2026, 4, 19)


def make_call(
    strike: float,
    dte: int = 35,
    delta: float | None = 0.15,
    bid: float = 1.00,
    ask: float = 1.10,
) -> FilterableQuote:
    return FilterableQuote(
        symbol="TEST",
        expiry=TODAY + timedelta(days=dte),
        strike=strike,
        right="C",
        bid=bid,
        ask=ask,
        delta=delta,
    )


def make_put(
    strike: float,
    dte: int = 30,
    delta: float | None = -0.20,
    bid: float = 0.80,
    ask: float = 0.90,
) -> FilterableQuote:
    return FilterableQuote(
        symbol="TEST",
        expiry=TODAY + timedelta(days=dte),
        strike=strike,
        right="P",
        bid=bid,
        ask=ask,
        delta=delta,
    )


INCOME_PRESET = IntentPreset(
    side="CALL", delta_max=0.20, dte_min=30, dte_max=45, rank_by="annualized_roc"
)

TRADE_PRESET = IntentPreset(
    side="CALL", delta_min=0.25, delta_max=0.35, dte_min=7, dte_max=21,
    rank_by="premium_absolute",
)

WTO_PRESET = IntentPreset(
    side="PUT", delta_max=0.30, dte_min=21, dte_max=45,
    strike_max_vs_target=1.0, rank_by="annualized_roc",
)


class TestIntentMetadata:
    def test_scannable_subset(self):
        assert SCANNABLE_INTENTS <= ALL_INTENTS

    def test_is_scannable_true_for_income(self):
        assert is_scannable("INCOME")

    def test_core_hold_not_scannable(self):
        assert not is_scannable("CORE_HOLD")

    def test_watch_not_scannable(self):
        assert not is_scannable("WATCH")


class TestFilterChain:
    def test_income_filters_by_side(self):
        chain = [make_call(105), make_put(95)]
        result = filter_chain(chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True)
        assert len(result) == 1
        assert result[0].right == "C"

    def test_income_excludes_high_delta(self):
        # Delta 0.25 should be rejected by INCOME (max 0.20)
        chain = [make_call(100, delta=0.25)]
        result = filter_chain(chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True)
        assert result == []

    def test_income_dte_window(self):
        # INCOME wants 30-45. Short-dated contract should be rejected.
        chain = [make_call(100, dte=14)]
        result = filter_chain(chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True)
        assert result == []

    def test_trade_prefers_aggressive_delta(self):
        # TRADE preset: 0.25-0.35 delta window
        chain = [
            make_call(100, dte=14, delta=0.10),   # below band
            make_call(101, dte=14, delta=0.30),   # in band
            make_call(102, dte=14, delta=0.40),   # above band
        ]
        result = filter_chain(chain, preset=TRADE_PRESET, today=TODAY, weekly_ok=True)
        assert len(result) == 1
        assert result[0].strike == 101

    def test_missing_delta_is_kept(self):
        # Don't penalize missing data; advisor can rank by ROC only.
        chain = [make_call(100, delta=None)]
        result = filter_chain(chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True)
        assert len(result) == 1

    def test_zero_bid_rejected(self):
        # A contract with no real bid is unfillable
        chain = [make_call(100, bid=0.0, ask=0.10)]
        result = filter_chain(chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True)
        assert result == []

    def test_want_to_own_requires_target(self):
        chain = [make_put(45)]
        result = filter_chain(chain, preset=WTO_PRESET, today=TODAY, target_buy_price=None, weekly_ok=True)
        assert result == []  # misconfiguration → drop all

    def test_want_to_own_strike_cap(self):
        # target=45, strike_max_vs_target=1.0 → strike must be <= 45
        chain = [
            make_put(40),   # passes
            make_put(45),   # passes (boundary)
            make_put(50),   # rejected
        ]
        result = filter_chain(chain, preset=WTO_PRESET, today=TODAY, target_buy_price=45.0, weekly_ok=True)
        strikes = {q.strike for q in result}
        assert strikes == {40, 45}

    def test_want_to_own_put_delta_uses_abs(self):
        # IBKR returns puts with negative delta. Our filter should use abs.
        chain = [make_put(40, delta=-0.40)]  # |delta|=0.40 > 0.30 max → reject
        result = filter_chain(chain, preset=WTO_PRESET, today=TODAY, target_buy_price=45.0, weekly_ok=True)
        assert result == []

    def test_earnings_exclusion(self):
        # Earnings in 20 days, expiry in 35 days → contract rejected
        earnings = [TODAY + timedelta(days=20)]
        chain = [make_call(100, dte=35)]
        result = filter_chain(
            chain, preset=INCOME_PRESET, today=TODAY, earnings_dates=earnings, weekly_ok=True
        )
        assert result == []

    def test_earnings_past_expiry_is_fine(self):
        # Earnings in 50 days, expiry in 35 → no conflict
        earnings = [TODAY + timedelta(days=50)]
        chain = [make_call(100, dte=35)]
        result = filter_chain(
            chain, preset=INCOME_PRESET, today=TODAY, earnings_dates=earnings, weekly_ok=True
        )
        assert len(result) == 1

    def test_earnings_on_expiry_day_is_conflict(self):
        # Earnings exactly on expiry day → reject (earnings move risk unresolved)
        earnings = [TODAY + timedelta(days=35)]
        chain = [make_call(100, dte=35)]
        result = filter_chain(
            chain, preset=INCOME_PRESET, today=TODAY, earnings_dates=earnings, weekly_ok=True
        )
        assert result == []
