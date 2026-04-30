"""Opening Advisor ranking tests."""
from datetime import date, timedelta

from pytest import approx as pytest_approx

from options_tool.domain.advisor_open import rank_chain
from options_tool.domain.intents import FilterableQuote
from options_tool.settings import IntentPreset


TODAY = date(2026, 4, 19)


def quote(
    strike: float,
    *,
    right: str = "C",
    dte: int = 35,
    delta: float = 0.15,
    bid: float = 1.00,
    ask: float = 1.10,
) -> FilterableQuote:
    return FilterableQuote(
        symbol="TEST",
        expiry=TODAY + timedelta(days=dte),
        strike=strike,
        right=right,
        bid=bid,
        ask=ask,
        delta=delta,
    )


INCOME = IntentPreset(
    side="CALL", delta_max=0.20, dte_min=30, dte_max=45, rank_by="annualized_roc",
    top_n=5,
)

TRADE = IntentPreset(
    side="CALL", delta_min=0.25, delta_max=0.35, dte_min=7, dte_max=21,
    rank_by="premium_absolute", top_n=5,
)


class TestRankChain:
    def test_ranks_by_annualized_roc(self):
        chain = [
            quote(105, dte=35, delta=0.10, bid=0.50, ask=0.60),
            quote(102, dte=35, delta=0.18, bid=1.20, ask=1.30),  # higher premium → higher ROC
            quote(108, dte=35, delta=0.05, bid=0.20, ask=0.30),
        ]
        result = rank_chain(
            chain, symbol="TEST", intent="INCOME", preset=INCOME,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        assert [c.strike for c in result] == [102, 105, 108]
        # ROC sanity
        assert result[0].annualized_roc > result[1].annualized_roc

    def test_ranks_by_premium_absolute_for_trade(self):
        chain = [
            quote(101, dte=14, delta=0.30, bid=2.00, ask=2.10),
            quote(102, dte=14, delta=0.30, bid=1.50, ask=1.60),
            quote(103, dte=14, delta=0.30, bid=3.00, ask=3.10),
        ]
        result = rank_chain(
            chain, symbol="TEST", intent="TRADE", preset=TRADE,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        # Highest absolute premium first
        assert [c.strike for c in result] == [103, 101, 102]

    def test_top_n_caps_results(self):
        chain = [quote(100 + i, delta=0.10) for i in range(10)]
        preset = IntentPreset(
            side="CALL", delta_max=0.20, dte_min=30, dte_max=45,
            rank_by="annualized_roc", top_n=3,
        )
        result = rank_chain(
            chain, symbol="TEST", intent="INCOME", preset=preset,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        assert len(result) == 3

    def test_empty_chain_returns_empty(self):
        result = rank_chain(
            [], symbol="TEST", intent="INCOME", preset=INCOME,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        assert result == []

    def test_dte_field_set_correctly(self):
        chain = [quote(105, dte=42)]
        result = rank_chain(
            chain, symbol="TEST", intent="INCOME", preset=INCOME,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        assert result[0].dte == 42

    def test_bid_ask_last_propagate_to_candidate(self):
        # FilterableQuote bid/ask/last must reach the Candidate so the UI
        # can render the breakdown tooltip + spread% warning.
        chain = [
            FilterableQuote(
                symbol="TEST",
                expiry=TODAY + timedelta(days=35),
                strike=105,
                right="C",
                bid=1.20,
                ask=1.40,
                delta=0.15,
                last=1.35,
            )
        ]
        result = rank_chain(
            chain, symbol="TEST", intent="INCOME", preset=INCOME,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        c = result[0]
        assert c.bid == 1.20
        assert c.ask == 1.40
        assert c.last == 1.35
        assert c.premium == pytest_approx(1.30)  # mid


class TestCandidateSpread:
    """Spread/spread_pct/quote_source properties drive the Web UI badges."""

    def _candidate(self, *, bid: float | None, ask: float | None,
                   last: float | None = None, premium: float | None = None):
        # Build a Candidate via rank_chain so we exercise the real path.
        if premium is None:
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                premium = (bid + ask) / 2.0
            else:
                premium = last or 0.0
        chain = [
            FilterableQuote(
                symbol="T", expiry=TODAY + timedelta(days=35),
                strike=100, right="C",
                bid=bid, ask=ask, delta=0.15, last=last,
            )
        ]
        result = rank_chain(
            chain, symbol="T", intent="INCOME", preset=INCOME,
            today=TODAY, underlying_price=100.0, weekly_ok=True,
        )
        return result[0]

    def test_spread_basic(self):
        c = self._candidate(bid=1.00, ask=1.20)
        assert c.spread == pytest_approx(0.20)
        assert c.spread_pct == pytest_approx(0.20 / 1.10)
        assert c.quote_source == "mid"

    def test_spread_none_when_bid_missing(self):
        c = self._candidate(bid=None, ask=1.10, last=1.05)
        assert c.spread is None
        assert c.spread_pct is None
        assert c.quote_source == "last"

    def test_spread_none_when_zero_bid(self):
        # IB returns 0 bid after-hours / illiquid → falls back to last.
        c = self._candidate(bid=0.0, ask=1.10, last=1.00)
        assert c.spread is None
        assert c.spread_pct is None
        assert c.quote_source == "last"

    def test_wide_spread_threshold_30pct(self):
        # bid 0.50 / ask 1.00 → mid 0.75, spread 0.50 = 67% of mid
        c = self._candidate(bid=0.50, ask=1.00)
        assert c.spread_pct is not None and c.spread_pct > 0.30
