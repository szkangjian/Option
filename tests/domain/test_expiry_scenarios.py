"""Tests for domain.expiry_scenarios."""
from options_tool.domain.expiry_scenarios import (
    CONTRACT_MULTIPLIER,
    scenario_for_short,
)


class TestShortCallScenario:
    def test_assigned_realizes_round_trip_with_basis(self):
        s = scenario_for_short(
            right="C",
            strike=60.0,
            qty=-10,
            avg_open_price=0.99,
            stock_avg_cost=53.43,
        )
        assert s is not None
        assert s.effective_price_per_share == 60.99
        # 60.99 - 53.43 = 7.56/share; × 10 × 100 = $7,560
        assert s.realized_per_share == 60.99 - 53.43
        assert round(s.realized_total, 2) == round(7.56 * 10 * CONTRACT_MULTIPLIER, 2)
        assert s.oom_kept_per_share == 0.99
        assert s.oom_kept_total == 0.99 * 10 * 100

    def test_assigned_without_basis_has_no_realized(self):
        s = scenario_for_short(
            right="C", strike=60, qty=-5, avg_open_price=1.25, stock_avg_cost=None,
        )
        assert s is not None
        assert s.realized_per_share is None
        assert s.realized_total is None
        assert s.effective_price_per_share == 61.25


class TestShortPutScenario:
    def test_effective_buy_is_strike_minus_premium(self):
        s = scenario_for_short(
            right="P", strike=50, qty=-2, avg_open_price=1.20,
        )
        assert s is not None
        assert s.effective_price_per_share == 48.80
        assert s.realized_per_share is None        # no prior basis on fresh lot
        assert s.oom_kept_total == 1.20 * 2 * 100

    def test_ignores_stock_avg_cost_for_put(self):
        s1 = scenario_for_short(right="P", strike=50, qty=-1, avg_open_price=1.0)
        s2 = scenario_for_short(
            right="P", strike=50, qty=-1, avg_open_price=1.0, stock_avg_cost=55,
        )
        assert s1 == s2


class TestGuards:
    def test_long_position_returns_none(self):
        assert (
            scenario_for_short(right="C", strike=60, qty=5, avg_open_price=1.0)
            is None
        )

    def test_missing_premium_returns_none(self):
        assert (
            scenario_for_short(right="C", strike=60, qty=-5, avg_open_price=None)
            is None
        )

    def test_zero_premium_returns_none(self):
        assert (
            scenario_for_short(right="C", strike=60, qty=-5, avg_open_price=0.0)
            is None
        )
