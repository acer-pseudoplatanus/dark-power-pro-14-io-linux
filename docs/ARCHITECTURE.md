# 🏛️ Architecture

High-level design of **bqio**. The goal is a thin, auditable pipeline from the
PSU's USB connector to your monitoring stack — with every layer swappable and
testable in isolation.

```
┌─────────────┐   USB-HID    ┌──────────────────────────────────────────┐
│  Dark Power │◄────────────►│                bqio                     │
│  Pro 14 IO  │  64-byte     │                                          │
│     (PSU)   │  HID reports │  ┌──────────┐   ┌───────────────────┐    │
└─────────────┘              │  │ protocol │──▶│      client       │    │
                             │  │  (codec) │   │ (high-level API)  │    │
                             │  └──────────┘   └────────┬──────────┘    │
                             │                          │               │
                             │        ┌─────────────────┼────────────┐  │
                             │        ▼                 ▼            ▼  │
                             │   ┌─────────┐      ┌──────────┐ ┌─────┐ │
                             │   │   CLI   │      │ exporter │ │ web │ │
                             │   └─────────┘      └────┬─────┘ └──┬──┘ │
                             └─────────────────────────┼──────────┼───┘
                                                       ▼          ▼
                                                 Prometheus   Browser
```

## Layers

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Transport | `hid_device.py` | Open/close `/dev/hidrawN`, raw 64-byte report I/O, device discovery by VID/PID via sysfs walk |
| Codec | `protocol.py` | Pure frame build/parse, CRC-16-CCITT, typed value decoding — no I/O, no state |
| API | `client.py` | Session handshake, request/response matching, retries, drain logic |
| Registry | `registry.py` | **Single source of truth**: sensor/control definitions, units, sanity bounds |
| CLI | `cli.py` | `detect`, `info`, `sensors`, `controls`, `watch`, `close-orphan` subcommands |
| Exporter | `exporter.py` | Persistent-session sampler thread, last-good-value cache, Prometheus text on `:9415` |
| Web | `web.py` | Stdlib HTTP server: dashboard + `/metrics` (byte-identical to the exporter) |

## Design Decisions

- **Zero runtime dependencies.** The transport reads the stock `hidraw`
  character device directly — the kernel's `usbhid` driver already delivers
  the 64-byte reports, so a userspace `hidapi`/`libusb` binding would only
  add a dependency for no gain. The exporter renders Prometheus text by hand;
  the web dashboard is served by `http.server`. Nothing outside the stdlib is
  imported at runtime.
- **Strict layering.** `protocol.py` is a stateless codec (unit-testable with
  plain bytes); `client.py` owns all timing/retry policy; `registry.py` owns
  all naming. Front-ends (CLI/exporter/web) derive everything from the
  registry, so they can never disagree.
- **Sysfs discovery, never `hidraw0`.** `idVendor`/`idProduct` live on the USB
  *device* node, an ancestor of the HID *interface* node — discovery climbs
  the sysfs tree until it finds them. Node numbering shifts after
  re-enumeration; assuming `/dev/hidraw0` is fragile.
- **Persistent session in the exporter.** A background sampler thread holds
  ONE session and polls at the device's cadence; scrapes read the cache and
  are instant. Opening a fresh session per scrape would hammer the MCU.
- **Last-good-value caching + sanity bounds.** Transient USB hiccups keep the
  last known value; physically impossible readings (e.g. a 5.9e14 °C firmware
  glitch observed in the field) are dropped and the last good value retained.
- **Derived metrics.** DC output power (sum of rail powers) and efficiency
  ratio (DC/AC) are computed in the renderer, clamped to sane ranges.
- **Fixture regression tests.** The codec is validated against 112 real
  captured frames (`tests/fixtures/`) with a ground-truth manifest — a
  regression that corrupts traffic toward the MCU is caught offline.

## Failure Modes

| Condition | Behaviour |
|-----------|-----------|
| Device unplugged | `bqio_up 0`, `bqio_data_age_seconds` grows, exporter keeps serving cached values, CLI exits with a clear error |
| CRC mismatch | Frame dropped, retried up to 3×, then reported as a failed cycle |
| Timeout (no ACK) | Same retry ladder; session re-opened on persistent failure |
| Unknown sensor type | Value skipped, logged, never crashes the scrape |
| Out-of-bounds reading | Dropped, last good value kept (sanity bounds per sensor) |
