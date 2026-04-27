"""Feedback-loop analyzer tests — outcome math + bucketing."""
from datetime import date

import pytest

from options_tool.domain.feedback_loop import (
    CONTRACT_MULTIPLIER,
    RecRow,
    analyze,
    classify_outcome,
)

TODAY = date(2026, 4, 22)


def rec(
    *,
    rec_id: int = 1,
    symbol: str = "ABC",
    intent: str = "INCOME",
    right: str = "C",
    strike: float = 50.0,
    expiry: date = date(2026, 3, 20),
    premium: float = 1.00,
    delta: float | None = -0.20,
    dte: int = 30,
    annualized_roc: float = 0.25,
    rank: int = 1,
    taken: bool = False,
) -> RecRow:
    return RecRow(
        id=rec_id, symbol=symbol, intent=intent, right=right, strike=strike,
        expiry=expiry, premium=premium, delta=delta, dte=dte,
        annualized_roc=annualized_roc, rank=rank, taken=taken,
    )


class TestClassify:
    def test_cc_oom_keeps_full_premium(self):
        # close 48 < strike 50 → kept $100.
        o = classify_outcome(rec(right="C", strike=50, premium=1.00), 48.0)
        assert o.pnl_total == 100.0
        assert o.profitable is True

    def test_cc_itm_loss(self):
        # close 54 > strike 50, premium $1 → pnl = 100 - 4*100 = -300.
        o = classify_outcome(rec(right="C", strike=50, premium=1.00), 54.0)
        assert o.pnl_total == -300.0
        assert o.profitable is False

    def test_cc_exact_pin_is_profit(self):
        # close == strike: keep premium (no intrinsic loss).
        o = classify_outcome(rec(right="C", strike=50, premium=1.00), 50.0)
        assert o.pnl_total == 100.0
        assert o.profitable is True

    def test_csp_oom_keeps_full_premium(self):
        # close 52 > strike 50 → kept $125.
        o = classify_outcome(rec(right="P", strike=50, premium=1.25), 52.0)
        assert o.pnl_total == 125.0
        assert o.profitable is True

    def test_csp_itm_loss(self):
        # close 47 < strike 50, premium $1.25 → pnl = 125 - 3*100 = -175.
        o = classify_outcome(rec(right="P", strike=50, premium=1.25), 47.0)
        assert o.pnl_total == -175.0
        assert o.profitable is False

    def test_kept_premium_uses_contract_multiplier(self):
        o = classify_outcome(rec(premium=0.37), 40.0)  # OOM call
        assert o.kept_premium_total == 0.37 * CONTRACT_MULTIPLIER

    def test_unknown_right_raises(self):
        with pytest.raises(ValueError):
            classify_outcome(rec(right="X"), 50.0)


