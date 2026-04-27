"""Return-on-capital math, annualized.

For a covered call:
  capital_at_risk ≈ underlying_price × 100  (one contract)
  premium = mid × 100
  roc = premium / capital_at_risk
  annualized = roc × (365 / dte)

For a cash-secured put:
  capital_at_risk = strike × 100  (cash you have to set aside)
  premium = mid × 100
  roc = premium / capital_at_risk
  annualized = roc × (365 / dte)

In both cases the per-share equivalent is identical, so we use simple
per-share math (premium per share / capital per share) and avoid the *100.
"""
from __future__ import annotations


def annualized_roc_call(
    premium_per_share: float, underlying_price: float, dte: int
) -> float:
    """Annualized ROC for a short call (covered call).

    Capital tied up = the stock you own × current price (opportunity cost).
    """
    if underlying_price <= 0 or dte <= 0:
        return 0.0
    return (premium_per_share / underlying_price) * (365.0 / dte)


def annualized_roc_put(
    premium_per_share: float, strike: float, dte: int
) -> float:
    """Annualized ROC for a short cash-secured put.

    Capital tied up = strike × 100 (cash collateral).
    """
    if strike <= 0 or dte <= 0:
        return 0.0
    return (premium_per_share / strike) * (365.0 / dte)


def annualized_roc(
    *,
    premium_per_share: float,
    side: str,                # "CALL" or "PUT"
    underlying_price: float,
    strike: float,
    dte: int,
) -> float:
    """Dispatch wrapper used by the advisor."""
    if side.upper() == "CALL":
        return annualized_roc_call(premium_per_share, underlying_price, dte)
    if side.upper() == "PUT":
        return annualized_roc_put(premium_per_share, strike, dte)
    raise ValueError(f"side must be CALL or PUT, got {side!r}")
