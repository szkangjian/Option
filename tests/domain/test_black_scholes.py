"""BS Δ tests — used to estimate Greeks on the Yahoo fallback path.

We don't need refined precision (Yahoo data is noisy anyway), but the value
must be in the right monotonic ballpark — ATM Δ near ±0.50, deep OTM near 0,
deep ITM near ±1.0.
"""
from __future__ import annotations

import math

import pytest

from options_tool.domain.black_scholes import bs_delta


def test_atm_call_delta_near_half():
    # ATM, ~30 day, 30% IV — Δ should be ~0.50 (slightly above due to drift).
    delta = bs_delta(
        spot=100, strike=100, years_to_expiry=30/365, iv=0.30, is_call=True,
    )
    assert delta is not None
    assert 0.50 < delta < 0.58


def test_atm_put_delta_near_negative_half():
    delta = bs_delta(
        spot=100, strike=100, years_to_expiry=30/365, iv=0.30, is_call=False,
    )
    assert delta is not None
    assert -0.50 < delta < -0.42


def test_deep_otm_call_delta_near_zero():
    # CC at strike 50% above spot, short DTE, low IV — Δ should be tiny.
    delta = bs_delta(
        spot=100, strike=150, years_to_expiry=14/365, iv=0.20, is_call=True,
    )
    assert delta is not None
    assert 0.0 <= delta < 0.02


def test_deep_itm_call_delta_near_one():
    delta = bs_delta(
        spot=100, strike=50, years_to_expiry=30/365, iv=0.30, is_call=True,
    )
    assert delta is not None
    assert delta > 0.97


def test_deep_otm_put_delta_near_zero():
    # CSP at strike 50% below spot — Δ should be tiny negative.
    delta = bs_delta(
        spot=100, strike=50, years_to_expiry=30/365, iv=0.30, is_call=False,
    )
    assert delta is not None
    assert -0.02 < delta <= 0.0


def test_call_delta_monotonic_in_strike():
    """Lower strike call → higher Δ (more ITM)."""
    deltas = [
        bs_delta(spot=100, strike=k, years_to_expiry=30/365, iv=0.30, is_call=True)
        for k in (80, 90, 100, 110, 120)
    ]
    # Strictly decreasing as strike increases
    for prev, nxt in zip(deltas, deltas[1:]):
        assert prev > nxt


def test_put_delta_monotonic_in_strike():
    """Higher strike put → more negative Δ (more ITM)."""
    deltas = [
        bs_delta(spot=100, strike=k, years_to_expiry=30/365, iv=0.30, is_call=False)
        for k in (80, 90, 100, 110, 120)
    ]
    # Strictly decreasing (more negative) as strike increases
    for prev, nxt in zip(deltas, deltas[1:]):
        assert prev > nxt


@pytest.mark.parametrize(
    "kwargs",
    [
        {"spot": 0, "strike": 100, "years_to_expiry": 0.1, "iv": 0.3},
        {"spot": 100, "strike": 0, "years_to_expiry": 0.1, "iv": 0.3},
        {"spot": 100, "strike": 100, "years_to_expiry": 0, "iv": 0.3},
        {"spot": 100, "strike": 100, "years_to_expiry": 0.1, "iv": 0},
        {"spot": -1, "strike": 100, "years_to_expiry": 0.1, "iv": 0.3},
    ],
)
def test_degenerate_inputs_return_none(kwargs):
    assert bs_delta(is_call=True, **kwargs) is None


def test_call_put_parity_relationship():
    """Δ_call − Δ_put = e^(-q*T) ≈ 1.0 for non-dividend underlying."""
    args = dict(spot=100, strike=105, years_to_expiry=45/365, iv=0.25)
    dc = bs_delta(is_call=True, **args)
    dp = bs_delta(is_call=False, **args)
    assert dc is not None and dp is not None
    assert math.isclose(dc - dp, 1.0, abs_tol=1e-9)
