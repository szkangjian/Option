"""IV rank / percentile pure-function tests."""
from datetime import date, timedelta

import pytest

from options_tool.domain.iv_stats import IVPoint, compute_stats


def series(values: list[float]) -> list[IVPoint]:
    """Build a chronological IVPoint list, one per day ending today."""
    today = date(2026, 4, 19)
    return [
        IVPoint(date=today - timedelta(days=len(values) - 1 - i), iv_30d=v)
        for i, v in enumerate(values)
    ]


class TestComputeStats:
    def test_empty_returns_none(self):
        assert compute_stats([]) is None

    def test_single_point_returns_none(self):
        # Need at least 2 points for rank / percentile to be defined.
        assert compute_stats(series([0.30])) is None

    def test_current_at_high_gives_rank_one(self):
        s = compute_stats(series([0.20, 0.30, 0.40, 0.50]))
        assert s is not None
        assert s.iv_rank == 1.0
        # 100% of prior days were < current → percentile = 1.0
        assert s.iv_percentile == 1.0

    def test_current_at_low_gives_rank_zero(self):
        s = compute_stats(series([0.50, 0.40, 0.30, 0.20]))
        assert s is not None
        assert s.iv_rank == 0.0
        assert s.iv_percentile == 0.0

    def test_midpoint(self):
        s = compute_stats(series([0.20, 0.40, 0.30]))
        assert s is not None
        assert s.iv_rank == pytest.approx(0.5)
        # 1 of 2 prior days strictly < 0.30
        assert s.iv_percentile == pytest.approx(0.5)

    def test_constant_series_falls_back_to_half(self):
        s = compute_stats(series([0.30, 0.30, 0.30]))
        assert s is not None
        assert s.iv_rank == 0.5
        assert s.iv_percentile == 0.0  # nothing was strictly less

    def test_window_size_reflects_valid_points(self):
        s = compute_stats(series([0.10, 0.20, 0.30, 0.40, 0.25]))
        assert s is not None
        assert s.window_days == 5
        assert s.low == 0.10
        assert s.high == 0.40
        assert s.current_iv == 0.25
