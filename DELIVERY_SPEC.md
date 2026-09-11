# 📦 Delivery Spec — Code Contract

> **Status: DELIVERED (v1.0.0).** This spec was the binding contract for
> delivering the implementation into this repository. The code below is the
> realized contract; deviations are noted inline. Kept for auditability.

## 🎯 Delivery Target

All code lives in this repository, branch `main`. The layout below is final —
do not introduce new top-level directories without discussion.

## 🗂️ Delivered Tree

```
bqio/
├── __init__.py           # version string (__version__ = "1.0.0")
├── __main__.py           # `python -m bqio` → calls cli.main()
├── hid_device.py         # transport layer (see Transport Contract)
├── protocol.py           # QLink codec (see Protocol Contract)
├── client.py             # session-aware high-level API (see API Contract)
├── registry.py           # SINGLE SOURCE OF TRUTH: sensors & controls
├── cli.py                # argparse subcommands (see CLI Contract)
├── exporter.py           # Prometheus exporter, port 9415
└── web.py                # stdlib dashboard server (+ /metrics)
tests/
├── conftest.py           # fixture loader + FakeHidDevice transport
├── test_protocol.py      # codec unit tests (frame build/parse, CRC)
├── test_registry.py      # registry invariants + consistency
├── test_hid_device.py    # transport (synthetic sysfs tree)
├── test_client.py        # session/API against fake transport
├── test_exporter.py      # renderer against seeded cache
├── test_web.py           # dashboard helpers
├── test_fixtures.py      # REGRESSION: 112 real captured frames
└── fixtures/             # captured 64-byte HID frames (*.bin) + manifest.json
```

**Deviations from the original spec (approved):**

| Original spec | Delivered | Reason |
|---------------|-----------|--------|
| `SENSOR_REGISTRY` in `client.py` | `registry.py` (dedicated module) | Cleaner separation; client stays focused on session/request logic |
| `hidapi` + `prometheus_client` runtime deps | **zero runtime deps** | `hidraw` char device suffices; hand-rolled Prometheus text is 40 lines |
| `black` + `flake8` | `ruff` (lint + format) + `mypy --strict` | Modern single-tool replacement; stricter typing |
| CLI: `status`, `sensor`, `fan-mode`, `rail-mode` | `detect`, `info`, `sensors`, `controls`, `watch`, `close-orphan` | Diagnostic-first CLI; control *writes* intentionally excluded (see below) |
| Web: bundled React SPA | stdlib `http.server` + inline HTML/JS | Zero-build, zero-dep philosophy; same functionality |
| systemd "out of scope" | `systemd/` + `udev/` shipped | Needed for the canonical deployment path |

## 📜 Contracts

### Transport (`hid_device.py`)

| Requirement | Detail |
|-------------|--------|
| Discovery | Find device by **VID `0x373f` / PID `0x0023`** via sysfs walk (do NOT hardcode `/dev/hidraw0`); fall back to explicit `--device` flag |
| Open | `O_RDWR \| O_NONBLOCK`, 64-byte reads/writes |
| Close | Idempotent, safe on error paths |
| Errors | Raise `DeviceNotFoundError` with actionable message (plugged? udev rule?) |
| Test seam | Structural `Transport` protocol so the client accepts any read/write/close object |

### Protocol (`protocol.py`)

| Requirement | Detail |
|-------------|--------|
| Frame | Fixed 64 bytes; layout per `docs/PROTOCOL.md` |
| CRC | CRC-16-CCITT, poly `0xA001`, init `0xFFFF`, LE trailer at `[62..63]` |
| Session | `ROOT/OPEN` (family 1, cmd 1) → response payload: `session_id` u8 at `[4]` |
| Request IDs | Wrap 1→255, never 0 |
| Value decode | Typed LE per `docs/PROTOCOL.md` value-type table, incl. half/float |
| Purity | No I/O, no mutable state — a stateless codec |

### API (`client.py`)

| Requirement | Detail |
|-------------|--------|
| Session | One session at a time; `open_session()` raises if already open; `close()` sends `ROOT/CLOSE` |
| Request matching | Responses matched by (feature, command, request_id); foreign frames drained and discarded |
| Retries | Configurable attempts + response window; bad-CRC frames dropped |
| Public API | `open_session()`, `request(feature, command, data)`, `get_serial_number()`, `get_device_info()`, `get_supported_features()`, `close()` |
| Scaling | Applied by the registry (`decode_sensor`), keeping raw + scaled available |

### Registry (`registry.py`)

| Requirement | Detail |
|-------------|--------|
| `SENSORS` | **Single source of truth**: index → `SensorDef(metric, unit, help, active)`. Must match the README sensor table and the exporter exactly (enforced by tests) |
| Sensors | Indices 0–22 per `docs/PROTOCOL.md`; 13 active on this hardware revision |
| `CONTROLS` | `fan_mode` (0/1), `rail_mode` (0/1) |
| Sanity bounds | Per-sensor `(min, max)`; out-of-range readings dropped (last good value kept) |

### CLI (`cli.py`)

Subcommands (match README examples exactly):

```
bqio detect                        # find the HID device node
bqio info                          # device info + serial + features
bqio sensors                       # dump all sensor values once
bqio controls                      # dump all control values once
bqio watch [-n COUNT]              # sample sensors repeatedly
bqio close-orphan                  # diagnose/close an orphaned session
```

Global flags: `--device PATH`, `-v/--verbose`.

**Control writes are intentionally NOT exposed.** The MCU has demonstrated
wedging behaviour under certain write sequences (firmware main-loop halt,
recoverable only by AC power cut). Until the write path is fully understood
and proven safe, the CLI is read-only for controls.

### Exporter (`exporter.py`)

- Port **9415**, path `/metrics`, Prometheus text format
- Metrics: `bqio_<sensor_metric_name>` for all 23 sensors + derived
  `bqio_dc_output_power_watts` / `bqio_efficiency_ratio` + `bqio_up` +
  `bqio_data_age_seconds` + `bqio_scrape_duration_seconds`
- Names must equal `registry.SENSORS` names (enforced by tests)
- Persistent session in a sampler thread; scrapes serve the cache instantly

### Web (`web.py`)

- Stdlib `http.server`; port 8080 default
- `GET /` → dashboard (inline HTML/JS, auto-refresh)
- `GET /metrics` → Prometheus text (delegates to the exporter renderer)

## ✅ Quality Gates (CI-enforceable)

1. `pytest` green — including the **registry-consistency tests** and the
   **fixture regression suite** (112 real captured frames)
2. `ruff check` + `ruff format --check` clean
3. `mypy --strict` clean on the package
4. No hardcoded device paths, IPs, hostnames, or credentials anywhere in the diff
5. Type hints on all public functions; docstrings on all public classes/functions
6. Fixture-based tests must run **without hardware** (fake transport)

Enforced by `.github/workflows/ci.yml` on Python 3.9–3.12; locally via `make check`.

## 📮 Delivery Process

1. Develop in the Hardware & Lab workspace
2. Copy finished modules into this repo's tree (layout above)
3. Run the quality gates locally (`make check`)
4. Open a PR against `main` using the PR template
5. Tag the PR with `delivery` so reviewers know it's a spec-conformant drop

## 🚫 Out of Scope (for now)

- Control *writes* (see CLI contract — MCU safety)
- Wheel distribution / PyPI publishing
- Windows support
- GUI beyond the web dashboard
- Any protocol extension beyond the documented families
