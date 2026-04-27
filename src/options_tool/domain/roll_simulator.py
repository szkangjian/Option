"""Roll simulator — given a short option flagged for ROLL, enumerate candidates.

A "roll" closes the current short (BTC) and opens a new short at a later expiry,
ideally for a net credit. This engine just scores the option chain against basic
defense-roll rules; the user decides which to execute in TWS.

Rules (first match wins per quote, then rank):

  * ``new_expiry > pos.expiry`` — must extend duration.
  * ``new_right == pos.right`` — P→P, C→C (a "roll" doesn't flip direction).
  * CC (``right == "C"``): ``new_strike >= pos.strike``  (up-and-out or flat-out).
    CSP (``right == "P"``): ``new_strike <= pos.strike`` (down-and-out or flat-out).
  * New leg Delta must sit *below* the warning threshold — else we'd roll straight
    into the next defense alert.
  * ``new_bid > 0`` — without a live bid there's no executable credit.

Net credit uses conservative sides: buy current back at its ask, sell new at its
bid. ``current_close_cost`` is supplied by the caller (whoever has the live quote
for the expiring leg). Net credit can be negative — the simulator reports what
the chain allows, the user decides.

Pure function. Consumes ``ShortPositionSnapshot`` + ``AlertsConfig`` (reusing the
same Delta threshold that triggered the ROLL advice in the first place), returns
``RollCandidate`` dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.settings import AlertsConfig


@dataclass(frozen=True, slots=True)
class RollQuote:
    """Minimal chain row the simulator needs. Narrower than ChainCache on purpose."""

    expiry: date
    strike: float
    right: str
    bid: float | None
    ask: float | None
    last: float | None
    delta: float | None


@dataclass(frozen=True, slots=True)
class RollCandidate:
    new_expiry: date
    new_strike: float
    new_dte: int
    new_delta: float | None
    close_cost: float   # per share (what we pay to BTC current short)
    open_credit: float  # per share (what we receive opening the new short)
    net_credit: float   # open_credit - close_cost (can be negative)
    strike_diff: float  # new_strike - pos.strike (signed — informative)


def simulate_rolls(
    pos: ShortPositionSnapshot,
    current_close_cost: float | None,
    quotes: list[RollQuote],
    today: date,
    config: AlertsConfig,
    *,
    top_n: int = 5,
    min_dte: int = 7,
    max_dte: int = 60,
) -> list[RollCandidate]:
    """Return up to ``top_n`` roll candidates sorted by net credit descending.

    ``current_close_cost`` is per-share: typically the current leg's ask (to be
    conservative) or its mid. If ``None``, returns ``[]`` — can't simulate a
    roll without knowing what we'd pay to close.
    """
    if current_close_cost is None or current_close_cost < 0:
        return []

    out: list[RollCandidate] = []
    for q in quotes:
        if q.right != pos.right:
            continue
        if q.expiry <= pos.expiry:
            continue
        if q.bid is None or q.bid <= 0:
            continue
        if pos.right == "C" and q.strike < pos.strike:
            continue
        if pos.right == "P" and q.strike > pos.strike:
            continue

        new_dte = (q.expiry - today).days
        if new_dte < min_dte or new_dte > max_dte:
            continue

        if q.delta is not None and abs(q.delta) >= config.delta_warning:
            continue

        open_credit = q.bid
        net = open_credit - current_close_cost
        out.append(
            RollCandidate(
                new_expiry=q.expiry,
                new_strike=q.strike,
                new_dte=new_dte,
                new_delta=q.delta,
                close_cost=current_close_cost,
                open_credit=open_credit,
                net_credit=net,
                strike_diff=q.strike - pos.strike,
            )
        )

    out.sort(key=lambda c: c.net_credit, reverse=True)
    return out[:top_n]


def format_roll_suggestion(
    pos: ShortPositionSnapshot,
    candidates: list[RollCandidate],
) -> str | None:
    """One-line roll summary for Telegram, or ``None`` if nothing to suggest.

    Picks the top candidate (already sorted by net credit). Format is compact
    so it appends cleanly to a delta-risk alert message:

        "  → roll: P48 2026-05-15 (DTE 25, Δ 0.18, net +0.32)"

    The leading two spaces + arrow are intentional — visually nests the
    suggestion under the alert it refines.
    """
    if not candidates:
        return None
    top = candidates[0]
    delta_str = f"{abs(top.new_delta):.2f}" if top.new_delta is not None else "—"
    sign = "+" if top.net_credit >= 0 else ""
    return (
        f"  → roll: {pos.right}{top.new_strike:g} {top.new_expiry.isoformat()} "
        f"(DTE {top.new_dte}, Δ {delta_str}, net {sign}{top.net_credit:.2f})"
    )
