"""Tests for the sensor/control registry (bqio.registry).

The registry is the single source of truth for metric names, units and
sanity bounds. These tests lock down its invariants so a typo or a
missing entry cannot silently corrupt dashboards.
"""

from __future__ import annotations

import pytest

from bqio.protocol import decode_value, scale
from bqio.registry import (
    CONTROLS,
    SANITY_BOUNDS,
    SENSORS,
    decode_sensor,
    within_bounds,
)


def test_registry_is_dense_from_zero():
    indices = sorted(SENSORS)
    assert indices[0] == 0
    assert indices == list(range(indices[0], indices[-1] + 1)), "sensor indices must be contiguous"


def test_active_sensors_have_metric_names():
    for idx, sd in SENSORS.items():
        if sd.active:
            assert sd.metric, f"sensor {idx} has empty metric name"
            assert sd.help, f"sensor {idx} has empty help text"


def test_control_definitions_are_well_formed():
    for ci, cd in CONTROLS.items():
        assert cd.values, f"control {ci} has no value map"
        for val, label in cd.values:
            assert isinstance(val, int)
            assert label


def test_sanity_bounds_cover_all_active_sensors():
    for idx, sd in SENSORS.items():
        if sd.active:
            assert idx in SANITY_BOUNDS, f"sensor {idx} lacks sanity bounds"
            lo, hi = SANITY_BOUNDS[idx]
            assert lo < hi, f"sensor {idx}: inverted bounds {lo}..{hi}"


def test_within_bounds_accepts_plausible_values():
    temp_idx = next(i for i, s in SENSORS.items() if s.metric == "temperature_celsius")
    assert within_bounds(temp_idx, 45.0)
    assert not within_bounds(temp_idx, 150.0)  # impossible
    assert not within_bounds(temp_idx, -50.0)  # impossible


def test_within_bounds_unknown_sensor_passes():
    assert within_bounds(99, 12345.0)  # unknown sensors are not gated


def test_decode_sensor_applies_vtype_and_exponent():
    # uint16 LE raw 0x0D83 = 3459, exponent -1 -> 345.9
    raw = bytes.fromhex("830d")
    assert decode_sensor(5, raw, vtype=2, exponent=-1) == pytest.approx(345.9)


def test_decode_sensor_matches_manual_pipeline():
    raw = bytes.fromhex("c834")  # 13512
    decoded = decode_value(raw, 2)
    assert decoded is not None
    manual = scale(decoded, 0)
    assert decode_sensor(0, raw, vtype=2, exponent=0) == manual
