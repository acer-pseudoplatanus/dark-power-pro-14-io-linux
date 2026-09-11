# Changelog

All notable changes to **bqio** are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2026-09-11

### Added
- **Official QLink session keep-alive (production-grade driving profile):**
  the firmware expires the QLink session after `SESSION_TIMEOUT`
  (reported by the device in `OpenSession`, measured: 2 s). Afterwards
  every request is answered with `status=1` (`INVALID_SESSION_ID`) and
  the push stream goes silent. The official QLink app keeps the session
  alive with a dedicated `KeepAlive` ping every `SESSION_TIMEOUT / 2`
  (reverse-engineered from the shipped web bundle). The exporter now
  implements exactly this profile:
  - `QLinkClient.keepalive()` sends the `KeepAlive` command (feature
    `ROOT`, command 3) and distinguishes three outcomes: `ok`,
    `expired` (answered `status=1`), `lost` (no response at all).
  - `QLinkClient.session_timeout` exposes the device-reported lifetime.
  - `QLinkClient.reopen_session()` re-opens the session in place
    (cheap recovery, no USB touch).
  - The collector sends a KeepAlive ping every `KEEPALIVE_INTERVAL`
    (default 1 s = `SESSION_TIMEOUT / 2`, env override
    `BQIO_KEEPALIVE_INTERVAL`).
- **Two-stage automatic recovery (eliminates manual PSU cold-starts):**
  - Stage 1 (cheap): an `expired` KeepAlive answer or a failed probe /
    full refresh triggers an immediate session re-open
    (`bqio_session_reopens_total`).
  - Stage 2 (invasive): `LOST_KA_BEFORE_USB_RESET` (default 6)
    consecutive `lost` pings (transport-level wedge, no responses at
    all) trigger a USB re-enumeration — close the HID fd, re-resolve
    the device node, re-open (`bqio_usb_resets_total`). Recovers from
    endpoint wedges without touching the PSU rocker switch.
- **New observability counters:** `bqio_keepalive_sent_total`,
  `bqio_keepalive_expired_total`, `bqio_session_reopens_total`,
  `bqio_usb_resets_total`.

### Changed
- **Liveness probe rotates round-robin** over all active sensors
  (carried over from the v1.2.1 interim fix): slow-moving sensors
  (uptime, error state, rail mode) are no longer starved by the
  30 s full refresh.
- **`status=1` handling corrected:** v1.2.1 interpreted `status=1` as
  "alive but no new value" and tolerated it. That interpretation was
  wrong — `status=1` is `INVALID_SESSION_ID` (verified against the
  official QLink protocol enum in the shipped web bundle). With proper
  KeepAlive the session no longer expires, so `status=1` is now treated
  as a session fault and triggers stage-1 recovery instead of being
  swallowed.

### Notes
- Verified on hardware (foundation test, 2026-09-11): 15 consecutive
  KeepAlive pings at 1/s keep the session AND the telemetry engine
  alive; subsequent `GetSensorValue` reads return `status=0` for all
  13 sensors; the push stream stays active throughout. Without
  KeepAlive the session dies within ~2 s (control phase reproduced
  `status=1` on 0/13 reads).
- Average request rate: ~1.05 req/s (KeepAlive 1/s + probe 0.2/s +
  refresh 0.033/s) — still far below the ~50 req/s that triggered the
  MCU hangs of INC-0012.

## [1.2.1] — 2026-09-11

### Fixed
- **Stale telemetry engine / "No data" on status metrics (post-restart
  regression):** empirical probing showed the MCU parks its telemetry
  engine in a low-power state after ~1.5 s without a `GetSensorValue`;
  while parked, every read returns `status=1` with an empty payload and
  the push stream goes silent. Nothing wakes the engine short of a
  session reopen. The v1.2.0 liveness probe (5 s interval) therefore
  always saw `status=1`, counted it as a failure, and drove a permanent
  reconnect loop — controls and rarely-changing sensors (fan mode, rail
  mode, error state, uptime, runtime) never received a value.
  - `PROBE_INTERVAL` default lowered 5 s → 1 s (keeps the engine warm;
    still ~0.35 req/s average incl. full refresh).
  - The probe now rotates round-robin over all active sensors
    (every sensor refreshed by the probe every ~13 s).
  - `status=1` ("no new value") is treated as **alive-but-unchanged**
    everywhere: neither the probe nor the full refresh counts it as a
    failure; only timeouts/exceptions do.
  - Full refresh skips `status=1` sensors instead of aborting the cycle.

### Changed
- **Privacy:** the `devinfo_getserial_rx.bin` fixture no longer contains
  a real device serial number (zeroed, CRC recomputed, manifest SHA
  updated). Captures published in this repo are sanitized.
