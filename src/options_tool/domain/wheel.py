"""Wheel intent auto-flip — detect assignments by diffing position snapshots.

The wheel strategy alternates CSP (cash-secured put) ↔ CC (covered call) on the
same underlying:

  * CSP gets assigned → you now own 100 shares per contract → write CC against
    them → intent should become ``INCOME``.
  * CC gets exercised → your shares are called away → write a new CSP to
    re-acquire at a target price → intent should become ``WANT_TO_OWN``.

Detection is by *position diff* between two sync snapshots:

  1. A short option that existed in the prior snapshot is missing in the new
     snapshot. (It either expired, was assigned, was bought-to-close, or rolled.)
  2. The stock qty in the *same account* moved in the direction consistent with
     assignment: +100×|qty| for CSP, −100×|qty| for CC.

If both signals line up *and* the symbol has ``wheel_enabled``, it's an
assignment event and the caller should flip ``intent``.

Why not look at the Transaction ledger? IBKR's ``reqExecutions`` only shows
the current trading day, and assignment fills sometimes don't appear in the
exec stream until T+1. Diffing positions is more robust — it works as long as
two consecutive syncs straddle the assignment.

This module is pure — no DB, no IBKR. Caller (``options_tool.sync``) supplies
snapshots as plain dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class OptionSnapshot:
    account_code: str
    symbol: str
    right: str    # "C" | "P"
    strike: float
    expiry: date
    qty: int      # negative for short legs


@dataclass(frozen=True, slots=True)
class StockSnapshot:
    account_code: str
    symbol: str
    qty: float


@dataclass(frozen=True, slots=True)
class AssignmentEvent:
    """One detected assignment / exercise."""

    account_code: str
    symbol: str
    right: str           # "C" or "P"
    strike: float
    expiry: date
    contracts: int       # positive: how many contracts were assigned
    kind: str            # "csp_assigned" | "cc_exercised"
    new_intent: str      # what we're flipping to ("INCOME" | "WANT_TO_OWN")


def _key(opt: OptionSnapshot) -> tuple:
    return (opt.account_code, opt.symbol, opt.right, opt.strike, opt.expiry)


def _stock_qty_delta(
    prior_stocks: list[StockSnapshot],
    current_stocks: list[StockSnapshot],
    account_code: str,
    symbol: str,
) -> float:
    """Net change in stock qty for a (account, symbol) pair."""
    prior = sum(
        s.qty for s in prior_stocks
        if s.account_code == account_code and s.symbol == symbol
    )
    current = sum(
        s.qty for s in current_stocks
        if s.account_code == account_code and s.symbol == symbol
    )
    return current - prior


def detect_assignments(
    prior_opts: list[OptionSnapshot],
    prior_stocks: list[StockSnapshot],
    current_opts: list[OptionSnapshot],
    current_stocks: list[StockSnapshot],
    wheel_symbols: set[str],
) -> list[AssignmentEvent]:
    """Diff two snapshots and emit one ``AssignmentEvent`` per detected case.

    Only short legs (``qty < 0``) of symbols in ``wheel_symbols`` are
    considered — non-wheel symbols don't trigger flips and aren't worth
    surfacing here.

    A "disappearance" alone isn't enough — we require the matching stock
    delta to rule out manual buy-to-close and rolls.
    """
    current_keys = {_key(o) for o in current_opts}

    events: list[AssignmentEvent] = []
    for prior in prior_opts:
        if prior.symbol not in wheel_symbols:
            continue
        if prior.qty >= 0:
            continue  # only short legs assign
        if _key(prior) in current_keys:
            continue  # still open

        contracts = abs(prior.qty)
        expected_share_move = 100 * contracts
        delta = _stock_qty_delta(
            prior_stocks, current_stocks,
            prior.account_code, prior.symbol,
        )

        if prior.right == "P" and delta >= expected_share_move - 0.5:
            events.append(
                AssignmentEvent(
                    account_code=prior.account_code,
                    symbol=prior.symbol,
                    right=prior.right,
                    strike=prior.strike,
                    expiry=prior.expiry,
                    contracts=contracts,
                    kind="csp_assigned",
                    new_intent="INCOME",
                )
            )
        elif prior.right == "C" and delta <= -(expected_share_move - 0.5):
            events.append(
                AssignmentEvent(
                    account_code=prior.account_code,
                    symbol=prior.symbol,
                    right=prior.right,
                    strike=prior.strike,
                    expiry=prior.expiry,
                    contracts=contracts,
                    kind="cc_exercised",
                    new_intent="WANT_TO_OWN",
                )
            )

    return events
