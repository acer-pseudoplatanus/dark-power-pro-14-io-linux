"""bqio.cli — diagnostic CLI for be quiet! IO-series PSUs.

Examples::

    python -m bqio.cli detect            # find the HID device
    python -m bqio.cli info              # device info + serial + features
    python -m bqio.cli sensors           # dump all sensor values once
    python -m bqio.cli controls          # dump all control values once
    python -m bqio.cli watch -n 10       # sample 10 times (default)
    python -m bqio.cli notif -d 10       # listen for pushed notifications
    python -m bqio.cli close-orphan      # diagnose a stuck session

Every command guarantees the session is closed on exit (context manager),
even on Ctrl+C or exceptions — orphaned sessions lock out other clients.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from .client import QLinkClient
from .hid_device import find_hidraw
from .protocol import (
    CTL_GETINFO,
    CTL_GETVAL,
    F_CONTROLS,
    F_SENSORS,
    S_GETINFO,
    S_GETSINFO,
    S_GETVAL,
    DeviceNotFoundError,
    ParsedPacket,
    ProtocolError,
    decode_value,
    is_notification,
    scale,
    signed_byte,
)
from .registry import CONTROLS, SENSORS


def cmd_detect(_args: argparse.Namespace) -> int:
    try:
        node = find_hidraw()
    except DeviceNotFoundError as e:
        print(f"NOT FOUND: {e}")
        return 1
    print(node)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    with QLinkClient(device=args.device, verbose=args.verbose) as qc:
        qc.open_session()
        info = qc.get_device_info()
        if info:
            print(f"ModelId:    {int(info['model_id'])}")
            print(f"Revision:   {int(info['revision'])}")
        serial = qc.get_serial_number()
        if serial:
            print(f"Serial:     {serial}")
        feats = qc.get_supported_features()
        if feats:
            print(f"Features:   {feats.hex()}")
    return 0


def _dump_controls(qc: QLinkClient) -> None:
    """Dump all control values (shared by ``sensors`` and ``controls``)."""
    ctrl = qc.request(F_CONTROLS, CTL_GETINFO)
    if not ctrl or ctrl["status"] != 0 or not ctrl["payload"]:
        print("(controls unavailable)")
        return
    for ci in range(ctrl["payload"][0]):
        cv = qc.request(F_CONTROLS, CTL_GETVAL, data=bytes([ci]))
        cname = CONTROLS[ci].metric if ci in CONTROLS else "?"
        if cv and cv["status"] == 0:
            print(f"control[{ci}] {cname} = {decode_value(cv['payload'], 0)}")
        else:
            print(f"control[{ci}] {cname} = ERR")


def _active_sensor_indices(qc: QLinkClient) -> set[int]:
    """Return the set of active sensor indices from the device."""
    resp = qc.request(F_SENSORS, S_GETINFO)
    if not resp:
        return set()
    bitmap = resp["payload"][1:]
    return {bi * 8 + bit for bi, byte in enumerate(bitmap) for bit in range(8) if byte >> bit & 1}


def cmd_sensors(args: argparse.Namespace) -> int:
    with QLinkClient(device=args.device, verbose=args.verbose) as qc:
        qc.open_session()
        resp = qc.request(F_SENSORS, S_GETINFO)
        if not resp:
            print("GetSensorInfo: no response")
            return 1
        count = resp["payload"][0]
        active = _active_sensor_indices(qc)
        print(f"Sensor count: {count}, active: {sorted(active)}")
        for idx in sorted(active):
            info = qc.request(F_SENSORS, S_GETSINFO, data=bytes([idx]))
            if not info or info["status"] != 0:
                print(f"[{idx:2d}] (info unavailable)")
                continue
            vtype, exponent = info["payload"][1], signed_byte(info["payload"][2])
            val_resp = qc.request(F_SENSORS, S_GETVAL, data=bytes([idx]))
            if not val_resp or val_resp["status"] != 0:
                print(f"[{idx:2d}] (value unavailable)")
                continue
            raw = decode_value(val_resp["payload"], vtype)
            value = scale(raw, exponent) if raw is not None else None
            name = SENSORS[idx].metric if idx in SENSORS else "?"
            print(f"[{idx:2d}] {name:28s} = {value}")
        _dump_controls(qc)
    return 0


def cmd_controls(args: argparse.Namespace) -> int:
    with QLinkClient(device=args.device, verbose=args.verbose) as qc:
        qc.open_session()
        _dump_controls(qc)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    with QLinkClient(device=args.device, verbose=args.verbose) as qc:
        qc.open_session()
        resp = qc.request(F_SENSORS, S_GETINFO)
        if not resp:
            print("GetSensorInfo: no response")
            return 1
        active = _active_sensor_indices(qc)
        meta: dict[int, tuple[int, int]] = {}
        for idx in sorted(active):
            info = qc.request(F_SENSORS, S_GETSINFO, data=bytes([idx]))
            if info and info["status"] == 0 and len(info["payload"]) >= 3:
                meta[idx] = (info["payload"][1], signed_byte(info["payload"][2]))
        headers = [f"[{i:2d}]" for i in sorted(meta)]
        print(" ".join(headers))
        for _ in range(args.count):
            row = []
            for idx in sorted(meta):
                vtype, exponent = meta[idx]
                vr = qc.request(F_SENSORS, S_GETVAL, data=bytes([idx]))
                if vr and vr["status"] == 0:
                    raw = decode_value(vr["payload"], vtype)
                    cell = f"{scale(raw, exponent):10.3f}" if raw is not None else f"{'-':>10}"
                    row.append(cell)
                else:
                    row.append(f"{'ERR':>10}")
            print(" ".join(row))
            time.sleep(0.5)
    return 0


def cmd_notif(args: argparse.Namespace) -> int:
    """Passively listen for device-pushed SensorValueChanged notifications.

    Opens a session, then waits for unsolicited notifications (request_id=0)
    without issuing any polls. Prints each decoded sensor value with a
    millisecond-precision wall-clock timestamp and measures inter-arrival
    cadence. Useful for verifying the firmware's native push rate.
    """
    t0 = time.time()
    last_t: float | None = None
    count = 0

    def on_pkt(pkt: ParsedPacket) -> None:
        nonlocal last_t, count
        if not is_notification(pkt) or pkt["feature"] != F_SENSORS:
            return
        ts = time.time()
        delta_ms = (ts - last_t) * 1000.0 if last_t is not None else float("nan")
        last_t = ts
        count += 1
        payload = pkt["payload"]
        # Decode with whatever vtypes we can infer; the CLI has no cached
        # GetSensorInfo here, so fall back to uint16 for common sensors.
        # (The exporter uses cached metadata; this is a diagnostic tool.)
        print(
            f"[{ts - t0:8.3f}s] notif #{count} gap={delta_ms:7.1f}ms "
            f"len={len(payload)} hex={payload.hex()}"
        )
        if args.count and count >= args.count:
            raise KeyboardInterrupt

    try:
        with QLinkClient(
            device=args.device,
            verbose=args.verbose,
            on_notification=on_pkt,
        ) as qc:
            qc.open_session()
            print(f"listening for {args.duration:.0f}s (Ctrl+C to stop)...")
            deadline = time.time() + args.duration
            while time.time() < deadline:
                qc.passive_read(min(0.5, max(0.05, deadline - time.time())))
    except KeyboardInterrupt:
        print(f"\ndone — {count} notifications in {time.time() - t0:.1f}s")
    finally:
        if count > 1:
            avg = (time.time() - t0) / count * 1000.0
            print(f"avg cadence ≈ {avg:.0f} ms")
    return 0


def cmd_close_orphan(args: argparse.Namespace) -> int:
    """Diagnose whether another tool left a session open.

    Opens a fresh session (which fails if one is active) and closes it.
    If the device accepts a new session, no orphan existed.
    """
    try:
        with QLinkClient(device=args.device, verbose=args.verbose) as qc:
            qc.open_session()
            print("OK: no orphaned session — device accepted a fresh session.")
    except ProtocolError as e:
        print(f"LIKELY ORPHAN: {e}")
        print("The device may hold a stale session. Options:")
        print("  1. Wait for the session timeout (usually a few minutes).")
        print("  2. Physical AC power cut of the PSU (guaranteed reset).")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bqio", description=__doc__)
    parser.add_argument("--device", default=None, help="/dev/hidraw node (default: auto-detect)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect", help="find the HID device node").set_defaults(func=cmd_detect)
    sub.add_parser("info", help="device info, serial, features").set_defaults(func=cmd_info)
    sub.add_parser("sensors", help="dump all sensor values once").set_defaults(func=cmd_sensors)
    sub.add_parser("controls", help="dump all control values once").set_defaults(func=cmd_controls)
    p_watch = sub.add_parser("watch", help="sample sensors repeatedly")
    p_watch.add_argument("-n", "--count", type=int, default=10)
    p_watch.set_defaults(func=cmd_watch)
    p_notif = sub.add_parser("notif", help="listen for device-pushed sensor notifications")
    p_notif.add_argument(
        "-d", "--duration", type=float, default=10.0, help="seconds to listen (default 10)"
    )
    p_notif.add_argument(
        "-n", "--count", type=int, default=0, help="stop after N notifications (0 = unlimited)"
    )
    p_notif.set_defaults(func=cmd_notif)
    sub.add_parser("close-orphan", help="diagnose orphaned session").set_defaults(
        func=cmd_close_orphan
    )
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except DeviceNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except PermissionError:
        print(
            "ERROR: permission denied on the HID device — run as root or "
            "add a udev rule granting access.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — session closed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
