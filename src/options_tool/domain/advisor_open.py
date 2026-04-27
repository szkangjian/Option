"""Opening Advisor — rank option chain candidates within an intent preset.

This is the strategy engine's first sub-engine. Inputs are a chain of quotes,
the intent preset, and the symbol's metadata (target_buy_price, earnings dates).
Output is a ranked list of ``Candidate`` rows.

Pure function. No I/O. The caller (``options_tool.advisor``) composes this with
the IBKR fetcher and DB persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_tool.domain.intents import FilterableQuote, filter_chain, is_monthly_expiry
from options_tool.domain.roc import annualized_roc
from options_tool.settings import IntentPreset


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    intent: str
    right: str
    strike: float
    expiry: date
    dte: int
    premium: float                # mid, per share
    delta: float | None
    annualized_roc: float
    underlying_price: float
    open_interest: int | None = None
    is_monthly: bool = False


def rank_chain(
    quotes: list[FilterableQuote],
    *,
    symbol: str,
    intent: str,
    preset: IntentPreset,
    today: date,
    underlying_price: float,
    earnings_dates: list[date] | None = None,
    target_buy_price: float | None = None,
    weekly_ok: bool = False,
) -> list[Candidate]:
    """Apply the preset filter, then sort by the preset's rank metric.

    Returns at most ``preset.top_n`` candidates.
    """
    survivors = filter_chain(
        quotes,
        preset=preset,
        today=today,
        earnings_dates=earnings_dates,
        target_buy_price=target_buy_price,
        weekly_ok=weekly_ok,
    )

    candidates: list[Candidate] = []
    for q in survivors:
        mid = q.mid
        if mid is None:
            continue
        dte = (q.expiry - today).days
        roc = annualized_roc(
            premium_per_share=mid,
            side=preset.side,
            underlying_price=underlying_price,
            strike=q.strike,
            dte=dte,
        )
        candidates.append(
            Candidate(
                symbol=symbol,
                intent=intent,
                right=q.right,
                strike=q.strike,
                expiry=q.expiry,
                dte=dte,
                premium=mid,
                delta=q.delta,
                annualized_roc=roc,
                underlying_price=underlying_price,
                open_interest=q.open_interest,
                is_monthly=is_monthly_expiry(q.expiry),
            )
        )

    if preset.rank_by == "premium_absolute":
        candidates.sort(key=lambda c: c.premium, reverse=True)
    else:  # default: annualized_roc
        candidates.sort(key=lambda c: c.annualized_roc, reverse=True)

    return candidates[: preset.top_n]
