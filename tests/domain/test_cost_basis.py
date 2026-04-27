"""Adjusted cost basis tests."""
from datetime import datetime

from options_tool.domain.cost_basis import TxLeg, adjusted_cost_per_share, net_premium_credit


def opt(action: str, qty: float, price: float, comm: float = 1.0) -> TxLeg:
    return TxLeg(
        asset_type="OPTION",
        action=action,
        qty=qty,
        price=price,
        commission=comm,
        executed_at=datetime(2026, 1, 1),
    )


class TestNetPremiumCredit:
    def test_single_short_open(self):
        # STO 1 contract at $1.00 = $100 credit, less $1 commission
        result = net_premium_credit([opt("STO", 1, 1.00)])
        assert result == 100.0 - 1.0

    def test_short_open_then_close_for_profit(self):
        # STO 1 at $2.00 (+$200), BTC 1 at $0.50 (-$50). Net +$150 less 2x commission.
        legs = [opt("STO", 1, 2.00), opt("BTC", 1, 0.50)]
        assert net_premium_credit(legs) == (200 - 50) - 2

    def test_short_open_then_close_at_loss(self):
        # Roll losing call: STO 1 at $1, BTC 1 at $3. Net -$200 (- commissions).
        legs = [opt("STO", 1, 1.00), opt("BTC", 1, 3.00)]
        assert net_premium_credit(legs) == (100 - 300) - 2

    def test_stock_legs_ignored(self):
        legs = [
            TxLeg("STOCK", "BUY", 100, 50.0, 1.0, datetime(2026, 1, 1)),
            opt("STO", 1, 1.50),
        ]
        # Stock leg shouldn't contribute to premium
        assert net_premium_credit(legs) == 150 - 1.0

    def test_empty_returns_zero(self):
        assert net_premium_credit([]) == 0.0


class TestAdjustedCostPerShare:
    def test_basis_lowered_by_premium(self):
        # Bought 100 shares at $50 (basis $50). Collected $200 net premium.
        # Adjusted basis = 50 - 2.00 = $48
        result = adjusted_cost_per_share(
            raw_avg_cost=50.0,
            shares_held=100,
            option_legs=[opt("STO", 1, 2.01)],  # 201 - 1 comm = 200 net
        )
        assert result == 48.0

    def test_basis_raised_by_net_debit(self):
        # Bad roll: collected $100 STO, paid $300 BTC. Net -$200 over 100 shares.
        # Basis goes UP by $2.
        result = adjusted_cost_per_share(
            raw_avg_cost=50.0,
            shares_held=100,
            option_legs=[opt("STO", 1, 1.00, comm=0), opt("BTC", 1, 3.00, comm=0)],
        )
        assert result == 52.0

    def test_zero_shares_returns_raw(self):
        # Avoid division by zero when position has been fully sold
        result = adjusted_cost_per_share(
            raw_avg_cost=50.0, shares_held=0,
            option_legs=[opt("STO", 1, 1.0)],
        )
        assert result == 50.0
