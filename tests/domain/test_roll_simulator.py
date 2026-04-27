"""Roll simulator tests — filter rules, ranking, edge cases."""
from datetime import date, timedelta

from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.domain.roll_simulator import (
    RollCandidate,
    RollQuote,
    format_roll_suggestion,
    simulate_rolls,
)
from options_tool.settings import AlertsConfig

TODAY = date(2026, 4, 20)


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


def pos(right: str = "P", strike: float = 50.0, dte: int = 5) -> ShortPositionSnapshot:
    return ShortPositionSnapshot(
        symbol="ABC",
        right=right,
        strike=strike,
        expiry=TODAY + timedelta(days=dte),
        qty=-1,
        avg_open_price=1.00,
        current_mark=0.95,
        current_delta=-0.42,
    )


def q(
    *,
    dte: int,
    strike: float,
    right: str = "P",
    bid: float | None = 1.00,
    ask: float | None = 1.10,
    last: float | None = 1.05,
    delta: float | None = -0.25,
) -> RollQuote:
    return RollQuote(
        expiry=TODAY + timedelta(days=dte),
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        last=last,
        delta=delta,
    )


class TestBasicFiltering:
    def test_skips_earlier_expiry(self):
        p = pos(dte=14)
        quotes = [q(dte=7, strike=50.0), q(dte=30, strike=50.0)]
        cands = simulate_rolls(p, current_close_cost=0.50, quotes=quotes, today=TODAY, config=cfg())
        assert len(cands) == 1
        assert cands[0].new_dte == 30

    def test_skips_same_expiry(self):
        p = pos(dte=14)
        quotes = [q(dte=14, strike=50.0), q(dte=30, strike=50.0)]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert len(cands) == 1
        assert cands[0].new_dte == 30

    def test_skips_wrong_right(self):
        p = pos(right="P", dte=5)
        quotes = [q(dte=30, strike=50.0, right="C")]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert cands == []

    def test_skips_missing_bid(self):
        p = pos(dte=5)
        quotes = [q(dte=30, strike=50.0, bid=None), q(dte=30, strike=48.0, bid=0)]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert cands == []

    def test_dte_window(self):
        p = pos(dte=5)
        quotes = [
            q(dte=8, strike=50.0),    # inside
            q(dte=70, strike=50.0),   # beyond max
            q(dte=6, strike=50.0),    # below min=7
        ]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert [c.new_dte for c in cands] == [8]


class TestStrikeDirection:
    def test_csp_allows_equal_or_lower_strike(self):
        p = pos(right="P", strike=50.0, dte=5)
        quotes = [
            q(dte=30, strike=48.0, delta=-0.20),  # lower: OK
            q(dte=30, strike=50.0, delta=-0.25),  # equal: OK (flat-out)
            q(dte=30, strike=52.0, delta=-0.30),  # higher: rejected for put
        ]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert sorted(c.new_strike for c in cands) == [48.0, 50.0]

    def test_cc_allows_equal_or_higher_strike(self):
        p = pos(right="C", strike=50.0, dte=5)
        quotes = [
            q(dte=30, strike=48.0, right="C", delta=0.50),  # lower: rejected
            q(dte=30, strike=50.0, right="C", delta=0.30),  # equal: OK
            q(dte=30, strike=52.0, right="C", delta=0.25),  # higher: OK
        ]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert sorted(c.new_strike for c in cands) == [50.0, 52.0]


class TestDeltaGuard:
    def test_rejects_delta_at_or_above_warning(self):
        p = pos(dte=5)
        quotes = [
            q(dte=30, strike=50.0, delta=-0.40),  # == warn → reject
            q(dte=30, strike=48.0, delta=-0.39),  # below → accept
        ]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert [c.new_strike for c in cands] == [48.0]

    def test_missing_delta_accepted(self):
        # No delta cached → we don't block; user can sanity-check visually.
        p = pos(dte=5)
        quotes = [q(dte=30, strike=50.0, delta=None)]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert len(cands) == 1


