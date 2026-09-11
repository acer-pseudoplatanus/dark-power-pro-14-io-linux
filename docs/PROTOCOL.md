# 📡 QLink Protocol Notes

> ⚠️ **Reverse-engineered.** Everything on this page was derived from packet
> captures of the official vendor tool. Field meanings marked with `?` are
> inferred, not confirmed. The protocol is undocumented and may change.

## Overview

- **Transport:** USB HID, single interrupt endpoint, fixed **64-byte** reports
- **Direction:** host-initiated polling (request → response pairs)
- **Integrity:** CRC-16-CCITT (poly `0xA001`, init `0xFFFF`) over bytes `[0..61]`,
  stored little-endian in bytes `[62..63]`
- **Encoding:** little-endian throughout

## Frame Layout

```
Offset  Size  Field
──────  ────  ─────────────────────────────────────────────
0       1     payload_length        (len(payload) + 6)
1       1     ? (reserved, observed 0x00)
2       1     session_id
3       1     status                (response only; 0 = OK)
4       1     request_id            (increments per request, wraps 1→255)
5       1     family
6       1     command
7       57    payload (zero-padded)
62      2     crc16 (little-endian)
```

## Families & Commands

| Family | ID | Purpose |
|--------|----|---------|
| `ROOT` | 1 | Session lifecycle |
| `DEVINFO` | 3 | Device identification |
| `SENSORS` | 48 | Telemetry |
| `CONTROLS` | 49 | Actuation |

### ROOT

| Command | ID | Direction | Payload |
|---------|----|-----------|---------|
| `OPEN` | 1 | → | `[client_type]` (observed `0x02` = web client); response payload carries `connection_id` (u32 LE, offsets 0–3) and `session_id` (u8, offset 4) |
| `CLOSE` | 2 | → | — |

### SENSORS

| Command | ID | Purpose |
|---------|----|---------|
| `GETINFO` | 1 | Enumerate sensors: per-sensor value type + exponent |
| `GETSINFO` | 2 | Extended per-sensor metadata `?` |
| `GETVAL` | 3 | Read one/all sensor values |

### CONTROLS

| Command | ID | Purpose |
|---------|----|---------|
| `GETINFO` | 1 | Enumerate controls |
| `GETCINFO` | 2 | Per-control metadata `?` |
| `GETVAL` | 3 | Read/set control values |

## Value Types

| ID | Size | Format |
|----|------|--------|
| 0 | 1 | uint8 |
| 1 | 1 | int8 |
| 2 | 2 | uint16 |
| 3 | 2 | int16 |
| 4 | 4 | uint32 |
| 5 | 4 | int32 |
| 6 | 8 | uint64 |
| 7 | 8 | int64 |
| 8 | 2 | IEEE 754 half |
| 9 | 4 | IEEE 754 float |
| 10 | 8 | IEEE 754 double `?` |

Raw values are scaled by a per-sensor exponent obtained from `GETINFO`.

## Sensor Table (indices observed)

| Index | Meaning |
|-------|---------|
| 0 | Uptime (s) |
| 1 | Total runtime (s) |
| 2 | Fan speed (rpm) |
| 3 | Temperature (°C) |
| 4 | Error state (0 = OK) |
| 5 | AC input power (W) |
| 6 | AC input voltage (V) |
| 7–8 | 3.3V rail current / voltage |
| 9–10 | 5V rail current / voltage |
| 11–22 | 12V rails 1–6, current / voltage pairs |

## Controls

| Index | Meaning | Values |
|-------|---------|--------|
| 0 | Fan mode | 0 = semi-passive, 1 = performance |
| 1 | Rail mode | 0 = multi, 1 = single |

## Unsolicited Notifications (device push)

The device pushes `SensorValueChanged` notifications on its own sampling
cadence (~500 ms, firmware-fixed) — no request needed. Identification:
`request_id == 0` **and** `sequence == 0` (vendor bundle criterion).
They share the numeric command id with `GetSensorInfo` (both = 2);
only the request id tells them apart.

Payload: a sequence of `(sensor_index, raw_value)` pairs, value width
given by the sensor's value type. One packet can carry several sensors.
Decoding: `bqio.protocol.parse_sensor_changed()`.

Verified 2026-09-09: `SetSensorConfig` (cmd=5) is **rejected** with
status 3 (invalid command id) — the 500 ms cadence is not configurable.
See `docs/HIRES.md` for the full empirical study.

## Open Questions

- [ ] Exact meaning of reserved byte 1
- [ ] Whether `GETVAL` supports batch reads (observed: single)
- [ ] Firmware version exposure (likely in `DEVINFO`)
- [ ] Behaviour of `error_state` codes beyond 0
- [x] Data-send modes / higher rates — investigated: firmware samples at
      500 ms, `SetSensorConfig` unsupported (see `docs/HIRES.md`)

## Capture Methodology

1. Run the official vendor tool on Windows, mirror traffic with a USB analyzer
   (or `hidraw` sniffing on Linux)
2. Normalize captures into `tests/fixtures/*.bin`
3. Decode with the codec; annotate findings here
4. Regression-test the codec against the fixtures
