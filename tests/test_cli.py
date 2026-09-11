"""Tests for the diagnostic CLI (bqio.cli).

The CLI is tested against a scripted fake client (monkeypatched into
``bqio.cli.QLinkClient``) so every command handler runs end-to-end without
a physical PSU. Values are chosen to sit inside the registry's sanity
bounds so the happy paths are deterministic.
"""

from __future__ import annotations

import argparse
import runpy
import struct
import sys

import pytest

import bqio.cli as cli
from bqio.protocol import (
    CTL_GETINFO,
    CTL_GETVAL,
    F_CONTROLS,
    F_SENSORS,
    S_GETINFO,
    S_GETSINFO,
    S_GETVAL,
    DeviceNotFoundError,
    SessionError,
)

# 13 active sensors (bits 0..12) -> bitmap FF 0F.
BITMAP = bytes([0xFF, 0x0F])
# Per-index uint16 values, all inside the registry sanity bounds.
VALUES = {
    0: 100,  # uptime
    1: 100,  # total runtime
    2: 1200,  # fan rpm
    3: 45,  # temperature
    4: 0,  # error state
    5: 120,  # AC input power
    6: 230,  # AC input voltage
    7: 5,  # 3.3V current
    8: 3,  # 3.3V voltage
    9: 2,  # 5V current
    10: 5,  # 5V voltage
    11: 10,  # 12V1 current
    12: 12,  # 12V1 voltage
}


def _pkt(status: int, payload: bytes) -> dict:
    """Minimal ParsedPacket-shaped dict (the CLI only reads status/payload)."""
    return {"status": status, "payload": payload, "raw": b""}


class ScriptedClient:
    """Fake QLinkClient answering from a (feature, command) table."""

    def __init__(self, device: str | None = None, verbose: bool = False) -> None:
        self.device = device
        self.verbose = verbose
        self.session = 7
        self.closed = False
        self.fail_open = False

    def __enter__(self) -> ScriptedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def open_session(self) -> int:
        if self.fail_open:
            raise SessionError("simulated orphaned session")
        return 7

    def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> dict | None:
        if (feature, command) == (F_SENSORS, S_GETINFO):
            return _pkt(0, bytes([13]) + BITMAP)
        if (feature, command) == (F_SENSORS, S_GETSINFO):
            return _pkt(0, bytes([0, 2, 0]))  # vtype=uint16, exponent=0
        if (feature, command) == (F_SENSORS, S_GETVAL):
            idx = data[0]
            return _pkt(0, struct.pack("<H", VALUES[idx]))
        if (feature, command) == (F_CONTROLS, CTL_GETINFO):
            return _pkt(0, bytes([2]))
        if (feature, command) == (F_CONTROLS, CTL_GETVAL):
            return _pkt(0, struct.pack("<B", 0))
        return None

    def get_device_info(self) -> dict[str, object] | None:
        return {
            "model_id": 0x1234,
            "revision": 1,
            "mcu_versions": [{"id": 0, "major": 1, "middle": 7, "minor": 0, "title": "1.7.0"}],
        }

    def get_serial_number(self) -> str | None:
        return "SN-TEST-001"

    def get_qlink_version(self) -> str | None:
        return "1.0.22"

    def get_supported_features(self) -> bytes | None:
        return bytes([0xFF, 0x00])

    def get_kv_entries(self) -> list[dict[str, int]] | None:
        return [{"index": 1, "value_len": 8}]


def _ns(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {"cmd": "sensors", "device": None, "verbose": False, "count": 1}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> ScriptedClient:
    fake = ScriptedClient()
    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: fake)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)  # no real waits
    return fake


# ------------------------------------------------------------------ detect