class TestRanking:
    def test_sorted_by_net_credit_desc(self):
        p = pos(dte=5)
        quotes = [
            q(dte=30, strike=50.0, bid=0.80),  # net = 0.30
            q(dte=30, strike=48.0, bid=1.20),  # net = 0.70
            q(dte=30, strike=49.0, bid=1.00),  # net = 0.50
        ]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert [c.new_strike for c in cands] == [48.0, 49.0, 50.0]
        assert cands[0].net_credit > cands[-1].net_credit

    def test_top_n_caps_output(self):
        p = pos(dte=5)
        quotes = [q(dte=30 + i, strike=50.0 - i, bid=1.0 + i * 0.1) for i in range(10)]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg(), top_n=3)
        assert len(cands) == 3

    def test_negative_net_credit_still_reported(self):
        # Sometimes no roll is a net credit — still show it, user decides.
        p = pos(dte=5)
        quotes = [q(dte=30, strike=50.0, bid=0.30)]  # net = -0.20
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert len(cands) == 1
        assert cands[0].net_credit < 0


class TestCloseCost:
    def test_missing_close_cost_returns_empty(self):
        p = pos(dte=5)
        quotes = [q(dte=30, strike=50.0)]
        assert simulate_rolls(p, None, quotes, TODAY, cfg()) == []

    def test_negative_close_cost_returns_empty(self):
        p = pos(dte=5)
        quotes = [q(dte=30, strike=50.0)]
        assert simulate_rolls(p, -0.10, quotes, TODAY, cfg()) == []

    def test_strike_diff_is_signed(self):
        p = pos(right="C", strike=50.0, dte=5)
        quotes = [q(dte=30, strike=52.5, right="C", delta=0.20)]
        cands = simulate_rolls(p, 0.50, quotes, TODAY, cfg())
        assert cands[0].strike_diff == 2.5


class TestFormatRollSuggestion:
    """One-liner formatter used by Telegram alert enrichment."""

    def _cand(self, **overrides) -> RollCandidate:
        base = dict(
            new_expiry=date(2026, 5, 15),
            new_strike=48.0,
            new_dte=25,
            new_delta=-0.18,
            close_cost=0.50,
            open_credit=0.82,
            net_credit=0.32,
            strike_diff=-2.0,
        )
        base.update(overrides)
        return RollCandidate(**base)

    def test_empty_returns_none(self):
        assert format_roll_suggestion(pos(), []) is None

    def test_positive_credit_uses_plus_sign(self):
        line = format_roll_suggestion(pos(right="P"), [self._cand()])
        assert line is not None
        assert "net +0.32" in line
        assert "P48 2026-05-15" in line
        assert "DTE 25" in line
        assert "Δ 0.18" in line  # absolute value, two decimals

    def test_negative_credit_no_extra_sign(self):
        line = format_roll_suggestion(
            pos(), [self._cand(net_credit=-0.15, open_credit=0.35)],
        )
        assert line is not None
        assert "net -0.15" in line  # native minus, no leading "+"

    def test_missing_delta_renders_dash(self):
        line = format_roll_suggestion(
            pos(), [self._cand(new_delta=None)],
        )
        assert line is not None
        assert "Δ —" in line

    def test_picks_first_candidate_only(self):
        # Caller passes already-sorted candidates; we just take [0].
        cands = [
            self._cand(new_strike=48.0, net_credit=0.32),
            self._cand(new_strike=47.0, net_credit=0.10),
        ]
        line = format_roll_suggestion(pos(), cands)
        assert "P48" in line
        assert "P47" not in line

    def test_call_right_carries_through(self):
        p = pos(right="C", strike=50.0)
        cand = self._cand(new_strike=52.5, new_delta=0.20)
        line = format_roll_suggestion(p, [cand])
        assert line.startswith("  → roll: C52.5 ")
