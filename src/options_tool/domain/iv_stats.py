"""IV rank / IV percentile from a daily IV history.

Two industry-standard summaries of "is implied vol unusually high right now":

  IV Rank      = (current - low) / (high - low) over the lookback window.
                 Linear, sensitive to outliers — one earnings spike can pin
                 high near 1.0 for months. Reported 0–100 (%).

  IV Percentile = fraction of days in the window with IV strictly less than
                 today. Distribution-aware, ignores outliers — a "boring"
                 measure that better captures "today is the 70th-percentile
                 day for this stock." Reported 0–100 (%).

Both are computed over a 252-trading-day lookback by default (≈ 1 calendar
year — the tastytrade / ToS convention; covers 4 earnings cycles).

Pure functions, no I/O. Tested in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class IVPoint:
    date: date
    iv_30d: float
    hv_30d: float | None = None


@dataclass(frozen=True, slots=True)
class IVStats:
    current_iv: float
    iv_rank: float       # 0.0–1.0
    iv_percentile: float # 0.0–1.0
    window_days: int
    low: float
    high: float


def compute_stats(history: list[IVPoint]) -> IVStats | None:
    """Compute rank + percentile from chronological history.

    The most recent point is treated as "today". Returns ``None`` if history
    is empty or has only one distinct value (rank undefined).
    """
    if not history:
        return None

    ivs = [p.iv_30d for p in history if p.iv_30d > 0]
    if len(ivs) < 2:
        return None

    current = ivs[-1]
    low = min(ivs)
    high = max(ivs)

    if high == low:
        rank = 0.5
    else:
        rank = (current - low) / (high - low)

    # Percentile: fraction of *prior* days with IV strictly less than current.
    prior = ivs[:-1]
    pct = sum(1 for v in prior if v < current) / len(prior)

    return IVStats(
        current_iv=current,
        iv_rank=rank,
        iv_percentile=pct,
        window_days=len(ivs),
        low=low,
        high=high,
    )
