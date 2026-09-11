"""bqio.registry — the single source of truth for sensors and controls.

Every front-end (CLI, exporter, web dashboard) derives its metric names,
units and help strings from this table. Adding a sensor touches exactly
one place.

Indices and value-type encodings were mapped empirically from the device
(``GetSensorInfo`` / ``GetSensorValue``) and are regression-tested against
captured traffic in ``tests/fixtures/``. Indices 13–22 exist in the
protocol but are not exposed by this hardware revision (the presence
bitmap only covers bits 0..12); they are listed for completeness and
marked inactive.
"""

from __future__ import annotations

from dataclasses import dataclass

from .protocol import decode_value, scale


@dataclass(frozen=True)
class SensorDef:
    """Definition of a single telemetry sensor."""

    index: int
    metric: str  # metric suffix (without the ``bqio_`` prefix)
    unit: str
    help: str
    active: bool = True


@dataclass(frozen=True)
class ControlDef:
    """Definition of a single actuator control."""

    index: int
    metric: str
    help: str
    values: tuple[tuple[int, str], ...]


#: idx -> sensor definition. Order is stable; the exporter emits metrics in
#: this order so dashboards can rely on a deterministic layout.
SENSORS: dict[int, SensorDef] = {
    0: SensorDef(0, "uptime_seconds", "seconds", "PSU uptime (s)"),
    1: SensorDef(1, "total_runtime_seconds", "seconds", "PSU total runtime (s)"),
    2: SensorDef(2, "fan_speed_rpm", "rpm", "PSU fan speed (rpm)"),
    3: SensorDef(3, "temperature_celsius", "degC", "PSU temperature (degC)"),
    4: SensorDef(4, "error_state", "", "PSU error state (0=OK)"),
    5: SensorDef(5, "ac_input_power_watts", "W", "AC input power (W)"),
    6: SensorDef(6, "ac_input_voltage_volts", "V", "AC input voltage (V)"),
    7: SensorDef(7, "rail_3v3_current_amps", "A", "Rail 3.3V current (A)"),
    8: SensorDef(8, "rail_3v3_voltage_volts", "V", "Rail 3.3V voltage (V)"),
    9: SensorDef(9, "rail_5v_current_amps", "A", "Rail 5V current (A)"),
    10: SensorDef(10, "rail_5v_voltage_volts", "V", "Rail 5V voltage (V)"),
    11: SensorDef(11, "rail_12v1_current_amps", "A", "Rail 12V1 current (A)"),
    12: SensorDef(12, "rail_12v1_voltage_volts", "V", "Rail 12V1 voltage (V)"),
    # Present in the protocol, not exposed by this hardware revision.
    13: SensorDef(13, "rail_12v2_current_amps", "A", "Rail 12V2 current (A)", active=False),
    14: SensorDef(14, "rail_12v2_voltage_volts", "V", "Rail 12V2 voltage (V)", active=False),
    15: SensorDef(15, "rail_12v3_current_amps", "A", "Rail 12V3 current (A)", active=False),
    16: SensorDef(16, "rail_12v3_voltage_volts", "V", "Rail 12V3 voltage (V)", active=False),
    17: SensorDef(17, "rail_12v4_current_amps", "A", "Rail 12V4 current (A)", active=False),
    18: SensorDef(18, "rail_12v4_voltage_volts", "V", "Rail 12V4 voltage (V)", active=False),
    19: SensorDef(19, "rail_12v5_current_amps", "A", "Rail 12V5 current (A)", active=False),
    20: SensorDef(20, "rail_12v5_voltage_volts", "V", "Rail 12V5 voltage (V)", active=False),
    21: SensorDef(21, "rail_12v6_current_amps", "A", "Rail 12V6 current (A)", active=False),
    22: SensorDef(22, "rail_12v6_voltage_volts", "V", "Rail 12V6 voltage (V)", active=False),
}

#: control index -> control definition
CONTROLS: dict[int, ControlDef] = {
    0: ControlDef(0, "fan_mode", "PSU fan mode", ((0, "semi-passive"), (1, "performance"))),
    1: ControlDef(1, "rail_mode", "PSU rail mode", ((0, "multi"), (1, "single"))),
}

#: idx -> (min, max) sanity bounds. Out-of-range readings are dropped
#: (last good value kept) — protects dashboards from firmware glitches
#: (e.g. a 5.9e14 degC spike observed in the field).
SANITY_BOUNDS: dict[int, tuple[float, float]] = {
    0: (0, 1e9),
    1: (0, 1e9),
    2: (0, 10000),
    3: (-40, 125),
    4: (0, 255),
    5: (0, 2000),
    6: (80, 300),
    7: (0, 100),
    8: (0, 10),
    9: (0, 100),
    10: (0, 10),
    11: (0, 200),
    12: (0, 20),
}


def within_bounds(idx: int, value: float) -> bool:
    """Sanity-check a decoded+scaled value against known-good ranges."""
    bounds = SANITY_BOUNDS.get(idx)
    if bounds is None:
        return True
    lo, hi = bounds
    return lo <= value <= hi


def decode_sensor(idx: int, raw: bytes, vtype: int, exponent: int) -> float | None:
    """Decode + scale a sensor value, applying sanity gating.

    Returns None when the value is undecodable or outside sanity bounds —
    callers keep their last good value in that case.
    """
    raw_val = decode_value(raw, vtype)
    if raw_val is None:
        return None
    value = scale(raw_val, exponent)
    return value if within_bounds(idx, value) else None
