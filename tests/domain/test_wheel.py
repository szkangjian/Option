"""Wheel auto-flip detection tests.

The crux: a disappearing short leg + matching stock qty change implies
assignment; either alone is ambiguous (BTC, roll, partial fill, etc.).
"""
from datetime import date, timedelta

import pytest

from options_tool.domain.wheel import (
    AssignmentEvent,
    OptionSnapshot,
    StockSnapshot,
    detect_assignments,
)

TODAY = date(2026, 4, 20)
EXP = TODAY - timedelta(days=1)  # expired yesterday — typical assignment window


def opt(symbol: str = "ABC", right: str = "P", strike: float = 50.0,
        qty: int = -1, account_code: str = "U001",
        expiry: date | None = None) -> OptionSnapshot:
    return OptionSnapshot(
        account_code=account_code, symbol=symbol, right=right,
        strike=strike, expiry=expiry or EXP, qty=qty,
    )


def stk(symbol: str = "ABC", qty: float = 100.0,
        account_code: str = "U001") -> StockSnapshot:
    return StockSnapshot(account_code=account_code, symbol=symbol, qty=qty)


class TestCSPAssignment:
    def test_basic_csp_assignment(self):
        # Short put existed; now gone. Stock qty went from 0 → 100. Wheel on.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-1)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=100.0)],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        e = events[0]
        assert e.kind == "csp_assigned"
        assert e.new_intent == "INCOME"
        assert e.contracts == 1

    def test_multiple_csp_contracts(self):
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-3)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=300.0)],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        assert events[0].contracts == 3

    def test_csp_btc_not_assigned(self):
        # Short put closed manually — no stock change → not an assignment.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-1)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[],
            wheel_symbols={"ABC"},
        )
        assert events == []

    def test_csp_partial_stock_move_skipped(self):
        # Stock only moved 50 shares — doesn't match a 1-contract assignment.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-1)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=50.0)],
            wheel_symbols={"ABC"},
        )
        assert events == []

    def test_csp_existing_stock_added_to(self):
        # Already owned 200 shares; CSP assignment adds 100 → 300.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-1)],
            prior_stocks=[stk(qty=200.0)],
            current_opts=[],
            current_stocks=[stk(qty=300.0)],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        assert events[0].kind == "csp_assigned"


class TestCCExercise:
    def test_basic_cc_exercise(self):
        # Short call existed; now gone. Stock qty went from 100 → 0.
        events = detect_assignments(
            prior_opts=[opt(right="C", qty=-1)],
            prior_stocks=[stk(qty=100.0)],
            current_opts=[],
            current_stocks=[],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        e = events[0]
        assert e.kind == "cc_exercised"
        assert e.new_intent == "WANT_TO_OWN"

    def test_cc_btc_not_exercise(self):
        # CC bought back, stock untouched.
        events = detect_assignments(
            prior_opts=[opt(right="C", qty=-1)],
            prior_stocks=[stk(qty=100.0)],
            current_opts=[],
            current_stocks=[stk(qty=100.0)],
            wheel_symbols={"ABC"},
        )
        assert events == []

    def test_cc_partial_called_away(self):
        # 200 shares + 1 CC; called away on 100. New stock = 100 → matches.
        events = detect_assignments(
            prior_opts=[opt(right="C", qty=-1)],
            prior_stocks=[stk(qty=200.0)],
            current_opts=[],
            current_stocks=[stk(qty=100.0)],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        assert events[0].kind == "cc_exercised"


class TestWheelGate:
    def test_wheel_disabled_no_event(self):
        # All signals lined up but symbol not in wheel set.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=-1)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=100.0)],
            wheel_symbols=set(),
        )
        assert events == []


class TestRoll:
    def test_roll_not_assignment(self):
        # Old short P50 disappeared, new short P48 (later expiry) appeared.
        # Stock unchanged → roll, not assignment.
        new_exp = TODAY + timedelta(days=30)
        events = detect_assignments(
            prior_opts=[opt(right="P", strike=50.0, qty=-1)],
            prior_stocks=[],
            current_opts=[opt(right="P", strike=48.0, qty=-1, expiry=new_exp)],
            current_stocks=[],
            wheel_symbols={"ABC"},
        )
        assert events == []


class TestMultiAccount:
    def test_per_account_isolation(self):
        # Same symbol exists in two accounts; assignment in U001 doesn't get
        # confused by U002's unrelated stock holding.
        events = detect_assignments(
            prior_opts=[
                opt(right="P", qty=-1, account_code="U001"),
                opt(right="P", qty=-1, account_code="U002"),  # still open
            ],
            prior_stocks=[stk(qty=500.0, account_code="U002")],
            current_opts=[opt(right="P", qty=-1, account_code="U002")],
            current_stocks=[
                stk(qty=100.0, account_code="U001"),
                stk(qty=500.0, account_code="U002"),
            ],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        assert events[0].account_code == "U001"


class TestDataIntegrity:
    def test_long_legs_skipped(self):
        # Long puts can't be assigned to *us* — skip.
        events = detect_assignments(
            prior_opts=[opt(right="P", qty=+1)],  # we owned it
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=100.0)],
            wheel_symbols={"ABC"},
        )
        assert events == []

    def test_still_open_no_event(self):
        # Same option still in current snapshot → no assignment yet.
        prior = opt(right="P", qty=-1)
        events = detect_assignments(
            prior_opts=[prior],
            prior_stocks=[],
            current_opts=[prior],
            current_stocks=[stk(qty=100.0)],  # ignored: option not gone
            wheel_symbols={"ABC"},
        )
        assert events == []

    def test_event_carries_contract_details(self):
        events = detect_assignments(
            prior_opts=[opt(right="P", strike=42.5, qty=-2)],
            prior_stocks=[],
            current_opts=[],
            current_stocks=[stk(qty=200.0)],
            wheel_symbols={"ABC"},
        )
        assert len(events) == 1
        e = events[0]
        assert e.symbol == "ABC"
        assert e.right == "P"
        assert e.strike == 42.5
        assert e.expiry == EXP
        assert e.contracts == 2
