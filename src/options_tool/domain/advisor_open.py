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

from options_tool.domain.intents import (
    FilterableQuote,
    Rejection,
    filter_chain,
    filter_chain_with_reasons,
    is_monthly_expiry,
)
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
    premium: float                # mid, per share (or last-price fallback)
    delta: float | None
    annualized_roc: float
    underlying_price: float
    open_interest: int | None = None
    is_monthly: bool = False
    bid: float | None = None      # for tooltip / spread display
    ask: float | None = None
    last: float | None = None     # populated when bid/ask invalid → premium = last

    @property
    def spread(self) -> float | None:
        """Absolute spread = ask - bid. None when either side missing."""
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= 0 or self.ask <= 0:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        """Spread as % of mid. None when bid/ask invalid.

        Used by UI to flag illiquid contracts (> 30% = warning). Range
        [0, 2.0] — values approach 2.0 only for ultra-wide quotes.
        """
        s = self.spread
        if s is None or self.premium <= 0:
            return None
        return s / self.premium

    @property
    def quote_source(self) -> str:
        """One of "mid" / "last" — which path FilterableQuote.mid took."""
        if (
            self.bid is not None and self.ask is not None
            and self.bid > 0 and self.ask > 0
        ):
            return "mid"
        return "last"


def rank_chain_with_rejections(
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
) -> tuple[list[Candidate], list[Rejection]]:
    """Filter + rank, returning both survivors and per-quote rejections.

    Returns ``(candidates, rejections)``. ``candidates`` is capped at
    ``preset.top_n``. ``rejections`` contains every quote dropped by the
    intent filter, tagged with the first binding reason — used by the Web UI
    to explain *why* the chain produced no candidates.
    """
    survivors, rejections = filter_chain_with_reasons(
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
                bid=q.bid,
                ask=q.ask,
                last=q.last,
            )
        )

    if preset.rank_by == "premium_absolute":
        candidates.sort(key=lambda c: c.premium, reverse=True)
    else:  # default: annualized_roc
        candidates.sort(key=lambda c: c.annualized_roc, reverse=True)

    return candidates[: preset.top_n], rejections


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
    """Survivors-only convenience wrapper. Existing callers keep working."""
    candidates, _ = rank_chain_with_rejections(
        quotes,
        symbol=symbol,
        intent=intent,
        preset=preset,
        today=today,
        underlying_price=underlying_price,
        earnings_dates=earnings_dates,
        target_buy_price=target_buy_price,
        weekly_ok=weekly_ok,
    )
    return candidates
