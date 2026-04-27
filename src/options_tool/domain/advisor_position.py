"""Position Advisor — per open short option, recommend Close / Hold / Roll.

Pure function. Reuses ``ShortPositionSnapshot`` and ``AlertsConfig`` so the
thresholds match what alert_detection pushes via Telegram — the Web Action
column and the chat ping read from the same definition.

Decision priority (first match wins):

  1. EARNINGS_CONFLICT in remaining DTE → ROLL away from earnings.
  2. PROFIT_80                          → CLOSE (lock the big profit).
  3. STOP_LOSS (mark ≥ N× premium)      → STOP_LOSS (signal, user decides).
  4. DELTA_CRIT                         → ROLL up/out for defense.
  5. PROFIT_50 + short DTE (≤ 7)        → CLOSE (marginal extra not worth carry).
  6. DELTA_WARN + short DTE (≤ 7)       → ROLL out for breathing room.
  7. else                               → HOLD.

STOP_LOSS sits above DELTA_CRIT because if the mark has doubled, rolling for
a small credit is rearranging deck chairs — the trade is already underwater
past the standard risk budget. We surface it as its own label (not CLOSE) so
the UI signals "you're losing, decide" rather than "great profit, lock it".

The labels are advisory text only — no order generation, no Roll candidate
search (that's the P3 Roll simulator).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.settings import AlertsConfig

# DTE under which a "marginal" 50% profit / Delta warn flips into action.
SHORT_DTE_THRESHOLD = 7

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True, slots=True)
class PositionAdvice:
    label: str        # "CLOSE" | "HOLD" | "ROLL" | "STOP_LOSS"
    reason: str       # one-line Chinese rationale, safe to render in HTML
    severity: str     # "info" | "warn" | "critical" — drives row color
    # Quantitative context — populated from the snapshot so the UI / CLI can
    # render a "why" panel without recomputing. All fields are optional because
    # mark / delta / premium may not be cached yet for a freshly opened leg.
    dte: int = 0
    contracts: int = 0
    decay_pct: float | None = None        # 0.0 – 1.0, fraction of premium decayed
    delta_abs: float | None = None
    premium_open_total: float | None = None   # $ collected at open
    current_value_total: float | None = None  # $ it would cost to close now
    pnl_unrealized: float | None = None       # $ P&L if closed at current mark
    remaining_max_profit: float | None = None  # $ still on the table if held to 0


def _decay(pos: ShortPositionSnapshot) -> float | None:
    if pos.avg_open_price is None or pos.current_mark is None:
        return None
    if pos.avg_open_price <= 0 or pos.current_mark < 0:
        return None
    return 1.0 - (pos.current_mark / pos.avg_open_price)


def _dollars(pos: ShortPositionSnapshot) -> dict[str, float | int | None]:
    """Compute $ context for a short leg. ``contracts`` is abs(qty)."""
    contracts = abs(int(pos.qty))
    premium_open_total = None
    current_value_total = None
    pnl_unrealized = None
    remaining_max_profit = None
    if pos.avg_open_price is not None and pos.avg_open_price > 0:
        premium_open_total = pos.avg_open_price * contracts * CONTRACT_MULTIPLIER
    if pos.current_mark is not None and pos.current_mark >= 0:
        current_value_total = pos.current_mark * contracts * CONTRACT_MULTIPLIER
    if premium_open_total is not None and current_value_total is not None:
        # Short: profit = premium received - cost to buy back.
        pnl_unrealized = premium_open_total - current_value_total
        remaining_max_profit = current_value_total  # what we'd keep if mark → 0
    return {
        "contracts": contracts,
        "premium_open_total": premium_open_total,
        "current_value_total": current_value_total,
        "pnl_unrealized": pnl_unrealized,
        "remaining_max_profit": remaining_max_profit,
    }


def _fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


def _earnings_in_window(
    pos: ShortPositionSnapshot,
    earnings_dates: list[date],
    today: date,
    window_days: int,
) -> date | None:
    """Return the offending earnings date (if any) within window of expiry."""
    if not earnings_dates:
        return None
    if pos.expiry < today:
        return None
    for e in earnings_dates:
        if today < e <= pos.expiry:
            return e
        if abs((pos.expiry - e).days) <= window_days:
            return e
    return None


def advise_position(
    pos: ShortPositionSnapshot,
    earnings_dates: list[date],
    today: date,
    config: AlertsConfig,
) -> PositionAdvice:
    """Score a single short option leg into one of CLOSE / HOLD / ROLL.

    ``earnings_dates`` is the list of *future* earnings dates for the symbol
    (caller-filtered to ``>= today``); pass ``[]`` if none known.
    """
    dte = (pos.expiry - today).days
    decay = _decay(pos)
    delta = abs(pos.current_delta) if pos.current_delta is not None else None
    dollars = _dollars(pos)

    def _advice(label: str, reason: str, severity: str) -> PositionAdvice:
        return PositionAdvice(
            label=label,
            reason=reason,
            severity=severity,
            dte=dte,
            contracts=dollars["contracts"],  # type: ignore[arg-type]
            decay_pct=decay,
            delta_abs=delta,
            premium_open_total=dollars["premium_open_total"],  # type: ignore[arg-type]
            current_value_total=dollars["current_value_total"],  # type: ignore[arg-type]
            pnl_unrealized=dollars["pnl_unrealized"],  # type: ignore[arg-type]
            remaining_max_profit=dollars["remaining_max_profit"],  # type: ignore[arg-type]
        )

    pnl_tag = (
        f"（浮盈 {_fmt_money(dollars['pnl_unrealized'])}）"
        if dollars["pnl_unrealized"] is not None
        else ""
    )
    earnings_hit = _earnings_in_window(
        pos, earnings_dates, today, config.earnings_conflict_dte
    )
    if earnings_hit is not None:
        return _advice(
            "ROLL",
            f"📅 与 {earnings_hit} 财报冲突（距到期 {dte}d）→ 建议 roll 到财报后",
            "warn",
        )

    if config.profit_take_80 and decay is not None and decay >= 0.80:
        return _advice(
            "CLOSE",
            (
                f"💰 已衰减 {decay * 100:.0f}% {pnl_tag}→ 锁定大部分利润，"
                f"剩余 {_fmt_money(dollars['remaining_max_profit'])} 不值再扛 {dte}d"
            ),
            "info",
        )

    # STOP_LOSS: mark grew to N× the premium received → standard defensive
    # signal. Severity is warn (not critical) because for wheel-mode users the
    # right call is often "ride into assignment" — surface it, don't dictate.
    if (
        config.stop_loss_multiplier > 0
        and pos.avg_open_price is not None
        and pos.current_mark is not None
        and pos.avg_open_price > 0
        and pos.current_mark > 0
    ):
        ratio = pos.current_mark / pos.avg_open_price
        if ratio >= config.stop_loss_multiplier:
            return _advice(
                "STOP_LOSS",
                (
                    f"🛑 已亏 {(ratio - 1) * 100:.0f}% "
                    f"(开仓 {pos.avg_open_price:.2f} → 当前 {pos.current_mark:.2f}, "
                    f"亏损 {_fmt_money(dollars['pnl_unrealized'])}, "
                    f"≥ {config.stop_loss_multiplier:g}× 阈值) → 自行决定止损或继续持仓"
                ),
                "warn",
            )

    if delta is not None and delta >= config.delta_critical:
        return _advice(
            "ROLL",
            (
                f"🔴 Delta {delta:.2f} ≥ {config.delta_critical:.2f} "
                f"(critical){pnl_tag} → roll up/out 防被行权"
            ),
            "critical",
        )

    if (
        config.profit_take_50
        and decay is not None
        and decay >= 0.50
        and dte <= SHORT_DTE_THRESHOLD
    ):
        return _advice(
            "CLOSE",
            (
                f"💵 已衰减 {decay * 100:.0f}% 且仅剩 {dte}d {pnl_tag}→ "
                f"边际收益小，平仓释放保证金"
            ),
            "info",
        )

    if (
        delta is not None
        and delta >= config.delta_warning
        and dte <= SHORT_DTE_THRESHOLD
    ):
        return _advice(
            "ROLL",
            (
                f"🟠 Delta {delta:.2f} ≥ {config.delta_warning:.2f} "
                f"且仅剩 {dte}d{pnl_tag} → roll out 争取时间"
            ),
            "warn",
        )

    bits = [f"DTE {dte}d"]
    if decay is not None:
        bits.append(f"已衰减 {decay * 100:.0f}%")
    if delta is not None:
        bits.append(f"Δ={delta:.2f}")
    if dollars["pnl_unrealized"] is not None:
        bits.append(f"浮盈 {_fmt_money(dollars['pnl_unrealized'])}")
    return _advice("HOLD", " · ".join(bits) + " · 维持现状", "info")
