"""Pure alert detection.

Takes dataclass snapshots of positions / candidates / earnings, returns
``AlertCandidate`` rows. No I/O, no DB, no Telegram — the caller composes
detection with persistence and dispatch.

Detection covers four types (IV spike deferred until iv_history accrues):

  PROFIT_50 / PROFIT_80
      Short option's current mark has decayed to 50% / 20% of open premium.
      Signal to consider buy-to-close.

  DELTA_WARN / DELTA_CRIT
      Short option's |delta| crossed a risk threshold — assignment risk.

  EARNINGS_CONFLICT
      Existing open option expires within N days of earnings. Should have
      been filtered at open time; this catches earnings rescheduling.

  OPPORTUNITY
      Top advisor candidate has annualized ROC above the push threshold.
      The "something juicy appeared" ping.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_tool.settings import AlertsConfig


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    alert_key: str
    alert_type: str
    symbol: str
    message: str
    severity: str  # "info" | "warn" | "critical"


@dataclass(frozen=True, slots=True)
class ShortPositionSnapshot:
    """Snapshot of one short option leg plus its current mark / delta."""

    symbol: str
    right: str
    strike: float
    expiry: date
    qty: int  # always negative for "short"
    avg_open_price: float | None
    current_mark: float | None
    current_delta: float | None


@dataclass(frozen=True, slots=True)
class OpportunitySnapshot:
    """Top-ranked advisor candidate, stripped to the fields detection cares about."""

    symbol: str
    right: str
    strike: float
    expiry: date
    dte: int
    premium: float
    annualized_roc: float
    rank: int


def _leg_key(kind: str, pos: ShortPositionSnapshot) -> str:
    return f"{kind}:{pos.symbol}:{pos.right}:{pos.strike:g}:{pos.expiry.isoformat()}"


def detect_profit_take(
    pos: ShortPositionSnapshot, config: AlertsConfig
) -> AlertCandidate | None:
    """Short leg decayed 50% / 80% → time to consider BTC.

    Fires the tighter threshold when both apply — 80% subsumes 50%.
    """
    if pos.avg_open_price is None or pos.current_mark is None:
        return None
    if pos.avg_open_price <= 0 or pos.current_mark < 0:
        return None

    decay = 1.0 - (pos.current_mark / pos.avg_open_price)
    if config.profit_take_80 and decay >= 0.80:
        return AlertCandidate(
            alert_key=_leg_key("PROFIT_80", pos),
            alert_type="PROFIT_80",
            symbol=pos.symbol,
            message=(
                f"💰 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
                f"已衰减 {decay * 100:.0f}% → 可考虑平仓 "
                f"(开仓 {pos.avg_open_price:.2f} → 当前 {pos.current_mark:.2f})"
            ),
            severity="info",
        )
    if config.profit_take_50 and decay >= 0.50:
        return AlertCandidate(
            alert_key=_leg_key("PROFIT_50", pos),
            alert_type="PROFIT_50",
            symbol=pos.symbol,
            message=(
                f"💵 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
                f"已衰减 {decay * 100:.0f}% "
                f"(开仓 {pos.avg_open_price:.2f} → 当前 {pos.current_mark:.2f})"
            ),
            severity="info",
        )
    return None


def detect_stop_loss(
    pos: ShortPositionSnapshot, config: AlertsConfig
) -> AlertCandidate | None:
    """Short leg's mark grew to N× the premium we received → defensive stop.

    Tastytrade-canonical "2× credit received" rule: the position is now down
    100%+ on the premium we sold. Whether you cut or ride is your call (wheel
    folks often ride into assignment) — this is a *signal*, not a verdict.

    Disabled when ``stop_loss_multiplier <= 0`` or open price unknown.
    """
    if config.stop_loss_multiplier <= 0:
        return None
    if pos.avg_open_price is None or pos.current_mark is None:
        return None
    if pos.avg_open_price <= 0 or pos.current_mark <= 0:
        return None

    ratio = pos.current_mark / pos.avg_open_price
    if ratio < config.stop_loss_multiplier:
        return None
    return AlertCandidate(
        alert_key=_leg_key("STOP_LOSS", pos),
        alert_type="STOP_LOSS",
        symbol=pos.symbol,
        message=(
            f"🛑 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
            f"已亏 {(ratio - 1) * 100:.0f}% "
            f"(开仓 {pos.avg_open_price:.2f} → 当前 {pos.current_mark:.2f}, "
            f"≥ {config.stop_loss_multiplier:g}× 阈值) → 考虑止损"
        ),
        severity="warn",
    )


def detect_delta_risk(
    pos: ShortPositionSnapshot, config: AlertsConfig
) -> AlertCandidate | None:
    """|Delta| crossed warn / critical threshold. Critical wins when both apply."""
    if pos.current_delta is None:
        return None
    d = abs(pos.current_delta)
    if d >= config.delta_critical:
        return AlertCandidate(
            alert_key=_leg_key("DELTA_CRIT", pos),
            alert_type="DELTA_CRIT",
            symbol=pos.symbol,
            message=(
                f"🔴 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
                f"Delta={d:.2f} 触发 critical ({config.delta_critical:.2f})"
            ),
            severity="critical",
        )
    if d >= config.delta_warning:
        return AlertCandidate(
            alert_key=_leg_key("DELTA_WARN", pos),
            alert_type="DELTA_WARN",
            symbol=pos.symbol,
            message=(
                f"🟠 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
                f"Delta={d:.2f} 触发 warn ({config.delta_warning:.2f})"
            ),
            severity="warn",
        )
    return None


def detect_earnings_conflict(
    pos: ShortPositionSnapshot,
    earnings_dates: list[date],
    today: date,
    config: AlertsConfig,
) -> AlertCandidate | None:
    """Open short option expires within ``earnings_conflict_dte`` days of earnings."""
    if not earnings_dates:
        return None
    dte_exp = (pos.expiry - today).days
    if dte_exp < 0:
        return None
    for e in earnings_dates:
        gap = abs((pos.expiry - e).days)
        within = today < e <= pos.expiry or gap <= config.earnings_conflict_dte
        if within:
            return AlertCandidate(
                alert_key=_leg_key("EARNINGS_CONFLICT", pos),
                alert_type="EARNINGS_CONFLICT",
                symbol=pos.symbol,
                message=(
                    f"📅 {pos.symbol} {pos.right}{pos.strike:g} {pos.expiry} "
                    f"与 {e} 财报冲突（距到期 {dte_exp}d，距财报 {(e - today).days}d）"
                ),
                severity="warn",
            )
    return None


def detect_opportunity(
    cand: OpportunitySnapshot, config: AlertsConfig
) -> AlertCandidate | None:
    """Surface advisor top candidate with ROC above push threshold."""
    if cand.annualized_roc < config.roc_threshold_annual:
        return None
    return AlertCandidate(
        alert_key=(
            f"OPPORTUNITY:{cand.symbol}:{cand.right}:{cand.strike:g}:"
            f"{cand.expiry.isoformat()}"
        ),
        alert_type="OPPORTUNITY",
        symbol=cand.symbol,
        message=(
            f"🎯 {cand.symbol} {cand.right}{cand.strike:g} {cand.expiry} "
            f"年化 ROC {cand.annualized_roc * 100:.1f}% · "
            f"premium {cand.premium:.2f} · DTE {cand.dte}"
        ),
        severity="info",
    )