- **Packaging/SEO:** `pyproject.toml` description and keywords now name
  the concrete device (be quiet! Dark Power Pro 14 IO, QLink, USB-HID)
  for discoverability; README gained a keyword line. Package version
  synced to 1.2.1.

## [1.2.0] — 2026-09-10

### Changed
- **Push-primary collection (INC-0012 mitigation):** the collector now
  treats device-pushed `SensorValueChanged` notifications as the PRIMARY
  data path (passive harvest, zero requests) instead of polling every
  sensor at 2 Hz. MCU load drops from ~50 requests/s to ~0.35 requests/s
  (−98 %) while data freshness stays at the device-native 500 ms
  resolution. Two tunable maintenance intervals keep the session alive
  and the cache complete:
  - liveness probe (default 5 s): one `GetSensorValue` on the PSU
    temperature sensor — proves MCU responsiveness, keeps
    `bqio_data_age_seconds` honest.
  - full refresh (default 30 s): one read per active sensor/control —
    catches sensors that stopped pushing, keeps derived metrics
    (DC power, efficiency) complete.
  - Env overrides: `BQIO_PROBE_INTERVAL`, `BQIO_FULL_REFRESH_INTERVAL`.
- **Hard wall-clock watchdog:** a collector iteration that exceeds
  `HARD_ITER_LIMIT` (30 s) forces a session teardown and reconnect.
  Protects against kernel-level HID stalls where the nominal 1.5 s
  `rx_timeout` cannot fire (INC-0012: 2 h wedged read).

### Fixed
- Efficiency EWMA alpha now derives from the push cadence
  (`PUSH_HARVEST_WINDOW`) instead of the removed poll constant.

## [Unreleased]

### Added
- **HiRes hybrid sampler:** the exporter now consumes device-pushed
  `SensorValueChanged` notifications (request_id=0) in addition to the
  500 ms poll backbone. Push updates carry exact reception timestamps.
- New metrics: `bqio_notifications_received`, `bqio_poll_updates`,
  `bqio_push_updates`, `bqio_last_update_source` (1=poll, 2=push).
- `bqio notif` CLI command: passive listener with ms-precision
  inter-arrival cadence measurement.
- `protocol.is_notification()` / `protocol.parse_sensor_changed()` —
  defensive decoder for multi-sensor notification payloads.
- `client.passive_read()` — listen-only RX (no TX) with notification
  dispatch; callback exceptions are contained and logged.
- `docs/HIRES.md` — empirical study: 3 ms roundtrip latency, firmware
  samples at 500 ms, `SetSensorConfig` rejected (status 3).
- `dashboards/bqio-hires.json` — Grafana dashboard incl. push/poll
  data-source panel and freshness gauge.

### Changed
- Hardened CI: SHA-pinned actions, least-privilege permissions, wheel-build
  smoke test on the release path.
- Raised the enforced test-coverage floor to 80% (currently ~88%) and
  extended the offline suite: CLI error paths, exporter `run()`/`serve()`/
  `main()`, live web-handler requests, client lifecycle/error paths.
- Added `CODE_OF_CONDUCT.md`, `SECURITY.md`, `.gitattributes` (lean sdist).
- `bqio detect` now exits cleanly (exit 1) when `/sys/class/hidraw` is
  absent instead of raising a traceback; `bqio_data_age_seconds` reports
  `NaN` before the first sample (Prometheus convention).

## [1.0.0] — 2026-09-09

First public release. Delivered per [`DELIVERY_SPEC.md`](DELIVERY_SPEC.md).

### Added
- Modular package layout: `hid_device` (transport), `protocol` (codec),
  `registry` (single source of truth), `client` (session API), `cli`,
  `exporter`, `web`.
- Diagnostic CLI: `detect`, `info`, `sensors`, `controls`, `watch`,
  `close-orphan`.
- Prometheus exporter (`:9415/metrics`) with persistent sampling session,
  EWMA effective-load smoothing and sanity-bound rejection.
- Stdlib-only web dashboard (`:8080`) with live charts and `/metrics`
  passthrough.
- Test suite (53 tests) including fixture-regression tests against 112
  captured QLink frames with ground-truth manifest.
- Quality gates: ruff (lint + format), mypy --strict, pytest, coverage.
- CI workflow (Python 3.9–3.12), Makefile, contribution scaffolding.
- Deployment assets: systemd unit + udev rule.

### Notes
- Zero runtime dependencies by design (raw `/dev/hidraw` + hand-rolled
  Prometheus text exposition).
- Known limitation: a wedged telemetry MCU (firmware main loop stopped)
  requires a physical AC power cut to recover; the exporter reports
  `bqio_up 0` in that state.
