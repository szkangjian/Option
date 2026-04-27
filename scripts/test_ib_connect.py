"""One-shot connection test against IB Gateway on port 4001.

Verifies: ib-async can connect, list accounts, fetch positions.
Read-only — no orders placed.
"""
import asyncio
from ib_async import IB


async def main() -> None:
    ib = IB()
    print("Connecting to 127.0.0.1:4001 ...")
    try:
        await ib.connectAsync("127.0.0.1", 4001, clientId=99, timeout=10)
        print(f"Connected. Server version: {ib.client.serverVersion()}")
        print(f"Managed accounts: {ib.managedAccounts()}")

        positions = ib.positions()
        print(f"\nTotal positions across all accounts: {len(positions)}")
        for p in positions[:20]:
            c = p.contract
            print(
                f"  [{p.account}] {c.secType:6s} {c.symbol:8s} "
                f"qty={p.position:>8.0f} avgCost={p.avgCost:>10.4f}"
            )
        if len(positions) > 20:
            print(f"  ... ({len(positions) - 20} more)")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
