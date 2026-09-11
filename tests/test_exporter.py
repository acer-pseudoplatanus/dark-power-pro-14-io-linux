"""Tests for the Prometheus exporter (bqio.exporter).

The Collector's sampler thread needs a live device, so these tests drive
the *renderer* directly by seeding its cache — which is exactly the code
path a Prometheus scrape exercises.
"""

from __future__ import annotations

import threading

import pytest

from bqio.exporter import Collector
from bqio.registry import SENSORS


def _seeded_collector(**values) -> Collector:
    col = Collector.__new__(Collector)  # bypass Thread.__init__
    col._lock = threading.Lock()
    col._values = dict(values)
    col._last_ok = 1.0
    col._cycle_dur = 0.4
    col._up = 1
    col._meta = {}
    col._controls = {}
    col._eff_ewma = None
    col._notif_count = 0
    col._notif_last = 0.0
    col._push_updates = 0
    col._poll_updates = 0
    col._last_update_source = "none"
    col._probe_cursor = 0
    col._ka_sent = 0
    col._ka_expired = 0
    col._reopens = 0
    col._usb_resets = 0
    return col


def test_render_emits_help_and_type_for_every_active_sensor():
    col = _seeded_collector(ac_input_power_watts=120.0)
    text = col.render()
    for _idx, sd in SENSORS.items():
        if not sd.active:
            continue
        name = f"bqio_{sd.metric}"
        assert f"# HELP {name} " in text
        assert f"# TYPE {name} gauge" in text


def test_render_includes_present_values():
    col = _seeded_collector(temperature_celsius=45.5, fan_speed_rpm=1200)
    text = col.render()
    assert "bqio_temperature_celsius 45.5" in text
    assert "bqio_fan_speed_rpm 1200" in text


def test_render_derived_dc_power_and_efficiency():
    col = _seeded_collector(
        rail_3v3_current_amps=1.0,
        rail_3v3_voltage_volts=3.3,
        rail_5v_current_amps=2.0,
        rail_5v_voltage_volts=5.0,
        rail_12v1_current_amps=5.0,
        rail_12v1_voltage_volts=12.0,
        ac_input_power_watts=100.0,
    )
    text = col.render()
    # DC = 3.3 + 10.0 + 60.0 = 73.3 W ; efficiency = 73.3/100 = 73.3 %
    assert "bqio_dc_output_power_w 73.3" in text
    assert "bqio_efficiency_percent" in text
    # EWMA seeded on first sample equals the raw ratio.
    assert "bqio_efficiency_percent 73.3" in text


def test_render_reports_up_and_age():
    col = _seeded_collector()
    text = col.render()
    assert "bqio_up 1" in text
    assert "bqio_data_age_seconds" in text
    assert "bqio_cycle_duration_seconds 0.4" in text


def test_render_clamps_efficiency_at_100():
    col = _seeded_collector(
        rail_12v1_current_amps=10.0,
        rail_12v1_voltage_volts=12.0,
        ac_input_power_watts=50.0,  # 120 W DC / 50 W AC = 240 % -> clamp
    )
    text = col.render()
    assert "bqio_efficiency_percent 100.0" in text


def test_signed_byte_interpretation():
    from bqio.protocol import signed_byte

    assert signed_byte(0x7F) == 127
    assert signed_byte(0x80) == -128
    assert signed_byte(0xFF) == -1


# ------------------------------------------------------------------ sampler
#
# The sampler thread needs a live device; instead we inject a scripted fake
# client into the collector and drive _sample_once() / _connect() directly.
# This exercises the exact code path the background thread runs.