def test_cmd_detect_found(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(cli, "find_hidraw", lambda: "/dev/hidraw3")
    assert cli.cmd_detect(_ns(cmd="detect")) == 0
    assert "/dev/hidraw3" in capsys.readouterr().out


def test_cmd_detect_not_found(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def boom() -> str:
        raise DeviceNotFoundError("no matching device")

    monkeypatch.setattr(cli, "find_hidraw", boom)
    assert cli.cmd_detect(_ns(cmd="detect")) == 1
    assert "NOT FOUND" in capsys.readouterr().out


# ------------------------------------------------------------------- info


def test_cmd_info(fake_cli: ScriptedClient, capsys: pytest.CaptureFixture[str]):
    assert cli.cmd_info(_ns(cmd="info")) == 0
    out = capsys.readouterr().out
    assert "ModelId:" in out
    assert "SN-TEST-001" in out
    assert "Features:" in out
    assert fake_cli.closed


# ---------------------------------------------------------------- sensors


def test_cmd_sensors(fake_cli: ScriptedClient, capsys: pytest.CaptureFixture[str]):
    assert cli.cmd_sensors(_ns(cmd="sensors")) == 0
    out = capsys.readouterr().out
    assert "Sensor count: 13" in out
    assert "fan_speed_rpm" in out
    assert "temperature_celsius" in out
    assert "control[0]" in out
    assert "control[1]" in out


def test_cmd_sensors_no_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    class Silent(ScriptedClient):
        def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> None:
            return None

    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: Silent())
    assert cli.cmd_sensors(_ns(cmd="sensors")) == 1
    assert "no response" in capsys.readouterr().out


def _neg_exp_fixture() -> type:
    """ScriptedClient with realistic raw values + per-sensor exponents.

    Mirrors the real device: volts/watts sensors carry exponent -2
    (0xFE), counters/temperature/fan carry 0. Raw values are the
    *scaled* figures times 100 (so exponent -2 restores them).
    """
    RAW = {
        0: 100,
        1: 100,
        2: 1200,
        3: 45,
        4: 0,  # exponent 0
        5: 18250,
        6: 23100,
        7: 500,
        8: 33100,  # exponent -2
        9: 100,
        10: 50500,
        11: 13810,
        12: 11990,  # exponent -2
    }
    EXP = {
        0: 0,
        1: 0,
        2: 0,
        3: 0,
        4: 0,
        5: 0xFE,
        6: 0xFE,
        7: 0xFE,
        8: 0xFE,
        9: 0xFE,
        10: 0xFE,
        11: 0xFE,
        12: 0xFE,
    }

    class NegExp(ScriptedClient):
        def request(
            self, feature: int, command: int, data: bytes = b"", **kw: object
        ) -> dict | None:
            if (feature, command) == (F_SENSORS, S_GETSINFO):
                return _pkt(0, bytes([0, 2, EXP[data[0]]]))  # uint16, exp
            if (feature, command) == (F_SENSORS, S_GETVAL):
                idx = data[0]
                return _pkt(0, struct.pack("<H", RAW[idx]))
            return super().request(feature, command, data=data, **kw)

    return NegExp


def test_cmd_sensors_negative_exponent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Regression: the exponent field is a *signed* byte.

    The real device reports e.g. 0xFE (-2) for volts/watts sensors.
    Reading it unsigned (254) produced absurd values like 2.31e+258.
    """
    neg = _neg_exp_fixture()
    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: neg())
    assert cli.cmd_sensors(_ns(cmd="sensors")) == 0
    out = capsys.readouterr().out
    # 23100 raw, exponent -2 -> 231.0 (AC input voltage)
    assert "= 231.0" in out
    # 1200 raw, exponent 0 -> 1200.0 (fan rpm, unaffected)
    assert "= 1200.0" in out
    assert "e+" not in out.lower()


def test_cmd_watch_negative_exponent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """Same regression, via the watch path (shares the meta lookup)."""
    neg = _neg_exp_fixture()
    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: neg())
    assert cli.cmd_watch(_ns(cmd="watch", count=1)) == 0
    out = capsys.readouterr().out
    # fan: 1200 raw, exponent 0 -> 1200.000
    assert "1200.000" in out
    # AC voltage: 23100 raw, exponent -2 -> 231.000
    assert "231.000" in out
    assert "e+" not in out.lower()


# --------------------------------------------------------------- controls


def test_cmd_controls(fake_cli: ScriptedClient, capsys: pytest.CaptureFixture[str]):
    assert cli.cmd_controls(_ns(cmd="controls")) == 0
    out = capsys.readouterr().out
    assert "control[0]" in out
    assert "control[1]" in out


def test_dump_controls_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    class NoControls(ScriptedClient):
        def request(self, feature: int, command: int, data: bytes = b"", **kw: object) -> None:
            return None

    fake = NoControls()
    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: fake)
    cli._dump_controls(fake)
    assert "(controls unavailable)" in capsys.readouterr().out


# ------------------------------------------------------------------ watch


def test_cmd_watch(fake_cli: ScriptedClient, capsys: pytest.CaptureFixture[str]):
    assert cli.cmd_watch(_ns(cmd="watch", count=2)) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Header line + 2 sample rows.
    assert len(lines) >= 3
    assert "[ 2]" in lines[0]


# ----------------------------------------------------------- close-orphan


def test_cmd_close_orphan_clean(fake_cli: ScriptedClient, capsys: pytest.CaptureFixture[str]):
    assert cli.cmd_close_orphan(_ns(cmd="close-orphan")) == 0
    assert "OK: no orphaned session" in capsys.readouterr().out


def test_cmd_close_orphan_detected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    fake = ScriptedClient()
    fake.fail_open = True
    monkeypatch.setattr(cli, "QLinkClient", lambda *a, **k: fake)
    assert cli.cmd_close_orphan(_ns(cmd="close-orphan")) == 1
    out = capsys.readouterr().out
    assert "LIKELY ORPHAN" in out
    assert "AC power cut" in out


# ------------------------------------------------------------------- main


def test_main_routes_subcommands(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "find_hidraw", lambda: "/dev/hidraw0")
    assert cli.main(["detect"]) == 0


def test_main_requires_subcommand():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_main_device_not_found_maps_to_exit_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """DeviceNotFoundError inside a handler -> clean ERROR line, exit 1."""

    def boom(ns) -> int:
        raise DeviceNotFoundError("no matching device")

    monkeypatch.setattr(cli, "cmd_detect", boom)
    assert cli.main(["detect"]) == 1
    err = capsys.readouterr().err
    assert "ERROR:" in err
    assert "Traceback" not in err


def test_main_permission_denied_maps_to_exit_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    def boom(ns) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(cli, "cmd_detect", boom)
    assert cli.main(["detect"]) == 1
    assert "permission denied" in capsys.readouterr().err.lower()


def test_main_keyboard_interrupt_maps_to_exit_130(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    def boom(ns) -> int:
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "cmd_detect", boom)
    assert cli.main(["detect"]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_python_minus_m_help(monkeypatch: pytest.MonkeyPatch):
    """``python -m bqio --help`` exits cleanly (covers bqio.__main__)."""
    monkeypatch.setattr(sys, "argv", ["bqio", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("bqio", run_name="__main__")
    assert exc.value.code == 0
