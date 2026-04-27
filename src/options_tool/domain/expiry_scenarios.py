"""Expiry scenarios — "if held to expiry" arithmetic for a short option.

Pure function. No I/O. Given a short option's strike / premium and (for CC)
the underlying stock's cost basis, return:

  - OOM outcome: the premium is already banked; nothing changes.
  - Assigned outcome: effective per-share price + realized P&L vs basis.

CC assignments realize a full round-trip on the stock; CSP assignments
acquire new stock at the effective entry (strike − premium), so there's no
prior basis to realize against.

These numbers let the UI show "what if" side-by-side with Roll candidates
instead of forcing the user to do the arithmetic in their head. The
"assigned + next-cycle CSP" compound strategy is not modeled here — that
would need a second chain lookup and a strike choice; for now, the user
pairs this value with the CSP chain in the detail pane.
"""
from __future__ import annotations

from dataclasses import dataclass

CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True, slots=True)
class ExpiryScenario:
    """Per-contract outcomes if a short option is held to expiry.

    Values are expressed per share (``*_per_share``) and scaled to the full
    position size (``*_total``) for quick UI display.

    ``realized_per_share`` is populated only for CC when stock cost basis
    is known (round-trip closes out); CSP assignment opens a new lot with
    no prior basis to realize against, so it stays ``None``.
    """

    contracts: int                  # always positive (abs of qty)
    right: str                      # "C" | "P"
    effective_price_per_share: float  # sale price for CC; buy price for CSP
    realized_per_share: float | None  # CC only
    realized_total: float | None      # realized_per_share × contracts × 100
    oom_kept_per_share: float         # the premium we already received
    oom_kept_total: float             # oom_kept_per_share × contracts × 100
    note: str                         # short human-readable summary


def scenario_for_short(
    *,
    right: str,
    strike: float,
    qty: int,
    avg_open_price: float | None,
    stock_avg_cost: float | None = None,
) -> ExpiryScenario | None:
    """Compute the expiry outcome for one short option leg.

    Args:
      right: "C" or "P".
      strike: option strike.
      qty: signed qty; this function expects negative (short) — returns
        ``None`` for longs since the "assignment" framing doesn't apply.
      avg_open_price: per-share premium we received when selling to open.
      stock_avg_cost: underlying stock's average cost per share. Only used
        for CC (realizes a round-trip). Safe to pass ``None`` for CSP.

    Returns ``None`` when required inputs are missing.
    """
    if qty >= 0 or avg_open_price is None or avg_open_price <= 0:
        return None

    contracts = abs(int(qty))
    premium = avg_open_price
    oom_kept_per_share = premium
    oom_kept_total = premium * contracts * CONTRACT_MULTIPLIER

    if right.upper() == "C":
        # CC assigned: shares sold at strike; we keep the premium on top.
        effective = strike + premium
        if stock_avg_cost is None:
            realized_per_share = None
            realized_total = None
            note = (
                f"若行权：以 ${effective:.2f}/sh 卖出（strike + 已收 premium）"
            )
        else:
            realized_per_share = effective - stock_avg_cost
            realized_total = realized_per_share * contracts * CONTRACT_MULTIPLIER
            note = (
                f"若行权：以 ${effective:.2f}/sh 卖出 "
                f"→ 兑现 ${realized_per_share:+.2f}/sh "
                f"(vs 成本 ${stock_avg_cost:.2f})"
            )
        return ExpiryScenario(
            contracts=contracts,
            right="C",
            effective_price_per_share=effective,
            realized_per_share=realized_per_share,
            realized_total=realized_total,
            oom_kept_per_share=oom_kept_per_share,
            oom_kept_total=oom_kept_total,
            note=note,
        )

    # CSP assigned: shares bought at strike − premium_received effective.
    effective = strike - premium
    note = (
        f"若被接：以 ${effective:.2f}/sh 接货（strike − 已收 premium）"
    )
    return ExpiryScenario(
        contracts=contracts,
        right="P",
        effective_price_per_share=effective,
        realized_per_share=None,
        realized_total=None,
        oom_kept_per_share=oom_kept_per_share,
        oom_kept_total=oom_kept_total,
        note=note,
    )
