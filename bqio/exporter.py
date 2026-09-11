"""bqio.exporter — Prometheus exporter for be quiet! IO-series PSUs.

Architecture (push-primary, v1.2.0)
-----------------------------------
The device pushes unsolicited ``SensorValueChanged`` notifications at its
native firmware cadence (~500 ms, verified empirically) — no request
needed. The collector therefore treats PUSH as the primary data path:
a background thread holds ONE persistent QLink session and continuously
harvests pushed notifications via ``passive_read()`` (sends nothing).

Two low-rate maintenance requests keep the session alive and the cache
complete (tunable via ``BQIO_PROBE_INTERVAL`` / ``BQIO_FULL_REFRESH_INTERVAL``):

* **Liveness probe** (default every 5 s): a single ``GetSensorValue``
  on the PSU temperature sensor. Proves the MCU is responsive and keeps
  ``bqio_data_age_seconds`` honest even if pushes stall.
* **Full refresh** (default every 30 s): one ``GetSensorValue`` per
  active sensor + control. Catches any sensor that stopped pushing and
  keeps derived metrics (DC power, efficiency) complete.

Total MCU load drops from ~50 requests/s (v1.1.0: 25 single-sensor reads
per 500 ms cycle) to ~0.35 requests/s — a 98 % reduction — while data
freshness stays at the device-native 500 ms resolution (push timestamps
are exact).

Motivation (INC-0012, 2026-09-10): two QLink MCU hangs within 24 h after
weeks of 24/7 polling at 50 req/s (chronic precursor: ~56 failed
OpenSession handshakes/h for 33 h before total hang). The MCU is a small
embedded controller; sustained high request rates are outside its design
operating point.

Note on resolution: the firmware internally samples at 500 ms (verified
empirically — 10 s of 343 Hz polling yields a single distinct value per
sensor, and ``SetSensorConfig`` is not implemented by the firmware,
status=3 INVALID_COMMAND_ID). 20–100 ms telemetry is physically not
achievable; 500 ms with exact push timestamps is the ceiling.

Scrape requests are served instantly from cache, so the Prometheus
scrape interval and the sampling interval are decoupled.

Metrics (prefix ``bqio_``)
-------------------------
bqio_<sensor>               raw sensor values (see bqio.registry.SENSORS)
bqio_ctrl_fan_mode          fan mode (0=semi-passive, 1=performance)
bqio_ctrl_rail_mode         rail mode (0=multi, 1=single)
bqio_dc_output_power_w      sum(I*V) over all rails (derived)
bqio_efficiency_percent     DC load / AC input, EWMA-smoothed tau=10 s
bqio_up                     1 if the device answered recently, else 0
bqio_data_age_seconds       seconds since last successful sample
bqio_cycle_duration_seconds duration of the last sample cycle
bqio_notifications_total    consumed SensorValueChanged notifications
bqio_notification_age_seconds seconds since the last notification
bqio_push_share             fraction of cache updates via push (0..1)

Safety
------
* Exactly one session is held at any time; reconnects close the old
  session first (bounded, so a wedged MCU cannot hang us).
* Outgoing packets are validated by the protocol layer.
* If the MCU wedges (no responses), the exporter backs off exponentially
  instead of hammering the device.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import socket
import threading
import time

from . import __version__
from .client import QLinkClient
from .protocol import (
    CTL_GETCINFO,
    CTL_GETINFO,
    CTL_GETVAL,
    F_CONTROLS,
    F_SENSORS,
    S_GETINFO,
    S_GETSINFO,
    S_GETVAL,
    S_NOTIF_CHANGED,
    DeviceNotFoundError,
    ParsedPacket,
    ProtocolError,
    decode_value,
    parse_sensor_changed,
    scale,
    signed_byte,
)
from .registry import CONTROLS, SENSORS, within_bounds

log = logging.getLogger("bqio.exporter")

METRIC_PREFIX = "bqio"
DEFAULT_PORT = 9415
#: Harvest window for passive push reads (seconds). The firmware pushes at
#: ~500 ms, so a 0.5 s window catches every notification with slack.
PUSH_HARVEST_WINDOW = 0.5
#: KeepAlive ping interval (seconds). The official QLink app pings every
#: ``SESSION_TIMEOUT / 2`` (device-reported timeout is 2 s → 1 s ping).
#: Without KeepAlive the firmware expires the session and answers every
#: request with status=1 (INVALID_SESSION_ID); the push stream stops.
#: Override with BQIO_KEEPALIVE_INTERVAL.
KEEPALIVE_INTERVAL = float(os.environ.get("BQIO_KEEPALIVE_INTERVAL", "1"))
#: Liveness probe interval (seconds). A single-sensor read (round-robin)
#: proves the telemetry engine is producing values and keeps the cache
#: honest even if pushes stall. Override with BQIO_PROBE_INTERVAL.
PROBE_INTERVAL = float(os.environ.get("BQIO_PROBE_INTERVAL", "5"))
#: Full-refresh interval (seconds). One read per active sensor/control.
#: Override with BQIO_FULL_REFRESH_INTERVAL.
FULL_REFRESH_INTERVAL = float(os.environ.get("BQIO_FULL_REFRESH_INTERVAL", "30"))
EFFICIENCY_EWMA_TAU = 10.0  # seconds
RECONNECT_BASE_DELAY = 1.0  # seconds
RECONNECT_MAX_DELAY = 60.0  # seconds
#: Consecutive "lost" KeepAlive pings (no response at all) before the
#: escalation ladder kicks in: session re-open first (cheap), then a USB
#: re-enumeration (device fd close + re-open), then backoff.
LOST_KA_BEFORE_REOPEN = 3
LOST_KA_BEFORE_USB_RESET = 6
#: Hard wall-clock limit for a single collector iteration (seconds).
#: Guards against kernel-level stalls where the nominal rx_timeout cannot
#: fire (INC-0012: collector wedged 2 h in a HID read despite the 1.5 s
#: timeout). If an iteration exceeds this, the session is torn down and
#: re-opened — the cheapest guaranteed recovery.
HARD_ITER_LIMIT = 30.0  # seconds


class Collector(threading.Thread):
    """Persistent-session sampler with cached values."""

    def __init__(self, device: str | None = None):
        super().__init__(daemon=True, name="bqio-collector")
        self.device = device
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}
        self._last_ok = 0.0
        self._cycle_dur = 0.0
        self._up = 0
        self._meta: dict[int, tuple[int, int]] = {}  # idx -> (vtype, exponent)
        self._controls: dict[int, int] = {}  # idx -> vtype
        self._eff_ewma: float | None = None
        self._qc: QLinkClient | None = None
        self._fails = 0
        self._lost_ka = 0  # consecutive "lost" KeepAlive pings (escalation)
        self._probe_cursor = 0  # round-robin cursor for the liveness probe
        self._probe_next = 0.0  # next probe deadline (monotonic)
        self._refresh_next = 0.0  # next full-refresh deadline (monotonic)
        # Push-notification accounting (hybrid sampler).
        self._notif_count = 0
        self._notif_last = 0.0
        self._push_updates = 0
        self._poll_updates = 0
        self._last_update_source = "none"  # "poll" | "push" | "none"
        # Recovery accounting (metrics).
        self._reopens = 0  # session re-opens performed
        self._usb_resets = 0  # USB re-enumerations performed
        self._ka_sent = 0  # KeepAlive pings sent
        self._ka_expired = 0  # KeepAlive pings answered with status=1

    # ------------------------------------------------------------------ meta

    def _connect(self) -> None:
        """Open device + session, cache sensor/control metadata."""
        qc = QLinkClient(device=self.device, on_notification=self._on_notification)
        qc.__enter__()
        try:
            qc.open_session()
        except ProtocolError:
            qc.__exit__(None, None, None)
            raise
        self._qc = qc

        resp = qc.request(F_SENSORS, S_GETINFO)
        if resp is None:
            raise ProtocolError("GetSensorInfo not acknowledged")
        count = resp["payload"][0]
        bitmap = resp["payload"][1:]
        active = set()
        for byte_idx, byte in enumerate(bitmap):
            for bit in range(8):
                if byte >> bit & 1:
                    active.add(byte_idx * 8 + bit)

        self._meta.clear()
        for idx in range(min(count, 23)):
            if idx not in active:
                continue
            info = qc.request(F_SENSORS, S_GETSINFO, data=bytes([idx]))
            if info and info["status"] == 0 and len(info["payload"]) >= 3:
                vtype = info["payload"][1]
                exponent = signed_byte(info["payload"][2])
                self._meta[idx] = (vtype, exponent)

        self._controls.clear()
        ctrl_info = qc.request(F_CONTROLS, CTL_GETINFO)
        if ctrl_info and ctrl_info["status"] == 0 and ctrl_info["payload"]:
            n_controls = ctrl_info["payload"][0]
            for ci in range(min(n_controls, 4)):
                info = qc.request(F_CONTROLS, CTL_GETCINFO, data=bytes([ci]))
                if info and info["status"] == 0 and len(info["payload"]) >= 2:
                    self._controls[ci] = info["payload"][1]

        log.info(
            "connected: session=%d sensors=%d controls=%d session_timeout=%.1fs",
            qc.session,
            len(self._meta),
            len(self._controls),
            qc.session_timeout or -1,
        )
        # Arm the maintenance timers (relative to now). The first
        # KeepAlive fires on the very first loop iteration (KA-first).
        now = time.monotonic()
        self._probe_next = now + PROBE_INTERVAL
        self._refresh_next = now + FULL_REFRESH_INTERVAL
        self._lost_ka = 0

    def _usb_reset(self) -> None:
        """Escalation step 2: re-enumerate the USB device (via the client).

        The client owns the transport, so the actual fd close/re-open
        lives in :meth:`QLinkClient.usb_reset`. This wrapper only counts
        and absorbs failures — an escalation must never kill the loop.
        """
        assert self._qc is not None
        try:
            self._qc.usb_reset()
            self._usb_resets += 1
            log.warning("USB reset complete (total: %d)", self._usb_resets)
        except Exception as e:  # noqa: BLE001 — escalation must not kill the loop
            log.error("USB reset failed: %s", e)

    # ----------------------------------------------------------------- sample

    def _sample_once(self) -> bool:
        """One polling cycle over all active sensors + controls."""
        assert self._qc is not None
        qc = self._qc
        vals: dict[str, float] = {}
        for idx, (vtype, exponent) in self._meta.items():
            # Tight per-request timeout: the refresh must never stretch the
            # loop period far beyond KEEPALIVE_INTERVAL (the session dies
            # after SESSION_TIMEOUT). Healthy device: ~3 ms/request.
            resp = qc.request(F_SENSORS, S_GETVAL, data=bytes([idx]), retries=1, timeout=0.5)
            if resp is None or resp["status"] != 0:
                return False
            raw = decode_value(resp["payload"], vtype)
            if raw is None:
                continue
            value = scale(raw, exponent)
            if not within_bounds(idx, value):
                log.debug("sensor %d out of sanity bounds: %r — dropped", idx, value)
                continue
            if idx in SENSORS:
                vals[SENSORS[idx].metric] = value
        for ci, vtype in self._controls.items():
            resp = qc.request(F_CONTROLS, CTL_GETVAL, data=bytes([ci]), retries=1, timeout=0.5)
            if resp is None or resp["status"] != 0:
                return False
            raw = decode_value(resp["payload"], vtype)
            if raw is None:
                continue
            if ci in CONTROLS:
                vals[f"ctrl_{CONTROLS[ci].metric}"] = raw

        with self._lock:
            self._values.update(vals)
            self._last_ok = time.time()
            self._up = 1
            self._poll_updates += len(vals)
            self._last_update_source = "poll"
        return True

    # ------------------------------------------------------------ push path

    def _on_notification(self, pkt: ParsedPacket) -> None:
        """Handle a device-pushed ``SensorValueChanged`` notification.

        Invoked by the client's RX path the moment a notification packet
        arrives — independent of the poll cadence. Updates the cache with
        the exact reception timestamp so Grafana sees push-grade timing.
        """
        if pkt["feature"] != F_SENSORS or pkt["command"] != S_NOTIF_CHANGED:
            return
        now = time.time()
        with self._lock:
            self._notif_count += 1
            self._notif_last = now
            meta_snapshot = dict(self._meta)
        if not meta_snapshot:
            return  # metadata not ready yet (mid-connect)
        vtypes = {idx: vt for idx, (vt, _exp) in meta_snapshot.items()}
        pairs = parse_sensor_changed(pkt["payload"], vtypes)
        if not pairs:
            return
        vals: dict[str, float] = {}
        for idx, raw_val in pairs:
            meta = meta_snapshot.get(idx)
            if meta is None:
                continue
            _vtype, exponent = meta
            value = scale(raw_val, exponent)
            if not within_bounds(idx, value):
                log.debug("push sensor %d out of bounds: %r — dropped", idx, value)
                continue
            if idx in SENSORS:
                vals[SENSORS[idx].metric] = value
        if not vals:
            return
        with self._lock:
            self._values.update(vals)
            self._last_ok = now
            self._up = 1
            self._push_updates += len(vals)
            self._last_update_source = "push"
        log.debug("push: %d sensor(s) updated at %.3f", len(vals), now)

    def _disconnect(self) -> None:
        qc = self._qc
        if qc is not None:
            try:
                qc.close()
            except Exception:
                log.exception("error during disconnect")
            self._qc = None

    # -------------------------------------------------------------------- run

    def _probe(self) -> bool:
        """Single-sensor liveness probe (round-robin over active sensors).

        One request proves the telemetry engine is producing values and
        keeps ``bqio_data_age_seconds`` honest even if pushes stall.
        Round-robin ensures every sensor gets polled regularly (slow-moving
        sensors like uptime/error-state would otherwise starve).
        """
        assert self._qc is not None
        qc = self._qc
        if not self._meta:
            return False
        idxs = sorted(self._meta)
        idx = idxs[self._probe_cursor % len(idxs)]
        self._probe_cursor += 1
        vtype, exponent = self._meta[idx]
        # Tight timeout: a hung probe must NOT stretch the loop period
        # (the KeepAlive cadence is the session lifeline). One attempt,
        # 0.8 s ceiling — a healthy device answers in ~3 ms.
        resp = qc.request(F_SENSORS, S_GETVAL, data=bytes([idx]), retries=1, timeout=0.8)
        if resp is None or resp["status"] != 0:
            return False
        raw = decode_value(resp["payload"], vtype)
        if raw is None:
            return False
        value = scale(raw, exponent)
        if not within_bounds(idx, value):
            log.debug("probe sensor %d out of bounds: %r — dropped", idx, value)
            return False
        with self._lock:
            if idx in SENSORS:
                self._values[SENSORS[idx].metric] = value
            self._last_ok = time.time()
            self._up = 1
            self._poll_updates += 1
            self._last_update_source = "poll"
        return True

    def _keepalive_tick(self) -> None:
        """Send a KeepAlive ping and run the escalation ladder on failure.

        Escalation (consecutive "lost" pings, i.e. no response at all):
          * >= LOST_KA_BEFORE_REOPEN   -> session re-open (cheap)
          * >= LOST_KA_BEFORE_USB_RESET -> USB re-enumeration (invasive)
        An "expired" answer (status=1) is handled immediately with a
        session re-open — that is the expected signature of a timed-out
        session and is cheap to recover.
        """
        assert self._qc is not None
        qc = self._qc
        self._ka_sent += 1
        result = qc.keepalive(timeout=min(1.0, max(0.5, (qc.session_timeout or 2.0) / 2)))
        if result == "ok":
            self._lost_ka = 0
            return
        if result == "expired":
            self._ka_expired += 1
            log.warning("session expired (status=1) — reopening session")
            self._recover_session()
            return
        # "lost": no response at all — escalate (edge-triggered ladder:
        # re-open exactly once at the first threshold, USB re-enumeration
        # exactly once at the second; the counter resets afterwards so a
        # persistently wedged device cycles through the ladder instead of
        # hammering the recovery path every ping).
        self._lost_ka += 1
        log.warning("KeepAlive lost (%d consecutive)", self._lost_ka)
        if self._lost_ka == LOST_KA_BEFORE_USB_RESET:
            log.warning("escalating to USB reset (%d lost pings)", self._lost_ka)
            self._usb_reset()
            self._lost_ka = 0
        elif self._lost_ka == LOST_KA_BEFORE_REOPEN:
            log.warning("escalating to session re-open (%d lost pings)", self._lost_ka)
            self._recover_session()

    def _recover_session(self) -> None:
        """Re-open the QLink session (cheap recovery, no USB touch)."""
        assert self._qc is not None
        qc = self._qc
        try:
            new_sid = qc.reopen_session()
            self._reopens += 1
            log.info("session reopened: %d (total reopens: %d)", new_sid, self._reopens)
            # Metadata is unchanged (same device) — just re-arm the
            # maintenance timers. _lost_ka is left untouched: the caller
            # owns the escalation ladder.
            now = time.monotonic()
            self._probe_next = now + PROBE_INTERVAL
            self._refresh_next = now + FULL_REFRESH_INTERVAL
        except (ProtocolError, OSError) as e:
            log.error("session re-open failed: %s — full reconnect", e)
            self._disconnect()

    def run(self) -> None:
        """KeepAlive-driven collection loop (official QLink app profile).

        Primary data path: passive harvest of device-pushed notifications
        (zero requests). The session is kept alive with a KeepAlive ping
        every ``KEEPALIVE_INTERVAL`` (official app: SESSION_TIMEOUT/2) —
        the KeepAlive answer is the single source of truth for session
        health (``ok`` / ``expired`` / ``lost`` drives the escalation
        ladder). Maintenance: one liveness probe every
        ``PROBE_INTERVAL`` (data freshness, round-robin) and one full
        refresh every ``FULL_REFRESH_INTERVAL`` (completeness). A hard
        wall-clock watchdog tears down the session if any iteration
        stalls beyond ``HARD_ITER_LIMIT`` (kernel-level HID stall
        protection, INC-0012).
        """
        backoff = RECONNECT_BASE_DELAY
        while True:
            try:
                if self._qc is None:
                    self._connect()
                    backoff = RECONNECT_BASE_DELAY
                t0 = time.monotonic()
                qc = self._qc
                assert qc is not None  # guaranteed by the connect block above
                # --- session keep-alive FIRST (official app profile) ---
                # Sent at the TOP of every iteration so the 1 s cadence is
                # guaranteed no matter how long the harvest/probe/refresh
                # below take. The loop is paced to KEEPALIVE_INTERVAL
                # (= SESSION_TIMEOUT/2, the official app's keep-alive
                # period), so this yields exactly one ping per interval.
                # The answer is the single source of truth for session
                # health (ok / expired / lost drives the escalation).
                self._keepalive_tick()
                if self._qc is None:
                    # _recover_session gave up -> full reconnect path
                    time.sleep(backoff)
                    backoff = min(RECONNECT_MAX_DELAY, backoff * 2)
                    continue
                # --- primary path: harvest pushed notifications (no TX) ---
                qc.passive_read(PUSH_HARVEST_WINDOW)
                # --- maintenance: liveness probe (data freshness) ---
                if t0 >= self._probe_next:
                    # Informational: a failed probe usually means an
                    # expired session; the next KeepAlive tick detects
                    # that and recovers. We do not double-react here.
                    if self._probe():
                        with self._lock:
                            self._cycle_dur = time.monotonic() - t0
                    self._probe_next = time.monotonic() + PROBE_INTERVAL
                # --- maintenance: full refresh (completeness) ---
                if t0 >= self._refresh_next:
                    if self._sample_once():
                        with self._lock:
                            self._cycle_dur = time.monotonic() - t0
                    self._refresh_next = time.monotonic() + FULL_REFRESH_INTERVAL
            except DeviceNotFoundError as e:
                log.error("device gone: %s — backing off %.0fs", e, backoff)
                self._disconnect()
                time.sleep(backoff)
                backoff = min(RECONNECT_MAX_DELAY, backoff * 2)
                continue
            except ProtocolError as e:
                log.warning("protocol error: %s — reconnecting", e)
                self._disconnect()
                time.sleep(backoff)
                backoff = min(RECONNECT_MAX_DELAY, backoff * 2)
                continue
            except Exception:
                log.exception("unexpected collector error")
                self._disconnect()
                time.sleep(backoff)
                continue
            # --- hard watchdog: kernel-level stall protection ---
            elapsed = time.monotonic() - t0
            if elapsed > HARD_ITER_LIMIT:
                log.error(
                    "collector iteration took %.1fs (>%.0fs limit) — "
                    "forcing session teardown (possible HID/kernel stall)",
                    elapsed,
                    HARD_ITER_LIMIT,
                )
                self._disconnect()
                time.sleep(RECONNECT_BASE_DELAY)
                backoff = RECONNECT_BASE_DELAY
                continue
            # Passive harvest already consumed ~PUSH_HARVEST_WINDOW; sleep
            # the remainder of the keep-alive window so the loop ticks at
            # the KeepAlive cadence (the session dies after SESSION_TIMEOUT,
            # so the loop period must stay well below it).
            time.sleep(max(0.05, KEEPALIVE_INTERVAL - elapsed))

    # ----------------------------------------------------------------- render

    def render(self) -> str:
        """Render the Prometheus text exposition from cache (instant)."""
        with self._lock:
            vals = dict(self._values)
            up = self._up
            last_ok = self._last_ok
            cycle = self._cycle_dur
            notif_count = self._notif_count
            poll_count = self._poll_updates
            push_count = self._push_updates
            last_src = self._last_update_source
            ka_sent = self._ka_sent
            ka_expired = self._ka_expired
            reopens = self._reopens
            usb_resets = self._usb_resets

        lines: list[str] = []
        for idx in sorted(SENSORS):
            sd = SENSORS[idx]
            if not sd.active:
                continue
            name = f"{METRIC_PREFIX}_{sd.metric}"
            lines.append(f"# HELP {name} {sd.help}")
            lines.append(f"# TYPE {name} gauge")
            if sd.metric in vals:
                lines.append(f"{name} {vals[sd.metric]!r}")
        for ci in sorted(CONTROLS):
            cd = CONTROLS[ci]
            name = f"{METRIC_PREFIX}_ctrl_{cd.metric}"
            lines.append(f"# HELP {name} {cd.help}")
            lines.append(f"# TYPE {name} gauge")
            if f"ctrl_{cd.metric}" in vals:
                lines.append(f"{name} {vals[f'ctrl_{cd.metric}']!r}")

        # Derived: DC output power = sum(I*V) over all rails.
        dc_total = 0.0
        have_dc = False
        for cur, volt in (
            ("rail_3v3_current_amps", "rail_3v3_voltage_volts"),
            ("rail_5v_current_amps", "rail_5v_voltage_volts"),
            ("rail_12v1_current_amps", "rail_12v1_voltage_volts"),
        ):
            if cur in vals and volt in vals:
                dc_total += vals[cur] * vals[volt]
                have_dc = True
        if have_dc:
            lines.append("# HELP bqio_dc_output_power_w Sum of I*V over all rails (DC load)")
            lines.append("# TYPE bqio_dc_output_power_w gauge")
            lines.append(f"bqio_dc_output_power_w {dc_total!r}")

        # Derived: efficiency = DC load / AC input, EWMA-smoothed.
        ac = vals.get("ac_input_power_watts")
        if have_dc and ac and ac > 0:
            raw_eff = min(100.0, dc_total / ac * 100.0)
            # Data arrives at the device-native ~2 Hz push cadence.
            alpha = 1.0 - math.exp(-PUSH_HARVEST_WINDOW / EFFICIENCY_EWMA_TAU)
            with self._lock:
                if self._eff_ewma is None:
                    self._eff_ewma = raw_eff
                else:
                    self._eff_ewma = alpha * raw_eff + (1 - alpha) * self._eff_ewma
                eff = self._eff_ewma
            lines.append("# HELP bqio_efficiency_percent DC load / AC input (EWMA smoothed)")
            lines.append("# TYPE bqio_efficiency_percent gauge")
            lines.append(f"bqio_efficiency_percent {eff!r}")

        age = (time.time() - last_ok) if last_ok else float("nan")
        lines.append("# HELP bqio_up 1 if the PSU responded recently, else 0")
        lines.append("# TYPE bqio_up gauge")
        lines.append(f"bqio_up {up}")
        lines.append("# HELP bqio_data_age_seconds Seconds since last successful sample")
        lines.append("# TYPE bqio_data_age_seconds gauge")
        lines.append(f"bqio_data_age_seconds {age:.3f}")
        lines.append("# HELP bqio_cycle_duration_seconds Duration of the last sample cycle")
        lines.append("# TYPE bqio_cycle_duration_seconds gauge")
        lines.append(f"bqio_cycle_duration_seconds {cycle:.3f}")
        lines.append(
            "# HELP bqio_notifications_received Total SensorValueChanged notifications consumed"
        )
        lines.append("# TYPE bqio_notifications_received counter")
        lines.append(f"bqio_notifications_received {notif_count}")
        lines.append("# HELP bqio_poll_updates Total sensor values obtained via polling")
        lines.append("# TYPE bqio_poll_updates counter")
        lines.append(f"bqio_poll_updates {poll_count}")
        lines.append("# HELP bqio_push_updates Total sensor values obtained via push notifications")
        lines.append("# TYPE bqio_push_updates counter")
        lines.append(f"bqio_push_updates {push_count}")
        lines.append("# HELP bqio_last_update_source 1=poll, 2=push, 0=none (last data source)")
        lines.append("# TYPE bqio_last_update_source gauge")
        src_val = {"poll": 1, "push": 2, "none": 0}.get(last_src, 0)
        lines.append(f"bqio_last_update_source {src_val}")
        lines.append("# HELP bqio_keepalive_sent_total KeepAlive pings sent (session keep-alive)")
        lines.append("# TYPE bqio_keepalive_sent_total counter")
        lines.append(f"bqio_keepalive_sent_total {ka_sent}")
        lines.append(
            "# HELP bqio_keepalive_expired_total KeepAlive pings answered with status=1 (expired session)"
        )
        lines.append("# TYPE bqio_keepalive_expired_total counter")
        lines.append(f"bqio_keepalive_expired_total {ka_expired}")
        lines.append(
            "# HELP bqio_session_reopens_total QLink session re-opens performed (recovery)"
        )
        lines.append("# TYPE bqio_session_reopens_total counter")
        lines.append(f"bqio_session_reopens_total {reopens}")
        lines.append("# HELP bqio_usb_resets_total USB re-enumerations performed (escalation)")
        lines.append("# TYPE bqio_usb_resets_total counter")
        lines.append(f"bqio_usb_resets_total {usb_resets}")
        return "\n".join(lines) + "\n"


def serve(collector: Collector, host: str, port: int) -> None:
    """Minimal blocking HTTP server exposing /metrics."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(5)
    log.info(
        "bqio exporter v%s listening on %s:%d (push-primary; probe %.1fs, refresh %.0fs)",
        __version__,
        host,
        port,
        PROBE_INTERVAL,
        FULL_REFRESH_INTERVAL,
    )
    while True:
        conn, _addr = sock.accept()
        try:
            conn.recv(4096)  # consume the request line
            body = collector.render().encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/plain; version=0.04; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            )
            conn.sendall(header.encode("ascii") + body)
        except OSError:
            pass
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="bqio Prometheus exporter")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--device", default=None, help="/dev/hidraw node (default: auto-detect by VID:PID)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    collector = Collector(device=args.device)
    collector.start()
    serve(collector, args.host, args.port)


if __name__ == "__main__":
    main()
