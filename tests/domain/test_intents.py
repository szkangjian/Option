"""Intent filter tests."""
from datetime import date, timedelta

import pytest

from options_tool.domain.intents import (
    ALL_INTENTS,
    REASON_CROSSES_EARNINGS,
    REASON_DELTA_TOO_HIGH,
    REASON_DELTA_TOO_LOW,
    REASON_DTE_TOO_LONG,
    REASON_DTE_TOO_SHORT,
    REASON_NO_QUOTE,
    REASON_STRIKE_ABOVE_TARGET,
    REASON_WEEKLY_NOT_ALLOWED,
    SCANNABLE_INTENTS,
    FilterableQuote,
    filter_chain,
    filter_chain_with_reasons,
    is_monthly_expiry,
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


class TestMonthlyExpiry:
    def test_regular_third_friday_is_monthly(self):
        assert is_monthly_expiry(date(2026, 5, 15))

    def test_regular_weekly_is_not_monthly(self):
        assert not is_monthly_expiry(date(2026, 5, 22))

    def test_juneteenth_adjusted_monthly_is_previous_business_day(self):
        assert is_monthly_expiry(date(2026, 6, 18))
        assert not is_monthly_expiry(date(2026, 6, 19))

    def test_good_friday_adjusted_monthly_is_previous_business_day(self):
        assert is_monthly_expiry(date(2025, 4, 17))
        assert not is_monthly_expiry(date(2025, 4, 18))


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


class TestFilterChainWithReasons:
    """The reasons-aware variant of filter_chain backs the UI's
    'why was nothing recommended?' panel.
    """

    def test_dte_too_short_is_tagged(self):
        # DTE 10 < INCOME min 30
        chain = [make_call(100, dte=10)]
        survivors, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert survivors == []
        assert len(rejections) == 1
        assert rejections[0].reason_code == REASON_DTE_TOO_SHORT
        assert "10" in rejections[0].reason_detail

    def test_dte_too_long_is_tagged(self):
        chain = [make_call(100, dte=90)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert rejections[0].reason_code == REASON_DTE_TOO_LONG

    def test_weekly_blocked_when_disabled(self):
        # 2026-05-22 is a Friday but NOT monthly (3rd Friday is 5/15).
        # DTE 33 fits INCOME's 30-45 window so we're testing the weekly
        # check, not DTE.
        non_monthly = date(2026, 5, 22)
        chain = [
            FilterableQuote(
                symbol="TEST",
                expiry=non_monthly,
                strike=100,
                right="C",
                bid=1.0,
                ask=1.1,
                delta=0.15,
            )
        ]
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=False
        )
        assert rejections[0].reason_code == REASON_WEEKLY_NOT_ALLOWED

    def test_holiday_adjusted_monthly_allowed_when_weekly_disabled(self):
        # 2026-06-19 is Juneteenth, so the standard June monthly last-trading
        # expiry is Thursday 2026-06-18. It should not require weekly_ok.
        monthly = date(2026, 6, 18)
        chain = [
            FilterableQuote(
                symbol="TEST",
                expiry=monthly,
                strike=100,
                right="C",
                bid=1.0,
                ask=1.1,
                delta=0.15,
            )
        ]
        survivors, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=date(2026, 5, 14), weekly_ok=False
        )
        assert len(survivors) == 1
        assert rejections == []

    def test_no_quote_is_tagged(self):
        chain = [make_call(100, bid=0.0, ask=0.0)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert rejections[0].reason_code == REASON_NO_QUOTE

    def test_delta_too_high_is_tagged(self):
        chain = [make_call(100, delta=0.40)]  # > INCOME max 0.20
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert rejections[0].reason_code == REASON_DELTA_TOO_HIGH
        assert "0.40" in rejections[0].reason_detail

    def test_delta_too_low_is_tagged(self):
        # TRADE preset: DTE 7-21, Δ 0.25-0.35. Pick DTE 14 + Δ 0.10
        # so only the Δ check fires.
        chain = [make_call(100, dte=14, delta=0.10)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=TRADE_PRESET, today=TODAY, weekly_ok=True
        )
        assert rejections[0].reason_code == REASON_DELTA_TOO_LOW

    def test_strike_above_target_is_tagged(self):
        chain = [make_put(50)]  # target=45, cap=45 → strike 50 rejected
        _, rejections = filter_chain_with_reasons(
            chain, preset=WTO_PRESET, today=TODAY,
            target_buy_price=45.0, weekly_ok=True,
        )
        assert rejections[0].reason_code == REASON_STRIKE_ABOVE_TARGET
        assert "$50" in rejections[0].reason_detail

    def test_earnings_crossing_is_tagged(self):
        earnings = [TODAY + timedelta(days=20)]
        chain = [make_call(100, dte=35)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY,
            earnings_dates=earnings, weekly_ok=True,
        )
        assert rejections[0].reason_code == REASON_CROSSES_EARNINGS
        assert earnings[0].isoformat() in rejections[0].reason_detail

    def test_each_quote_gets_one_reason_first_binding(self):
        # DTE too short AND delta too high — should report DTE first per
        # documented order.
        chain = [make_call(100, dte=10, delta=0.99)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert len(rejections) == 1
        assert rejections[0].reason_code == REASON_DTE_TOO_SHORT

    def test_survivors_unchanged_vs_legacy(self):
        # Same input → same survivor set as the legacy filter_chain wrapper.
        chain = [
            make_call(100, dte=35, delta=0.15),  # survives
            make_call(100, dte=10, delta=0.15),  # rejected (DTE)
            make_call(100, dte=35, delta=0.50),  # rejected (Δ)
        ]
        legacy = filter_chain(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        survivors, _ = filter_chain_with_reasons(
            chain, preset=INCOME_PRESET, today=TODAY, weekly_ok=True
        )
        assert legacy == survivors
        assert len(survivors) == 1

    def test_rejection_carries_quote_metadata(self):
        chain = [make_put(50, dte=35, delta=-0.25)]
        _, rejections = filter_chain_with_reasons(
            chain, preset=WTO_PRESET, today=TODAY,
            target_buy_price=45.0, weekly_ok=True,
        )
        r = rejections[0]
        assert r.right == "P"
        assert r.strike == 50
        assert r.dte == 35
        assert r.delta == -0.25
        assert r.mid is not None and r.mid > 0
