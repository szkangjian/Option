"""Test the rejection-aggregation helper that backs the Web 'why empty?' UI."""
from datetime import date

from options_tool.advisor import _build_rejection_groups
from options_tool.domain.intents import (
    REASON_CROSSES_EARNINGS,
    REASON_DELTA_TOO_HIGH,
    REASON_DTE_TOO_SHORT,
    REASON_LABELS,
    Rejection,
)


def _rej(strike: float, code: str, *, expiry: date = date(2026, 6, 19)) -> Rejection:
    return Rejection(
        right="P",
        strike=strike,
        expiry=expiry,
        dte=30,
        delta=-0.20,
        mid=0.50,
        reason_code=code,
        reason_detail=f"detail for {strike}",
    )


class TestBuildRejectionGroups:
    def test_empty_input_returns_empty(self):
        assert _build_rejection_groups([]) == []

    def test_single_reason_one_group(self):
        rejections = [_rej(48, REASON_DELTA_TOO_HIGH), _rej(49, REASON_DELTA_TOO_HIGH)]
        groups = _build_rejection_groups(rejections)
        assert len(groups) == 1
        assert groups[0].code == REASON_DELTA_TOO_HIGH
        assert groups[0].label == REASON_LABELS[REASON_DELTA_TOO_HIGH]
        assert groups[0].count == 2

    def test_groups_sorted_by_count_desc(self):
        rejections = [
            _rej(48, REASON_DELTA_TOO_HIGH),
            _rej(49, REASON_CROSSES_EARNINGS),
            _rej(50, REASON_CROSSES_EARNINGS),
            _rej(51, REASON_CROSSES_EARNINGS),
        ]
        groups = _build_rejection_groups(rejections)
        assert [g.code for g in groups] == [REASON_CROSSES_EARNINGS, REASON_DELTA_TOO_HIGH]
        assert groups[0].count == 3
        assert groups[1].count == 1

    def test_items_within_group_sorted_by_expiry_then_strike(self):
        # Same code, mixed (expiry, strike) → ascending sort.
        rejections = [
            _rej(50, REASON_DTE_TOO_SHORT, expiry=date(2026, 7, 17)),
            _rej(48, REASON_DTE_TOO_SHORT, expiry=date(2026, 6, 19)),
            _rej(49, REASON_DTE_TOO_SHORT, expiry=date(2026, 6, 19)),
        ]
        groups = _build_rejection_groups(rejections)
        items = groups[0].items
        assert [(r.expiry, r.strike) for r in items] == [
            (date(2026, 6, 19), 48),
            (date(2026, 6, 19), 49),
            (date(2026, 7, 17), 50),
        ]

    def test_unknown_code_falls_back_to_code_as_label(self):
        # Defensive: if a new reason_code lands without a label entry,
        # don't crash — just show the code itself.
        rejections = [_rej(48, "future_unmapped_code")]
        groups = _build_rejection_groups(rejections)
        assert groups[0].label == "future_unmapped_code"