class TestAnalyze:
    def test_skips_unexpired(self):
        # Rec expires 2 weeks from now — not in the analysis window.
        future_expiry = date(2026, 5, 15)
        r = rec(expiry=future_expiry)
        report = analyze([r], closes={}, as_of=TODAY)
        assert report.total_recs == 1
        assert report.expired_recs == 0
        assert report.priced_recs == 0
        assert report.missing_prices == []

    def test_missing_prices_tracked(self):
        r = rec(expiry=date(2026, 3, 20))
        report = analyze([r], closes={}, as_of=TODAY)
        assert report.expired_recs == 1
        assert report.priced_recs == 0
        assert ("ABC", date(2026, 3, 20)) in report.missing_prices

    def test_basic_hit_rate(self):
        # 4 recs, 2 win.
        recs_in = [
            rec(rec_id=1, right="C", strike=50, premium=1.00),    # close 48 → win
            rec(rec_id=2, right="C", strike=50, premium=1.00),    # close 54 → lose
            rec(rec_id=3, right="P", strike=50, premium=1.00),    # close 52 → win
            rec(rec_id=4, right="P", strike=50, premium=1.00),    # close 47 → lose
        ]
        closes = {
            ("ABC", date(2026, 3, 20)): 48.0,  # applied to all (same symbol+expiry)
        }
        # Use distinct symbols so close lookups don't collide:
        recs_in = [
            rec(rec_id=1, symbol="A", right="C", strike=50, premium=1.00),
            rec(rec_id=2, symbol="B", right="C", strike=50, premium=1.00),
            rec(rec_id=3, symbol="C", right="P", strike=50, premium=1.00),
            rec(rec_id=4, symbol="D", right="P", strike=50, premium=1.00),
        ]
        closes = {
            ("A", date(2026, 3, 20)): 48.0,
            ("B", date(2026, 3, 20)): 54.0,
            ("C", date(2026, 3, 20)): 52.0,
            ("D", date(2026, 3, 20)): 47.0,
        }
        report = analyze(recs_in, closes=closes, as_of=TODAY)
        assert report.priced_recs == 4
        assert report.overall.hit_rate == 0.5
        # 100 - 300 + 100 - 200 = -300
        assert report.overall.total_pnl == -300.0

    def test_only_not_taken_filters(self):
        taken_win = rec(rec_id=1, symbol="A", taken=True, premium=1.00)   # close 48 → +100
        skipped_lose = rec(rec_id=2, symbol="B", taken=False, premium=1.00)  # close 54 → -300
        closes = {
            ("A", date(2026, 3, 20)): 48.0,
            ("B", date(2026, 3, 20)): 54.0,
        }
        report = analyze(
            [taken_win, skipped_lose], closes=closes, as_of=TODAY,
            only_not_taken=True,
        )
        # Only the skipped rec is analyzed.
        assert report.priced_recs == 1
        assert report.overall.total_pnl == -300.0
        assert report.overall.hit_rate == 0.0

    def test_bucket_by_intent(self):
        recs_in = [
            rec(rec_id=1, symbol="A", intent="INCOME",
                right="C", strike=50, premium=1.00),
            rec(rec_id=2, symbol="B", intent="WANT_TO_OWN",
                right="P", strike=50, premium=1.00),
        ]
        closes = {
            ("A", date(2026, 3, 20)): 48.0,   # win
            ("B", date(2026, 3, 20)): 45.0,   # lose (-400)
        }
        report = analyze(recs_in, closes=closes, as_of=TODAY)
        by_intent = {b.label: b for b in report.by_intent}
        assert by_intent["INCOME"].hit_rate == 1.0
        assert by_intent["WANT_TO_OWN"].hit_rate == 0.0
        assert by_intent["WANT_TO_OWN"].total_pnl == -400.0

    def test_bucket_by_rank_ordered(self):
        recs_in = [
            rec(rec_id=1, symbol="A", rank=1, premium=1.00),
            rec(rec_id=2, symbol="B", rank=2, premium=1.00),
            rec(rec_id=3, symbol="C", rank=3, premium=1.00),
        ]
        closes = {
            ("A", date(2026, 3, 20)): 48.0,
            ("B", date(2026, 3, 20)): 48.0,
            ("C", date(2026, 3, 20)): 48.0,
        }
        report = analyze(recs_in, closes=closes, as_of=TODAY)
        labels = [b.label for b in report.by_rank]
        assert labels == ["rank 1", "rank 2", "rank 3"]

    def test_bucket_by_symbol_sorted_by_pnl(self):
        # Symbol B wins more than A → B sorts first.
        recs_in = [
            rec(rec_id=1, symbol="A", premium=0.50),   # win $50
            rec(rec_id=2, symbol="B", premium=2.00),   # win $200
        ]
        closes = {
            ("A", date(2026, 3, 20)): 48.0,
            ("B", date(2026, 3, 20)): 48.0,
        }
        report = analyze(recs_in, closes=closes, as_of=TODAY)
        labels = [b.label for b in report.by_symbol]
        assert labels == ["B", "A"]

    def test_overall_with_zero_outcomes(self):
        report = analyze([], closes={}, as_of=TODAY)
        assert report.overall.count == 0
        assert report.overall.hit_rate == 0.0
        assert report.overall.total_pnl == 0.0
