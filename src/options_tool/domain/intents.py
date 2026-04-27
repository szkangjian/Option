"""Intent → filter logic for the Opening Advisor.

A symbol is tagged with one intent (CORE_HOLD, INCOME, TRADE, WANT_TO_OWN,
WATCH). Each intent has a preset filter box defined in ``config/intents.yaml``
and loaded as ``IntentPreset`` (see ``options_tool.settings``).

This module:
- Validates an intent value.
- Decides which intents are scannable (i.e., produce recommendations).
- Filters a set of OptionQuotes through a preset, returning only contracts
  that pass every constraint.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_tool.settings import IntentPreset

def is_monthly_expiry(d: date) -> bool:
    """True if ``d`` is a standard US monthly options expiry (3rd Friday)."""
    return d.weekday() == 4 and 15 <= d.day <= 21


# Tags that produce recommendations.
SCANNABLE_INTENTS = frozenset({"INCOME", "TRADE", "WANT_TO_OWN"})

# Tags that are valid but produce nothing.
SILENT_INTENTS = frozenset({"CORE_HOLD", "WATCH"})

ALL_INTENTS = SCANNABLE_INTENTS | SILENT_INTENTS


@dataclass(frozen=True, slots=True)
class FilterableQuote:
    """Minimal contract shape needed for filtering.

    Defined here (not in ibkr.py) so domain code stays I/O-free. ``ibkr.OptionQuote``
    is structurally compatible — duck-typed.
    """

    symbol: str
    expiry: date
    strike: float
    right: str
    bid: float | None
    ask: float | None
    delta: float | None
    last: float | None = None  # used as mid fallback when market closed
    open_interest: int | None = None

    @property
    def mid(self) -> float | None:
        if (
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
        ):
            return (self.bid + self.ask) / 2.0
        if self.last is not None and self.last > 0:
            return self.last
        return None


def is_scannable(intent: str) -> bool:
    return intent in SCANNABLE_INTENTS


def filter_chain(
    quotes: list[FilterableQuote],
    *,
    preset: IntentPreset,
    today: date,
    earnings_dates: list[date] | None = None,
    target_buy_price: float | None = None,
    weekly_ok: bool = False,
) -> list[FilterableQuote]:
    """Apply preset constraints to ``quotes``, return survivors.

    Constraints applied (in order):
      - right matches preset.side
      - DTE in [dte_min, dte_max]
      - expiry is monthly (3rd Friday) unless ``weekly_ok``
      - bid/ask present and > 0 (i.e., quote is real)
      - delta within [delta_min, delta_max] when present (puts use abs(delta))
      - strike <= target_buy_price * strike_max_vs_target (WANT_TO_OWN only)
      - DTE does not cross any earnings date (when exclude_earnings_dte)

    Quotes lacking delta when delta_max is set are *kept* (we can't penalize
    missing data; the advisor ranks them lower if ROC is similar).
    """
    earnings_dates = earnings_dates or []
    expected_right = "C" if preset.side.upper() == "CALL" else "P"

    out: list[FilterableQuote] = []
    for q in quotes:
        if q.right != expected_right:
            continue

        dte = (q.expiry - today).days
        if dte < preset.dte_min or dte > preset.dte_max:
            continue

        if not weekly_ok and not is_monthly_expiry(q.expiry):
            continue

        if q.mid is None:
            continue

        # Delta filter — for puts, IBKR returns negative delta; compare on abs.
        if q.delta is not None:
            d = abs(q.delta)
            if preset.delta_min is not None and d < preset.delta_min:
                continue
            if preset.delta_max is not None and d > preset.delta_max:
                continue

        # WANT_TO_OWN strike cap
        if preset.strike_max_vs_target is not None:
            if target_buy_price is None:
                # Misconfiguration: WANT_TO_OWN preset but no target. Drop everything
                # to surface the problem visibly.
                return []
            cap = target_buy_price * preset.strike_max_vs_target
            if q.strike > cap:
                continue

        # Earnings exclusion: drop if any earnings date falls in (today, expiry]
        if preset.exclude_earnings_dte and earnings_dates:
            if any(today < e <= q.expiry for e in earnings_dates):
                continue

        out.append(q)

    return out
