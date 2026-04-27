"""One-shot CSP scan for URA — compare strikes for post-5/15 re-entry.

Pulls URA PUT chain (DTE 28-90, strikes 48-58), prints per-contract
premium, Δ, annualized ROC, and effective cost-if-assigned. Read-only.

Anchor for comparison: rolling the CC60 5/15 to 7/17 @ 65 brings in
$+925 net credit on 10 lots; flat roll to 7/17 brings $+2,825.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

from options_tool.ibkr import MultiAccountClient, OptionQuote
from options_tool.settings import load_accounts

SYMBOL = "URA"
DTE_MIN, DTE_MAX = 28, 90
STRIKE_WINDOW_PCT = 0.20   # ±20% of spot
MAX_STRIKES_PER_SIDE = 20
STRIKE_FLOOR, STRIKE_CEIL = 48.0, 58.0


async def main() -> int:
    today = date.today()
    accounts = load_accounts().accounts
    if not accounts:
        print("No accounts configured.")
        return 1

    async with MultiAccountClient([accounts[0]]) as multi:
        client = multi.clients[0]
        quotes = await client.fetch_option_chain(
            SYMBOL,
            side="PUT",
            dte_min=DTE_MIN,
            dte_max=DTE_MAX,
            today=today,
            strike_window_pct=STRIKE_WINDOW_PCT,
            max_strikes_per_side=MAX_STRIKES_PER_SIDE,
        )

    if not quotes:
        print(f"No quotes returned for {SYMBOL}.")
        return 2

    spot = next((q.underlying_price for q in quotes if q.underlying_price), None)
    quotes = [
        q for q in quotes
        if STRIKE_FLOOR <= q.strike <= STRIKE_CEIL and q.right == "P"
    ]
    quotes.sort(key=lambda q: (q.expiry, q.strike))

    print(f"\n=== {SYMBOL} CSP Scan · {today} ===")
    print(f"spot: ${spot:.2f}" if spot else "spot: n/a")
    print(f"filters: PUT  DTE {DTE_MIN}–{DTE_MAX}  strike {STRIKE_FLOOR:g}–{STRIKE_CEIL:g}")
    print()

    print(
        f"{'expiry':12} {'DTE':>4} {'strike':>7} {'Δ':>6} "
        f"{'bid':>6} {'ask':>6} {'mid':>6} "
        f"{'eff_cost':>9} {'ann_ROC':>9} {'10-lot':>9}"
    )
    print("-" * 90)

    for q in quotes:
        dte = (q.expiry - today).days
        mid = q.mid
        d_s = f"{q.delta:+.2f}" if q.delta is not None else "   —"
        bid_s = f"{q.bid:.2f}" if q.bid else "  —"
        ask_s = f"{q.ask:.2f}" if q.ask else "  —"
        mid_s = f"{mid:.2f}" if mid else "  —"
        if mid is not None and mid > 0 and dte > 0:
            eff_cost = q.strike - mid
            ann_roc = (mid / q.strike) * (365.0 / dte)
            eff_s = f"${eff_cost:>6.2f}"
            roc_s = f"{ann_roc*100:>6.1f}%"
            lot_s = f"${mid*1000:>+6,.0f}"
        else:
            eff_s = "       —"
            roc_s = "       —"
            lot_s = "        —"
        print(
            f"{q.expiry.isoformat():12} {dte:>4} {q.strike:>7g} "
            f"{d_s:>6} {bid_s:>6} {ask_s:>6} {mid_s:>6} "
            f"{eff_s:>9} {roc_s:>9} {lot_s:>9}"
        )

    print()

    # Highlight by highest ann_ROC with |Δ| ≤ 0.30 (conservative CSP zone)
    picks: list[tuple[OptionQuote, float, float]] = []
    for q in quotes:
        if q.mid is None or q.mid <= 0 or q.delta is None:
            continue
        dte = (q.expiry - today).days
        if dte <= 0:
            continue
        if abs(q.delta) > 0.35:
            continue
        ann_roc = (q.mid / q.strike) * (365.0 / dte)
        picks.append((q, ann_roc, q.mid))
    picks.sort(key=lambda t: t[1], reverse=True)

    if picks:
        print("top 5 by annualized ROC (|Δ| ≤ 0.35):")
        for q, ann_roc, mid in picks[:5]:
            dte = (q.expiry - today).days
            total = mid * 1000
            print(
                f"  {q.expiry.isoformat()} P{q.strike:g}  "
                f"Δ{q.delta:+.2f}  mid ${mid:.2f}  "
                f"ann_ROC {ann_roc*100:.1f}%  10-lot premium ${total:+,.0f}"
            )
        print()

    print("anchor: roll up+out 7/17 @ 65 → net +$925 on 10 lots")
    print("anchor: roll flat   7/17 @ 60 → net +$2,825 on 10 lots")
    print()
    print("quotes = last prints (market closed). Re-run during US hours for live bids.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
