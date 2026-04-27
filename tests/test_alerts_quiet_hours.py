"""Quiet-hours window logic."""
from datetime import datetime

from options_tool.alerts import in_quiet_hours


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 4, 19, h, m)


class TestQuietHours:
    def test_within_wraparound_window_late_night(self):
        # 22:00 → 07:00 ; 23:30 is inside
        assert in_quiet_hours(at(23, 30), "22:00", "07:00") is True

    def test_within_wraparound_window_early_morning(self):
        # 22:00 → 07:00 ; 03:00 is inside
        assert in_quiet_hours(at(3, 0), "22:00", "07:00") is True

    def test_outside_wraparound_window(self):
        # 22:00 → 07:00 ; 14:00 is outside
        assert in_quiet_hours(at(14, 0), "22:00", "07:00") is False

    def test_boundary_start_inclusive(self):
        assert in_quiet_hours(at(22, 0), "22:00", "07:00") is True

    def test_boundary_end_exclusive(self):
        assert in_quiet_hours(at(7, 0), "22:00", "07:00") is False

    def test_non_wraparound_window(self):
        # 12:00 → 14:00 ; lunch break style
        assert in_quiet_hours(at(13, 0), "12:00", "14:00") is True
        assert in_quiet_hours(at(11, 30), "12:00", "14:00") is False
