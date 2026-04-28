"""Black-Scholes Δ — used to estimate Greeks when the data source (Yahoo)
doesn't supply them.

Pure math, no I/O. Standard textbook BS for European options on a non-dividend
underlying. Equity options are American so Δ is technically off, but for
short-DTE OTM strikes (the bread and butter of CC/CSP scans) the difference
is negligible — well within the noise of a backup data source.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """N(x) — standard normal CDF via math.erf, no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    *,
    spot: float,
    strike: float,
    years_to_expiry: float,
    iv: float,
    is_call: bool,
    risk_free_rate: float = 0.045,
) -> float | None:
    """Black-Scholes delta for a European option.

    ``risk_free_rate`` defaults to 4.5% (rough mid-2025/2026 short-rate level).
    Δ is not very sensitive to r for short DTE, so a constant is fine for the
    Yahoo fallback path; promote to settings if it ever matters.

    Returns ``None`` for inputs that don't yield a meaningful delta
    (non-positive time, vol, or prices) instead of raising — caller can leave
    ``OptionQuote.delta`` as None and let downstream filters skip the row.
    """
    if (
        spot <= 0
        or strike <= 0
        or years_to_expiry <= 0
        or iv <= 0
    ):
        return None
    sigma_sqrt_t = iv * math.sqrt(years_to_expiry)
    if sigma_sqrt_t == 0:
        return None
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate + 0.5 * iv * iv) * years_to_expiry
    ) / sigma_sqrt_t
    if is_call:
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0
