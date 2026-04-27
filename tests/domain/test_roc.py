"""ROC math sanity checks."""
import pytest

from options_tool.domain.roc import (
    annualized_roc,
    annualized_roc_call,
    annualized_roc_put,
)


class TestCallROC:
    def test_basic(self):
        # Sell $1.00 call, stock at $50, 30 days to expiry
        # roc per period = 1/50 = 0.02
        # annualized = 0.02 * (365/30) = 0.2433...
        result = annualized_roc_call(premium_per_share=1.0, underlying_price=50.0, dte=30)
        assert result == pytest.approx(0.2433, abs=0.001)

    def test_zero_dte_returns_zero(self):
        assert annualized_roc_call(1.0, 50.0, 0) == 0.0

    def test_zero_underlying_returns_zero(self):
        assert annualized_roc_call(1.0, 0.0, 30) == 0.0

    def test_higher_premium_higher_roc(self):
        a = annualized_roc_call(0.50, 100.0, 30)
        b = annualized_roc_call(1.50, 100.0, 30)
        assert b > a

    def test_shorter_dte_higher_annualized(self):
        # Same premium, shorter DTE = higher annualized ROC
        a = annualized_roc_call(1.0, 100.0, 7)
        b = annualized_roc_call(1.0, 100.0, 45)
        assert a > b


class TestPutROC:
    def test_basic(self):
        # Sell $0.80 put, strike $40, 21 DTE
        # 0.80/40 = 0.02 * 365/21 = 0.3476
        result = annualized_roc_put(0.80, 40.0, 21)
        assert result == pytest.approx(0.3476, abs=0.001)

    def test_capital_basis_is_strike_not_spot(self):
        # CSP capital is the strike (cash collateral), not current spot
        assert annualized_roc_put(1.0, 50.0, 30) == pytest.approx(
            annualized_roc_call(1.0, 50.0, 30)
        )


class TestDispatch:
    def test_call_routes_to_call_func(self):
        result = annualized_roc(
            premium_per_share=1.0, side="CALL", underlying_price=50.0, strike=55.0, dte=30
        )
        # Should use underlying_price (50) not strike (55)
        assert result == annualized_roc_call(1.0, 50.0, 30)

    def test_put_routes_to_put_func(self):
        result = annualized_roc(
            premium_per_share=1.0, side="PUT", underlying_price=50.0, strike=45.0, dte=30
        )
        # Should use strike (45) not underlying_price (50)
        assert result == annualized_roc_put(1.0, 45.0, 30)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError):
            annualized_roc(
                premium_per_share=1.0, side="STRADDLE", underlying_price=50.0,
                strike=50.0, dte=30,
            )
