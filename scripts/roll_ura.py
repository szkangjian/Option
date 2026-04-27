"""One-shot roll simulation for URA CC60 2026-05-15.

Pulls a fresh CALL chain (DTE 14-120) via IB Gateway, locates the current
leg's close_cost from the same snapshot, runs ``domain.roll_simulator``,
prints top candidates. Read-only — no orders are sent.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date

from options_tool.domain.alert_detection import ShortPositionSnapshot
from options_tool.domain.roll_simulator import RollQuote, simulate_rolls
from options_tool.ibkr import MultiAccountClient, OptionQuote
from options_tool.settings import AlertsConfig, load_accounts

SYMBOL = "URA"
EXPIRY = date(2026, 5, 15)
STRIKE = 60.0
RIGHT = "C"
QTY = -10
AVG_OPEN = 0.9929

DTE_MIN, DTE_MAX = 14, 120
STRIKE_WINDOW_PCT = 0.22
MAX_STRIKES_PER_SIDE = 20
# Simulator drops rolls with |Δ| ≥ delta_warning. Relax from 0.40 default
# so flat-strike and slight up-and-out rolls (often Δ 0.40-0.55) show up.
DELTA_CEILING = 0.55
TOP_N = 15


def _to_rollquote(q: OptionQuote) -> RollQuote:
    return RollQuote(
        expiry=q.expiry,
        strike=q.strike,
        right=q.right,
        bid=q.bid,
        ask=q.ask,
        last=q.last,
        delta=q.delta,
    )


async def main() -> int:
    today = date.today()
    accounts = load_accounts().accounts
    if not accounts:
        print("No accounts configured in config/accounts.yaml")
        return 1

    async with MultiAccountClient([accounts[0]]) as multi:
        client = multi.clients[0]
        quotes = await client.fetch_option_chain(
            SYMBOL,
            side="CALL",
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

    current = next(
        (q for q in quotes
         if q.expiry == EXPIRY and q.strike == STRIKE and q.right == RIGHT),
        None,
    )
    if current is None:
        print(
            f"Could not locate {RIGHT}{STRIKE:g} {EXPIRY} in fetched chain — "
            f"adjust STRIKE_WINDOW_PCT or DTE bounds."
        )
        return 3

    close_cost_mid = current.mid
    close_cost_ask = current.ask

    print(f"\n=== {SYMBOL} Roll Simulation · {today} ===")
    print(f"spot          : ${spot:.2f}" if spot else "spot          : n/a")
    print(f"current leg   : {RIGHT}{STRIKE:g} {EXPIRY} qty {QTY}  open ${AVG_OPEN:.2f}/sh")
    bid_s = f"{current.bid:.2f}" if current.bid else "—"
    ask_s = f"{current.ask:.2f}" if current.ask else "—"
    last_s = f"{current.last:.2f}" if current.last else "—"
    d_s = f"{current.delta:+.2f}" if current.delta is not None else "—"
    print(f"current mkt   : bid={bid_s}  ask={ask_s}  last={last_s}  Δ={d_s}")
    if close_cost_mid is not None:
        pnl = (AVG_OPEN - close_cost_mid) * 1000
        print(f"close_cost    : mid=${close_cost_mid:.2f}  ask=${close_cost_ask or 0:.2f}")
        print(f"unreal P&L    : ${AVG_OPEN - close_cost_mid:+.2f}/sh × 1000 = ${pnl:+,.0f}")
    else:
        print("close_cost    : mid unavailable (bid/ask missing) — aborting.")
        return 4

    print()
    print(
        f"filters       : DTE {DTE_MIN}-{DTE_MAX}  new_strike ≥ {STRIKE:g}  "
        f"|Δ| < {DELTA_CEILING}  rank=net_credit(mid)"
    )
    print()

    candidates = simulate_rolls(
        pos=ShortPositionSnapshot(
            symbol=SYMBOL, right=RIGHT, strike=STRIKE, expiry=EXPIRY,
            qty=QTY, avg_open_price=AVG_OPEN,
            current_mark=close_cost_mid, current_delta=current.delta,
        ),
        current_close_cost=close_cost_mid,
        quotes=[_to_rollquote(q) for q in quotes if q is not current],
        today=today,
        config=AlertsConfig(delta_warning=DELTA_CEILING),
        top_n=TOP_N,
        min_dte=DTE_MIN,
        max_dte=DTE_MAX,
    )

    if not candidates:
        print("No viable roll candidates.")
        print("Try: raise DELTA_CEILING, widen STRIKE_WINDOW_PCT, or market closed → stale bids.")
        return 0

    print(
        f"{'expiry':12} {'DTE':>4} {'strike':>7} {'Δstk':>5} {'Δ':>6} "
        f"{'close':>7} {'open':>7} {'net':>8} {'10-lot':>10}"
    )
    print("-" * 76)
    for c in candidates:
        d_str = f"{c.new_delta:.2f}" if c.new_delta is not None else "   —"
        total = c.net_credit * 1000
        sign = "+" if c.net_credit >= 0 else ""
        print(
            f"{c.new_expiry.isoformat():12} {c.new_dte:>4} "
            f"{c.new_strike:>7g} {c.strike_diff:>+5g} {d_str:>6} "
            f"${c.close_cost:>5.2f} ${c.open_credit:>5.2f} "
            f"{sign}${c.net_credit:>5.2f} ${total:>+8,.0f}"
        )

    print()
    flat = [c for c in candidates if c.strike_diff == 0]
    up = [c for c in candidates if c.strike_diff > 0]
    if flat:
        f = flat[0]
        print(
            f"best flat    : {f.new_expiry} @ {f.new_strike:g}  "
            f"net ${f.net_credit:+.2f}/sh  (${f.net_credit*1000:+,.0f} on 10 lots)"
        )
    if up:
        u = up[0]
        print(
            f"best up+out  : {u.new_expiry} @ {u.new_strike:g} (+{u.strike_diff:g})  "
            f"net ${u.net_credit:+.2f}/sh  (${u.net_credit*1000:+,.0f} on 10 lots)"
        )

    print()
    print("quotes = last prints; re-run during US market hours for live bids.")
    print("close_cost uses mid. Conservative fill: assume BTC at ask, so")
    print(f"subtract ${(close_cost_ask or 0) - close_cost_mid:.2f}/sh from net credits shown.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