class FakeQC:
    """Scripted stand-in for QLinkClient (only the methods the sampler uses)."""

    def __init__(self) -> None:
        self.session = 7
        self.session_timeout = 2.0
        self.closed = False
        self.passive_reads = 0
        self._dev: object | None = None
        self.keepalive_results: list[str] = []  # scripted: "ok"/"expired"/"lost"
        self.keepalive_calls = 0
        self.reopen_calls = 0
        self.usb_reset_calls = 0

    def __enter__(self) -> FakeQC:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def open_session(self) -> int:
        return 7

    def reopen_session(self) -> int:
        self.reopen_calls += 1
        return 8

    def usb_reset(self) -> None:
        self.usb_reset_calls += 1

    def keepalive(self, timeout: float | None = None) -> str:
        self.keepalive_calls += 1
        if self.keepalive_results:
            return self.keepalive_results.pop(0)
        return "ok"

    def passive_read(self, window: float = 0.5) -> None:
        """Listen-only RX — no-op in the fake (no pushed notifications)."""
        self.passive_reads += 1

    def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> dict | None:
        from bqio.protocol import (
            CTL_GETCINFO,
            CTL_GETINFO,
            CTL_GETVAL,
            F_CONTROLS,
            F_SENSORS,
            S_GETINFO,
            S_GETSINFO,
            S_GETVAL,
        )

        if (feature, command) == (F_SENSORS, S_GETINFO):
            return {"status": 0, "payload": bytes([13, 0xFF, 0x0F]), "raw": b""}
        if (feature, command) == (F_SENSORS, S_GETSINFO):
            return {"status": 0, "payload": bytes([0, 2, 0]), "raw": b""}  # uint16, exp 0
        if (feature, command) == (F_SENSORS, S_GETVAL):
            import struct

            values = {0: 100, 2: 1200, 3: 45, 5: 120, 11: 10, 12: 12}
            idx = data[0]
            return {"status": 0, "payload": struct.pack("<H", values.get(idx, 0)), "raw": b""}
        if (feature, command) == (F_CONTROLS, CTL_GETINFO):
            return {"status": 0, "payload": bytes([2]), "raw": b""}
        if (feature, command) == (F_CONTROLS, CTL_GETCINFO):
            return {"status": 0, "payload": bytes([0, 0]), "raw": b""}
        if (feature, command) == (F_CONTROLS, CTL_GETVAL):
            return {"status": 0, "payload": bytes([0]), "raw": b""}
        return None


def _sampler_collector() -> Collector:
    col = Collector.__new__(Collector)
    col.device = None
    col._lock = threading.Lock()
    col._values = {}
    col._last_ok = 0.0
    col._cycle_dur = 0.0
    col._up = 0
    col._meta = {}
    col._controls = {}
    col._eff_ewma = None
    col._qc = None
    col._notif_count = 0
    col._notif_last = 0.0
    col._push_updates = 0
    col._poll_updates = 0
    col._last_update_source = "none"
    col._probe_cursor = 0
    col._ka_sent = 0
    col._ka_expired = 0
    col._reopens = 0
    col._usb_resets = 0
    col._lost_ka = 0
    col._probe_next = 0.0
    col._refresh_next = 0.0
    return col


def test_connect_populates_metadata(monkeypatch: pytest.MonkeyPatch):
    import bqio.exporter as ex

    col = _sampler_collector()
    qc = FakeQC()
    monkeypatch.setattr(ex, "QLinkClient", lambda *a, **k: qc)
    col._connect()
    assert col._qc is qc
    assert 3 in col._meta  # temperature
    assert col._meta[3] == (2, 0)
    assert 0 in col._controls
    assert 1 in col._controls


def test_sample_once_updates_cache():
    col = _sampler_collector()
    col._qc = FakeQC()
    col._meta = {3: (2, 0), 2: (2, 0), 5: (2, 0)}
    col._controls = {0: 0}
    assert col._sample_once() is True
    assert col._up == 1
    assert col._values["temperature_celsius"] == 45.0
    assert col._values["fan_speed_rpm"] == 1200.0
    assert col._values["ac_input_power_watts"] == 120.0
    assert col._values["ctrl_fan_mode"] == 0.0
    assert col._last_ok > 0


def test_sample_once_failure_returns_false():
    class BrokenQC(FakeQC):
        def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> None:
            return None

    col = _sampler_collector()
    col._qc = BrokenQC()
    col._meta = {3: (2, 0)}
    col._controls = {}
    assert col._sample_once() is False
    assert col._up == 0


def test_disconnect_closes_client():
    col = _sampler_collector()
    qc = FakeQC()
    col._qc = qc
    col._disconnect()
    assert qc.closed
    assert col._qc is None


def test_disconnect_without_client_is_safe():
    col = _sampler_collector()
    col._disconnect()  # must not raise


