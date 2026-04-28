"""Debug the chain pull: show what reqTickers actually returns for URA."""
import asyncio
from datetime import date

from options_tool.ibkr import MultiAccountClient
from options_tool.logging_setup import configure_logging
from options_tool.settings import load_accounts


async def main() -> None:
    configure_logging()
    accounts = load_accounts().accounts
    async with MultiAccountClient([accounts[0]]) as multi:
        c = multi.clients[0]
        # Pull URA INCOME-window calls
        result = await c.fetch_option_chain(
            "URA",
            side="CALL",
            dte_min=30,
            dte_max=45,
            today=date(2026, 4, 19),
        )
        quotes = result.quotes

    print(f"Got {len(quotes)} quotes (spot={result.spot} reason={result.reason})\n")
    print(f"{'Expiry':<12} {'K':>7} {'R':<2} {'bid':>6} {'ask':>6} {'last':>6} "
          f"{'delta':>7} {'iv':>6} {'OI':>6}")
    for q in quotes:
        print(
            f"{q.expiry.isoformat():<12} {q.strike:>7.2f} {q.right:<2} "
            f"{q.bid if q.bid is not None else '-':>6} "
            f"{q.ask if q.ask is not None else '-':>6} "
            f"{q.last if q.last is not None else '-':>6} "
            f"{q.delta if q.delta is not None else '-':>7} "
            f"{q.iv if q.iv is not None else '-':>6} "
            f"{q.open_interest if q.open_interest is not None else '-':>6}"
        )


if __name__ == "__main__":
    asyncio.run(main())
