"""Pure-function tests for alert detection."""
from datetime import date, timedelta

from options_tool.domain.alert_detection import (
    OpportunitySnapshot,
    ShortPositionSnapshot,
    detect_delta_risk,
    detect_earnings_conflict,
    detect_opportunity,
    detect_profit_take,
    detect_stop_loss,
)
from options_tool.settings import AlertsConfig


TODAY = date(2026, 4, 19)
EXPIRY = TODAY + timedelta(days=30)
CFG = AlertsConfig()  # all defaults


def short(
    *,
    avg: float | None = 1.00,
    mark: float | None = None,
    delta: float | None = None,
) -> ShortPositionSnapshot:
    return ShortPositionSnapshot(
        symbol="TEST", right="C", strike=100.0, expiry=EXPIRY, qty=-1,
        avg_open_price=avg, current_mark=mark, current_delta=delta,
    )


class TestProfitTake:
    def test_50_percent_decay_fires_50(self):
        a = detect_profit_take(short(avg=1.00, mark=0.50), CFG)
        assert a is not None and a.alert_type == "PROFIT_50"

    def test_80_percent_decay_fires_80(self):
        a = detect_profit_take(short(avg=1.00, mark=0.20), CFG)
        assert a is not None and a.alert_type == "PROFIT_80"

    def test_under_50_no_alert(self):
        assert detect_profit_take(short(avg=1.00, mark=0.60), CFG) is None

    def test_missing_data_no_alert(self):
        assert detect_profit_take(short(avg=None, mark=0.5), CFG) is None
        assert detect_profit_take(short(avg=1.0, mark=None), CFG) is None

    def test_zero_avg_no_alert(self):
        # Avoid div-by-zero; treat as missing
        assert detect_profit_take(short(avg=0.0, mark=0.0), CFG) is None


class TestDeltaRisk:
    def test_critical_threshold(self):
        a = detect_delta_risk(short(delta=-0.55), CFG)
        assert a is not None and a.alert_type == "DELTA_CRIT"

    def test_warn_threshold(self):
        a = detect_delta_risk(short(delta=0.42), CFG)
        assert a is not None and a.alert_type == "DELTA_WARN"

    def test_below_warn(self):
        assert detect_delta_risk(short(delta=0.30), CFG) is None

    def test_missing_delta(self):
        assert detect_delta_risk(short(delta=None), CFG) is None

    def test_uses_abs_for_puts(self):
        # Put delta is negative — must compare on abs.
        a = detect_delta_risk(short(delta=-0.45), CFG)
        assert a is not None and a.alert_type == "DELTA_WARN"


class TestEarningsConflict:
    def test_earnings_inside_dte_window(self):
        # Earnings 5 days out, expiry 30 days out → conflict
        earnings = [TODAY + timedelta(days=5)]
        a = detect_earnings_conflict(short(), earnings, TODAY, CFG)
        assert a is not None

    def test_earnings_close_to_expiry(self):
        # Earnings 3 days after expiry → within ±earnings_conflict_dte (default 7)
        earnings = [EXPIRY + timedelta(days=3)]
        a = detect_earnings_conflict(short(), earnings, TODAY, CFG)
        assert a is not None

    def test_no_earnings(self):
        assert detect_earnings_conflict(short(), [], TODAY, CFG) is None

    def test_far_away_earnings_skipped(self):
        # 60 days out, expiry 30 days out → no overlap, gap=30 > 7 → no alert
        earnings = [TODAY + timedelta(days=60)]
        assert detect_earnings_conflict(short(), earnings, TODAY, CFG) is None

    def test_expired_position_no_alert(self):
        pos = ShortPositionSnapshot(
            symbol="TEST", right="C", strike=100.0,
            expiry=TODAY - timedelta(days=1), qty=-1,
            avg_open_price=1.0, current_mark=None, current_delta=None,
        )
        earnings = [TODAY + timedelta(days=5)]
        assert detect_earnings_conflict(pos, earnings, TODAY, CFG) is None


class TestOpportunity:
    def _opp(self, roc: float) -> OpportunitySnapshot:
        return OpportunitySnapshot(
            symbol="TEST", right="C", strike=100.0, expiry=EXPIRY,
            dte=30, premium=1.0, annualized_roc=roc, rank=1,
        )

    def test_above_threshold_fires(self):
        a = detect_opportunity(self._opp(0.50), CFG)  # default 0.40
        assert a is not None and a.alert_type == "OPPORTUNITY"

    def test_below_threshold_skipped(self):
        assert detect_opportunity(self._opp(0.30), CFG) is None


class TestStopLoss:
    """2× premium received → stop-loss signal (tastytrade-canonical)."""

    def test_at_2x_fires(self):
        a = detect_stop_loss(short(avg=1.00, mark=2.00), CFG)
        assert a is not None
        assert a.alert_type == "STOP_LOSS"
        assert a.severity == "warn"
        assert "100%" in a.message  # mark = 2× → +100% loss

    def test_above_2x_fires(self):
        a = detect_stop_loss(short(avg=1.00, mark=2.50), CFG)
        assert a is not None and a.alert_type == "STOP_LOSS"
        assert "150%" in a.message

    def test_just_under_2x_skipped(self):
        assert detect_stop_loss(short(avg=1.00, mark=1.99), CFG) is None

    def test_profitable_position_skipped(self):
        # Mark below open price = winning trade, never stop-loss territory
        assert detect_stop_loss(short(avg=1.00, mark=0.50), CFG) is None

    def test_missing_data_skipped(self):
        assert detect_stop_loss(short(avg=None, mark=2.0), CFG) is None
        assert detect_stop_loss(short(avg=1.0, mark=None), CFG) is None

    def test_zero_avg_skipped(self):
        # Avoid div-by-zero
        assert detect_stop_loss(short(avg=0.0, mark=2.0), CFG) is None

    def test_disabled_when_multiplier_zero(self):
        cfg = AlertsConfig(stop_loss_multiplier=0)
        assert detect_stop_loss(short(avg=1.00, mark=5.00), cfg) is None

    def test_custom_multiplier(self):
        cfg = AlertsConfig(stop_loss_multiplier=2.5)
        # 2× under 2.5× → no fire
        assert detect_stop_loss(short(avg=1.00, mark=2.00), cfg) is None
        # 2.5× hits
        assert detect_stop_loss(short(avg=1.00, mark=2.50), cfg) is not None

    def test_alert_key_distinct_per_leg(self):
        a1 = detect_stop_loss(short(avg=1.00, mark=2.00), CFG)
        # different strike → different key
        pos2 = ShortPositionSnapshot(
            symbol="TEST", right="C", strike=110.0, expiry=EXPIRY, qty=-1,
            avg_open_price=1.0, current_mark=2.0, current_delta=None,
        )
        a2 = detect_stop_loss(pos2, CFG)
        assert a1 is not None and a2 is not None
        assert a1.alert_key != a2.alert_key