# ------------------------------------------------------------------- run()
#
# The run() loop is driven with a scripted fake client and a patched
# time.sleep that raises a sentinel after a few iterations — covering the
# success path, the fail-counter/reconnect path and the device-lost path.


class _StopLoop(BaseException):
    """Sentinel to escape run()'s broad `except Exception` handlers."""


def test_run_success_then_interval_sleep(monkeypatch):
    import bqio.exporter as ex

    col = _sampler_collector()
    col._qc = FakeQC()
    col._meta = {3: (2, 0)}
    col._controls = {}
    monkeypatch.setattr(ex.time, "sleep", lambda s: (_ for _ in ()).throw(_StopLoop()))
    with pytest.raises(_StopLoop):
        col.run()
    assert col._cycle_dur > 0  # successful cycle recorded its duration
    assert col._qc.passive_reads >= 1  # push harvest ran (primary path)
    assert col._qc.keepalive_calls >= 1  # keep-alive tick fired (session health oracle)


def test_run_lost_keepalive_escalates_to_reopen_and_usb_reset(monkeypatch):
    """Lost KeepAlive pings climb the escalation ladder: re-open, then USB reset."""
    import bqio.exporter as ex

    col = _sampler_collector()
    qc = FakeQC()
    qc._dev = None  # no real transport; _usb_reset's find_hidraw() will fail safely
    qc.keepalive_results = ["lost"] * 10
    col._qc = qc
    col._meta = {3: (2, 0)}
    col._controls = {}

    clock = [1000.0]
    monkeypatch.setattr(ex.time, "monotonic", lambda: clock[0])

    sleeps: list[float] = []

    def fake_sleep(sec: float) -> None:
        sleeps.append(sec)
        clock[0] += sec
        if len(sleeps) >= 8:
            raise _StopLoop

    monkeypatch.setattr(ex.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        col.run()
    # 8 iterations = 8 lost pings. While the counter climbs, the cheap
    # session re-open is retried (stage 1); at 6 consecutive losses the
    # ladder escalates to a USB re-enumeration (stage 2).
    assert qc.reopen_calls >= 1
    assert qc.usb_reset_calls == 1
    assert col._reopens >= 1
    assert col._ka_sent == 8


def test_run_hard_watchdog_forces_teardown(monkeypatch):
    """An iteration slower than HARD_ITER_LIMIT tears the session down."""
    import bqio.exporter as ex

    col = _sampler_collector()
    qc = FakeQC()
    col._qc = qc
    col._meta = {3: (2, 0)}
    col._controls = {}

    clock = [1000.0]
    monkeypatch.setattr(ex.time, "monotonic", lambda: clock[0])

    def slow_passive_read(window: float = 0.5) -> None:
        clock[0] += ex.HARD_ITER_LIMIT + 10  # simulate a wedged HID read

    qc.passive_read = slow_passive_read

    def fake_sleep(sec: float) -> None:
        raise _StopLoop

    monkeypatch.setattr(ex.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        col.run()
    assert qc.closed  # watchdog forced the session teardown


# ------------------------------------------------------------------- probe()


def test_probe_updates_cache_and_marks_poll():
    col = _sampler_collector()
    col._qc = FakeQC()
    col._meta = {3: (2, 0)}
    col._controls = {}
    assert col._probe() is True
    assert col._values["temperature_celsius"] == 45.0
    assert col._last_update_source == "poll"
    assert col._poll_updates == 1
    assert col._up == 1


def test_probe_failure_returns_false():
    class BrokenQC(FakeQC):
        def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> None:
            return None

    col = _sampler_collector()
    col._qc = BrokenQC()
    col._meta = {3: (2, 0)}
    assert col._probe() is False
    assert col._up == 0


def test_probe_falls_back_when_probe_sensor_inactive():
    col = _sampler_collector()
    col._qc = FakeQC()
    col._meta = {5: (2, 0)}  # probe sensor 3 not active
    assert col._probe() is True
    assert "ac_input_power_watts" in col._values


def test_probe_no_metadata_returns_false():
    col = _sampler_collector()
    col._qc = FakeQC()
    col._meta = {}
    assert col._probe() is False


# ------------------------------------------------------------------- serve()


def test_serve_responds_to_raw_http_request():
    """serve() is a minimal blocking HTTP server — hit it with a raw socket.

    The server thread is a daemon; it dies with the test process.
    """
    import socket as socket_mod
    import threading as th

    import bqio.exporter as ex

    s = socket_mod.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    col = _seeded_collector(temperature_celsius=45.5)
    t = th.Thread(target=ex.serve, args=(col, "127.0.0.1", port), daemon=True)
    t.start()
    # The server thread reaches listen() asynchronously — retry the connect
    # briefly instead of racing it (flaky on slow CI runners).
    import time as time_mod

    deadline = time_mod.monotonic() + 5.0
    while True:
        try:
            client = socket_mod.create_connection(("127.0.0.1", port), timeout=5)
            break
        except ConnectionRefusedError:
            if time_mod.monotonic() >= deadline:
                raise
            time_mod.sleep(0.01)
    client.sendall(b"GET /metrics HTTP/1.1\r\n\r\n")
    data = b""
    while b"\r\n\r\n" not in data or len(data.split(b"\r\n\r\n", 1)[-1]) < 100:
        chunk = client.recv(65536)
        if not chunk:
            break
        data += chunk
    client.close()
    assert b"HTTP/1.1 200 OK" in data
    assert b"text/plain; version=0.04" in data
    assert b"bqio_up 1" in data
    assert b"bqio_temperature_celsius 45.5" in data


# --------------------------------------------------------------------- main()


def test_exporter_main_wires_collector_and_serve(monkeypatch):
    import sys

    import bqio.exporter as ex

    started: list[bool] = []
    served: list[bool] = []

    class FakeCol:
        def __init__(self, device: str | None = None) -> None:
            pass

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(ex, "Collector", FakeCol)
    monkeypatch.setattr(ex, "serve", lambda *a, **k: served.append(True))
    monkeypatch.setattr(sys, "argv", ["bqio-exporter", "--port", "19999"])
    ex.main()
    assert started and served


# ------------------------------------------------------- push path


def _notif_pkt(payload: bytes) -> dict:
    return {
        "payload_length": len(payload) + 6,
        "sequence": 0,
        "session": 0,
        "status": 0,
        "request": 0,
        "feature": 48,  # F_SENSORS
        "command": 2,  # S_NOTIF_CHANGED
        "payload": payload,
        "raw": b"",
    }


def test_on_notification_updates_cache_with_scaled_values():
    col = _sampler_collector()
    # sensor 3 = temp (uint16, exp -2 -> 4550 = 45.5 degC),
    # sensor 5 = AC watts (uint16, exp -2 -> 1234 = 12.34 W)
    col._meta = {3: (2, -2), 5: (2, -2)}
    # idx3=4550 (0x11C6 LE), idx5=1234 (0x04D2 LE)
    payload = bytes([3, 0xC6, 0x11, 5, 0xD2, 0x04])
    col._on_notification(_notif_pkt(payload))
    assert col._values["temperature_celsius"] == 45.5
    assert col._values["ac_input_power_watts"] == 12.34
    assert col._notif_count == 1
    assert col._push_updates == 2
    assert col._last_update_source == "push"
    assert col._up == 1


def test_on_notification_ignores_non_sensor_features():
    col = _sampler_collector()
    col._meta = {3: (2, 0)}
    pkt = _notif_pkt(bytes([3, 0x01, 0x00]))
    pkt["feature"] = 49  # not F_SENSORS
    col._on_notification(pkt)
    assert col._notif_count == 0
    assert col._values == {}


def test_on_notification_skipped_while_meta_missing():
    col = _sampler_collector()
    col._meta = {}  # mid-connect: metadata not ready
    col._on_notification(_notif_pkt(bytes([3, 0x01, 0x00])))
    assert col._values == {}
    assert col._push_updates == 0


def test_render_reports_hybrid_accounting_metrics():
    col = _seeded_collector(ac_input_power_watts=120.0)
    col._notif_count = 42
    col._poll_updates = 100
    col._push_updates = 30
    col._last_update_source = "push"
    text = col.render()
    assert "bqio_notifications_received 42" in text
    assert "bqio_poll_updates 100" in text
    assert "bqio_push_updates 30" in text
    assert "bqio_last_update_source 2" in text  # 2 = push
