"""Opening Advisor ranking tests."""
from datetime import date, timedelta

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
