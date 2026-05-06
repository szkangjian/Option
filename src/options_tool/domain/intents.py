"""Intent → filter logic for the Opening Advisor.

A symbol is tagged with one intent (CORE_HOLD, INCOME, TRADE, WANT_TO_OWN,
WATCH). Each intent has a preset filter box defined in ``config/intents.yaml``
and loaded as ``IntentPreset`` (see ``options_tool.settings``).

This module:
- Validates an intent value.
- Decides which intents are scannable (i.e., produce recommendations).
- Filters a set of OptionQuotes through a preset, returning only contracts
  that pass every constraint.
- Reports per-quote rejection reasons so the UI can show the user *why*
  a chain that returned 12 quotes produced 0 candidates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from options_tool.settings import IntentPreset


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth weekday in a month. Monday=0, Friday=4."""
    first = date(year, month, 1)
    days_until = (weekday - first.weekday()) % 7
    return first + timedelta(days=days_until + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last weekday in a month. Monday=0, Friday=4."""
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:  # Saturday
        return actual - timedelta(days=1)
    if actual.weekday() == 6:  # Sunday
        return actual + timedelta(days=1)
    return actual


def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday via the Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _is_us_market_holiday(d: date) -> bool:
    """NYSE-style full-day holiday calendar for expiry-date adjustment."""
    holidays = {
        _nth_weekday(d.year, 1, 0, 3),   # Martin Luther King Jr. Day
        _nth_weekday(d.year, 2, 0, 3),   # Washington's Birthday
        _easter_sunday(d.year) - timedelta(days=2),  # Good Friday
        _last_weekday(d.year, 5, 0),     # Memorial Day
        _nth_weekday(d.year, 9, 0, 1),   # Labor Day
        _nth_weekday(d.year, 11, 3, 4),  # Thanksgiving Day
    }
    for year in (d.year - 1, d.year, d.year + 1):
        holidays.add(_observed_fixed_holiday(year, 1, 1))    # New Year's Day
        if year >= 2022:
            holidays.add(_observed_fixed_holiday(year, 6, 19))  # Juneteenth
        holidays.add(_observed_fixed_holiday(year, 7, 4))    # Independence Day
        holidays.add(_observed_fixed_holiday(year, 12, 25))  # Christmas Day
    return d in holidays


def standard_monthly_expiry_date(year: int, month: int) -> date:
    """Standard US equity monthly option last-trading expiry date.

    Equity monthlies are anchored to the third Friday. If that Friday is a
    full market holiday (for example Good Friday or Juneteenth), the listed
    last trading date moves back to the previous business day.
    """
    expiry = _nth_weekday(year, month, 4, 3)
    while expiry.weekday() >= 5 or _is_us_market_holiday(expiry):
        expiry -= timedelta(days=1)
    return expiry


def is_monthly_expiry(d: date) -> bool:
    """True if ``d`` is a standard US equity monthly option expiry date."""
    return d == standard_monthly_expiry_date(d.year, d.month)


# Tags that produce recommendations.
SCANNABLE_INTENTS = frozenset({"INCOME", "TRADE", "WANT_TO_OWN"})

# Tags that are valid but produce nothing.
SILENT_INTENTS = frozenset({"CORE_HOLD", "WATCH"})

ALL_INTENTS = SCANNABLE_INTENTS | SILENT_INTENTS


# Rejection reason codes — stable strings used by templates / tests.
# Order matters in filter_chain_with_reasons: each quote gets the FIRST
# binding reason, so put the most fundamental checks first.
REASON_DTE_TOO_SHORT = "dte_too_short"
REASON_DTE_TOO_LONG = "dte_too_long"
REASON_WEEKLY_NOT_ALLOWED = "weekly_not_allowed"
REASON_NO_QUOTE = "no_quote"
REASON_DELTA_TOO_LOW = "delta_too_low"
REASON_DELTA_TOO_HIGH = "delta_too_high"
REASON_STRIKE_ABOVE_TARGET = "strike_above_target"
REASON_CROSSES_EARNINGS = "crosses_earnings"
# Post-filter reasons (set by advisor.py, not filter_chain):
REASON_ALREADY_HELD = "already_held"
REASON_PENDING_ORDER = "pending_order"

# Localized labels shown in the Web UI.
REASON_LABELS: dict[str, str] = {
    REASON_DTE_TOO_SHORT: "DTE 太短",
    REASON_DTE_TOO_LONG: "DTE 太长",
    REASON_WEEKLY_NOT_ALLOWED: "周期权（intent 未启用 weekly）",
    REASON_NO_QUOTE: "无报价（bid/ask 缺失）",
    REASON_DELTA_TOO_LOW: "Δ 太小",
    REASON_DELTA_TOO_HIGH: "Δ 太大",
    REASON_STRIKE_ABOVE_TARGET: "Strike 高于 target × cap",
    REASON_CROSSES_EARNINGS: "DTE 跨财报",
    REASON_ALREADY_HELD: "已持仓",
    REASON_PENDING_ORDER: "已挂单",
}


@dataclass(frozen=True, slots=True)
class Rejection:
    """One quote that didn't make it past the filter.

    Carries enough context (right/strike/expiry/dte/delta/mid) to render
    in a UI table without needing to look up the original quote.
    """

    right: str
    strike: float
    expiry: date
    dte: int
    delta: float | None
    mid: float | None
    reason_code: str
    reason_detail: str  # human-readable specific (e.g. "DTE 18 < min 21")


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


def filter_chain_with_reasons(
    quotes: list[FilterableQuote],
    *,
    preset: IntentPreset,
    today: date,
    earnings_dates: list[date] | None = None,
    target_buy_price: float | None = None,
    weekly_ok: bool = False,
) -> tuple[list[FilterableQuote], list[Rejection]]:
    """Same logic as :func:`filter_chain`, but also returns one ``Rejection``
    per dropped quote, tagged with the FIRST binding reason.

    The order of checks is:
      1. right matches preset.side  (silent — wrong-side quotes never reach UI)
      2. DTE in [dte_min, dte_max]
      3. expiry is monthly unless ``weekly_ok``
      4. bid/ask present and > 0
      5. delta within [delta_min, delta_max] when present
      6. strike <= target_buy_price * strike_max_vs_target  (WANT_TO_OWN)
      7. DTE does not cross any earnings date (when exclude_earnings_dte)
    """
    earnings_dates = earnings_dates or []
    expected_right = "C" if preset.side.upper() == "CALL" else "P"

    survivors: list[FilterableQuote] = []
    rejections: list[Rejection] = []

    def _reject(q: FilterableQuote, code: str, detail: str) -> None:
        dte_ = (q.expiry - today).days
        rejections.append(
            Rejection(
                right=q.right,
                strike=q.strike,
                expiry=q.expiry,
                dte=dte_,
                delta=q.delta,
                mid=q.mid,
                reason_code=code,
                reason_detail=detail,
            )
        )

    for q in quotes:
        if q.right != expected_right:
            # Silent: chain fetcher only returns one side; if this fires it's
            # a developer bug, not user-actionable. Drop without recording.
            continue

        dte = (q.expiry - today).days
        if dte < preset.dte_min:
            _reject(q, REASON_DTE_TOO_SHORT, f"DTE {dte} < min {preset.dte_min}")
            continue
        if dte > preset.dte_max:
            _reject(q, REASON_DTE_TOO_LONG, f"DTE {dte} > max {preset.dte_max}")
            continue

        if not weekly_ok and not is_monthly_expiry(q.expiry):
            _reject(
                q, REASON_WEEKLY_NOT_ALLOWED,
                f"{q.expiry.isoformat()} 不是标准月度到期日",
            )
            continue

        if q.mid is None:
            _reject(q, REASON_NO_QUOTE, "bid/ask 都缺或为 0")
            continue

        if q.delta is not None:
            d = abs(q.delta)
            if preset.delta_min is not None and d < preset.delta_min:
                _reject(
                    q, REASON_DELTA_TOO_LOW,
                    f"|Δ| {d:.2f} < min {preset.delta_min:.2f}",
                )
                continue
            if preset.delta_max is not None and d > preset.delta_max:
                _reject(
                    q, REASON_DELTA_TOO_HIGH,
                    f"|Δ| {d:.2f} > max {preset.delta_max:.2f}",
                )
                continue

        if preset.strike_max_vs_target is not None:
            if target_buy_price is None:
                # Misconfiguration: WANT_TO_OWN preset but no target. Surface
                # as wholesale empty (UI handles separately via fetch_reason).
                return [], []
            cap = target_buy_price * preset.strike_max_vs_target
            if q.strike > cap:
                _reject(
                    q, REASON_STRIKE_ABOVE_TARGET,
                    f"strike ${q.strike:g} > cap ${cap:.2f}"
                    f"（target ${target_buy_price:g} × {preset.strike_max_vs_target:g}）",
                )
                continue

        if preset.exclude_earnings_dte and earnings_dates:
            crossing = [e for e in earnings_dates if today < e <= q.expiry]
            if crossing:
                _reject(
                    q, REASON_CROSSES_EARNINGS,
                    f"earnings {crossing[0].isoformat()} 落在 DTE 区间内",
                )
                continue

        survivors.append(q)

    return survivors, rejections


def filter_chain(
    quotes: list[FilterableQuote],
    *,
    preset: IntentPreset,
    today: date,
    earnings_dates: list[date] | None = None,
    target_buy_price: float | None = None,
    weekly_ok: bool = False,
) -> list[FilterableQuote]:
    """Survivors-only convenience wrapper around :func:`filter_chain_with_reasons`.

    Quotes lacking delta when delta_max is set are *kept* (we can't penalize
    missing data; the advisor ranks them lower if ROC is similar).
    """
    survivors, _ = filter_chain_with_reasons(
        quotes,
        preset=preset,
        today=today,
        earnings_dates=earnings_dates,
        target_buy_price=target_buy_price,
        weekly_ok=weekly_ok,
    )
    return survivors
