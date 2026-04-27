"""Premium-adjusted cost basis math.

For a given (account, symbol), starting from the average cost reported by IBKR,
we subtract net option premium credits collected on that symbol to get the
"adjusted cost basis" — what each share effectively cost you after the rent
you've collected.

This is intentionally I/O-free: callers pass in the relevant transactions and
the current share count.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TxLeg:
    """Subset of fields from ``transactions`` used by the basis calc."""

    asset_type: str       # "STOCK" or "OPTION"
    action: str           # BTO/STO/BTC/STC/BUY/SELL/...
    qty: float
    price: float          # per share
    commission: float
    executed_at: datetime


def net_premium_credit(option_legs: list[TxLeg]) -> float:
    """Sum signed premium across option legs (positive = credit to you).

    STO and STC are credits when *opening* a short; BTC and BTO are debits.
    To handle wheel/roll cleanly we use simple sign rules:
      - SELL-side actions (STO, STC, SELL) → +qty * price * 100
      - BUY-side actions (BTO, BTC, BUY)   → -qty * price * 100
    Commission is always a deduction.
    """
    sell_actions = {"STO", "STC", "SELL"}
    buy_actions = {"BTO", "BTC", "BUY"}
    total = 0.0
    for leg in option_legs:
        if leg.asset_type != "OPTION":
            continue
        notional = leg.qty * leg.price * 100.0
        if leg.action in sell_actions:
            total += notional
        elif leg.action in buy_actions:
            total -= notional
        # Other actions (ASSIGN, EXPIRE) generate no premium.
        total -= leg.commission
    return total


def adjusted_cost_per_share(
    *,
    raw_avg_cost: float,
    shares_held: float,
    option_legs: list[TxLeg],
) -> float:
    """The headline number: cost per share after subtracting collected premium.

    Returns ``raw_avg_cost`` unchanged when ``shares_held`` is zero (avoid div0).
    """
    if shares_held <= 0:
        return raw_avg_cost
    credit = net_premium_credit(option_legs)
    return raw_avg_cost - (credit / shares_held)
