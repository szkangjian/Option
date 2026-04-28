"""Per-symbol preset override merge + validation."""
from __future__ import annotations

import pytest

from options_tool.settings import (
    IntentPreset,
    PresetOverrideError,
    apply_preset_overrides,
    validate_preset_overrides,
)


def _income_preset() -> IntentPreset:
    return IntentPreset(
        side="CALL",
        delta_max=0.20,
        dte_min=25,
        dte_max=55,
        rank_by="annualized_roc",
        strike_window_pct=0.40,
        max_strikes_per_side=30,
        exclude_earnings_dte=True,
        top_n=5,
    )


def test_empty_overrides_returns_empty_dict():
    assert validate_preset_overrides(None) == {}
    assert validate_preset_overrides({}) == {}


def test_blank_values_dropped():
    out = validate_preset_overrides({"delta_max": "", "dte_min": None})
    assert out == {}


def test_string_inputs_coerced_to_numeric():
    out = validate_preset_overrides(
        {"delta_max": "0.25", "dte_min": "10", "max_strikes_per_side": "12"}
    )
    assert out == {"delta_max": 0.25, "dte_min": 10, "max_strikes_per_side": 12}
    assert isinstance(out["delta_max"], float)
    assert isinstance(out["dte_min"], int)


def test_unknown_field_rejected():
    with pytest.raises(PresetOverrideError, match="未知 override 字段"):
        validate_preset_overrides({"side": "PUT"})


def test_delta_out_of_range_rejected():
    with pytest.raises(PresetOverrideError, match=r"\[0, 1\]"):
        validate_preset_overrides({"delta_max": 1.5})
    with pytest.raises(PresetOverrideError, match=r"\[0, 1\]"):
        validate_preset_overrides({"delta_min": -0.1})


def test_strike_window_pct_must_be_positive():
    with pytest.raises(PresetOverrideError, match=r"\(0, 2\]"):
        validate_preset_overrides({"strike_window_pct": 0})
    with pytest.raises(PresetOverrideError, match=r"\(0, 2\]"):
        validate_preset_overrides({"strike_window_pct": 3.0})


def test_strike_max_vs_target_must_be_positive():
    with pytest.raises(PresetOverrideError, match=r"\(0, 5\]"):
        validate_preset_overrides({"strike_max_vs_target": 0})


def test_negative_int_field_rejected():
    with pytest.raises(PresetOverrideError, match="不能为负"):
        validate_preset_overrides({"dte_max": -5})


def test_non_numeric_value_rejected():
    with pytest.raises(PresetOverrideError, match="必须是整数"):
        validate_preset_overrides({"dte_min": "abc"})
    with pytest.raises(PresetOverrideError, match="必须是数字"):
        validate_preset_overrides({"delta_max": "abc"})


def test_delta_min_gt_delta_max_rejected():
    with pytest.raises(PresetOverrideError, match="delta_min .* delta_max"):
        validate_preset_overrides({"delta_min": 0.4, "delta_max": 0.2})


def test_dte_min_gt_dte_max_rejected():
    with pytest.raises(PresetOverrideError, match="dte_min .* dte_max"):
        validate_preset_overrides({"dte_min": 60, "dte_max": 30})


def test_apply_no_overrides_returns_unchanged():
    p = _income_preset()
    assert apply_preset_overrides(p, None) is p
    assert apply_preset_overrides(p, {}) is p


def test_apply_overrides_returns_new_instance_with_patches():
    p = _income_preset()
    out = apply_preset_overrides(p, {"strike_window_pct": 0.50, "delta_max": 0.15})
    # Original untouched
    assert p.strike_window_pct == 0.40
    assert p.delta_max == 0.20
    # New has patches
    assert out.strike_window_pct == 0.50
    assert out.delta_max == 0.15
    # Unrelated fields preserved
    assert out.dte_min == p.dte_min
    assert out.side == p.side


def test_apply_overrides_ignores_unknown_keys_defensively():
    """``apply_preset_overrides`` trusts callers to validate first, but if a
    stale ``side`` slipped into the JSON it must not crash IntentPreset."""
    p = _income_preset()
    out = apply_preset_overrides(p, {"side": "PUT", "delta_max": 0.10})
    assert out.side == "CALL"  # stale 'side' in overrides ignored
    assert out.delta_max == 0.10
