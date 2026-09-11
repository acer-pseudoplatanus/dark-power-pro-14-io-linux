"""bqio.protocol — QLink frame codec.

Pure, transport-agnostic frame building and parsing for the QLink protocol.
No I/O lives here: this module is a stateless codec that turns bytes into
validated frames and back, which makes it trivially unit-testable (and
regression-tested against captured traffic in ``tests/fixtures/``).

Wire format
-----------
64-byte HID reports ("small" frame layout)::

    [0]      PAYLOAD_LENGTH   = len(data) + 6
    [1]      SEQUENCE_ID      (bit7 = more frames follow)
    [2]      SESSION_ID
    [3]      STATUS           (request: 0 = success)
    [4]      REQUEST_ID       (matched against responses)
    [5]      FEATURE_ID
    [6]      COMMAND_ID
    [7..61]  DATA
    [62..63] CHECKSUM         CRC16-CCITT (init 0xFFFF, poly 0xA001), little-endian

Feature IDs
-----------
ROOT=1, DFU=2, DEVICE_INFO=3, STORAGE=4, HUB=5, USB_DEVICE=6, KEYBOARD=7,
MOUSE=8, BATTERY=9, PAIRING=10, LIGHTINGS=16, BINDINGS=17, MACROS=18,
NUMPAD=32, MEDIA_DOCK=33, SENSORS=48, CONTROLS=49, ARGB=50, DISPLAY=51,
KV_STORAGE=52, ONE_CORD=53, STANDALONE_DISPLAY=64

ROOT commands: OpenSession=1, CloseSession=2, KeepAlive=3,
GetSupportedFeatures=4, GetQLinkVersion=5, GetActiveSessionInfo=6,
RequestStateChange=7, SendStateChangeDecision=8

DEVICE_INFO commands: GetDeviceInfo=1, GetSerialNumber=2,
SetSerialNumber=3, ResetSerialNumber=4, FactoryReset=5

SENSORS commands: GetInfo=1, GetSensorInfo=2, GetSensorValue=3,
GetSensorConfig=4, SetSensorConfig=5. Unsolicited notifications use
feature=SENSORS, command=2 (SensorValueChanged), request_id=0.

CONTROLS commands: GetInfo=1, GetControlInfo=2, GetControlValue=3,
SetControlValue=4, GetControlConfig=5, SetControlConfig=6,
GetControlCurve=7, SetControlCurve=8, GetControlPID=9, SetControlPID=10

Client type: WEB = 2

Safety notes (learned the hard way)
-----------------------------------
* The device firmware tolerates exactly ONE active session. Always close
  the session when done — an orphaned session locks out every other
  client (including monitoring exporters) until the MCU resets.
* Sending malformed packets (wrong length, garbage payloads) can wedge the
  telemetry MCU permanently. Only a physical AC power cut recovers it.
  This module therefore validates every outgoing packet and refuses to
  build anything shorter than the 64-byte report size.
"""

from __future__ import annotations

import struct
from typing import TypedDict

#: HID report size in bytes (fixed for the small-frame layout).
PKT = 64

#: Vendor/Product IDs of the IO-series telemetry interface.
VID = 0x373F
PID = 0x0023


class ParsedPacket(TypedDict):
    """Fields extracted from a validated 64-byte HID report."""

    payload_length: int
    sequence: int
    session: int
    status: int
    request: int
    feature: int
    command: int
    payload: bytes
    raw: bytes


# --------------------------------------------------------------------------
# Feature / command constants
# --------------------------------------------------------------------------
F_ROOT = 1
F_DF = 2
F_DEVINFO = 3
F_SENSORS = 48
F_CONTROLS = 49

C_OPEN = 1
C_CLOSE = 2
C_KEEPALIVE = 3
C_GETSUPP = 4
C_GETQLINK = 5
C_GETACTIVE = 6

C_GETDEV = 1
C_GETSERIAL = 2
C_FACTORYRESET = 5

S_GETINFO = 1
S_GETSINFO = 2
S_GETVAL = 3
S_GETCFG = 4
S_SETCFG = 5

#: Unsolicited ``SensorValueChanged`` notification. Shares the numeric
#: command id with ``GetSensorInfo`` (both = 2); the two are told apart by
#: ``request_id == 0`` (plus ``sequence == 0``) on incoming packets.
S_NOTIF_CHANGED = 2


def is_notification(pkt: ParsedPacket) -> bool:
    """True for unsolicited device notifications (request_id == 0).

    The vendor web bundle marks a packet as a notification when both
    ``requestId`` and ``sequenceId`` are zero — responses to our own
    requests always carry a non-zero request id.
    """
    return pkt["request"] == 0 and pkt["sequence"] == 0


CTL_GETINFO = 1
CTL_GETCINFO = 2
CTL_GETVAL = 3
CTL_SETVAL = 4
CTL_GETCFG = 5

CLIENT_WEB = 2


class ProtocolError(Exception):
    """Base class for bqio protocol errors."""


class DeviceNotFoundError(ProtocolError):
    """No matching HID device found."""


class MalformedPacketError(ProtocolError):
    """An outgoing packet failed validation — refused to build."""


class SessionError(ProtocolError):
    """Session open/close failed."""


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def crc16_ccitt(data: bytes) -> int:
    """CRC16-CCITT (init 0xFFFF, poly 0xA001)."""
    acc = 0xFFFF
    for byte in data:
        acc ^= byte
        for _ in range(8):
            low = acc & 1
            acc >>= 1
            if low:
                acc ^= 0xA001
    return acc


def build_packet(
    session_id: int,
    req_id: int,
    feature: int,
    command: int,
    data: bytes = b"",
    seq: int = 0,
    total_frames: int = 1,
) -> bytes:
    """Build a validated 64-byte QLink packet.

    Raises MalformedPacketError instead of emitting garbage — a malformed
    packet can wedge the device MCU permanently.
    """
    if not 0 <= session_id <= 255:
        raise MalformedPacketError(f"session_id out of range: {session_id}")
    if not 0 <= req_id <= 255:
        raise MalformedPacketError(f"req_id out of range: {req_id}")
    if not 0 <= feature <= 255 or not 0 <= command <= 255:
        raise MalformedPacketError(f"feature/command out of range: {feature}/{command}")

    pkt = bytearray(PKT)
    pkt[0] = len(data) + 6  # PAYLOAD_LENGTH
    pkt[1] = seq + ((1 << 7) if (total_frames > 1 and seq < total_frames - 1) else 0)
    pkt[2] = session_id
    if seq == 0:
        pkt[3] = 0  # STATUS = success
        pkt[4] = req_id
        pkt[5] = feature
        pkt[6] = command
        off = 7
    else:
        off = 4
    if data:
        if len(data) > PKT - off:
            raise MalformedPacketError(f"data too long for report: {len(data)} > {PKT - off}")
        pkt[off : off + len(data)] = data
    checksum = crc16_ccitt(bytes(pkt[: PKT - 2]))
    pkt[PKT - 2] = checksum & 0xFF
    pkt[PKT - 1] = (checksum >> 8) & 0xFF
    assert len(pkt) == PKT
    return bytes(pkt)


def parse_packet(raw: bytes) -> ParsedPacket | None:
    """Parse and validate an incoming HID report.

    Returns None for short/garbage frames or bad checksums.

    Handles both 64-byte reads (this device) and 65-byte reads (some
    kernels prefix a report-ID byte): the interpretation whose CRC
    verifies wins.
    """
    if len(raw) < PKT:
        return None
    candidates = [raw[:PKT]]
    if len(raw) >= PKT + 1:
        candidates.append(raw[1 : 1 + PKT])
    for frame in candidates:
        checksum = crc16_ccitt(frame[: PKT - 2])
        stored = frame[PKT - 2] | (frame[PKT - 1] << 8)
        if checksum != stored:
            continue
        plen = frame[0]
        payload = frame[7 : 7 + max(0, plen - 6)]
        return {
            "payload_length": plen,
            "sequence": frame[1],
            "session": frame[2],
            "status": frame[3],
            "request": frame[4],
            "feature": frame[5],
            "command": frame[6],
            "payload": payload,
            "raw": frame,
        }
    return None


def decode_value(raw: bytes, value_type: int) -> float | None:
    """Decode a sensor value payload according to its value-type encoding.

    Value-type IDs (from ``GetSensorInfo``): 0=uint8, 1=int8, 2=uint16,
    3=int16, 4=uint32, 5=int32, 6=uint64, 7=int64, 8=half, 9=float,
    10=double. Returns None for unknown types or undecodable payloads.
    """
    formats: dict[int, tuple[int, str]] = {
        0: (1, "B"),
        1: (1, "b"),
        2: (2, "H"),
        3: (2, "h"),
        4: (4, "I"),
        5: (4, "i"),
        6: (8, "Q"),
        7: (8, "q"),
        8: (2, "e"),
        9: (4, "f"),
        10: (8, "d"),
    }
    spec = formats.get(value_type)
    if spec is None:
        return None
    size, fmt = spec
    buf = raw[:size].ljust(size, b"\x00")
    try:
        return float(struct.unpack("<" + fmt, buf)[0])
    except struct.error:
        return None


def signed_byte(b: int) -> int:
    """Interpret a single byte as signed (sensor exponent field).

    The QLink ``GetSensorInfo`` payload encodes the decimal exponent as a
    single *signed* byte (e.g. ``0xFE`` = -2). Reading it unsigned yields
    254 and scales values by 10**254 — the classic "e+258" symptom.
    """
    return b - 256 if b >= 128 else b


def scale(value: float, exponent: int) -> float:
    """Apply the sensor's decimal exponent (10**exp)."""
    return float(value * (10**exponent))


def value_size(value_type: int) -> int | None:
    """Wire size in bytes for a QLink value-type id (None if unknown)."""
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 8, 7: 8, 8: 2, 9: 4, 10: 8}
    return sizes.get(value_type)


def parse_sensor_changed(payload: bytes, vtypes: dict[int, int]) -> list[tuple[int, float]]:
    """Decode a ``SensorValueChanged`` notification payload.

    Layout (from the vendor web bundle ``onSensorValueChanged``): a
    sequence of ``(sensor_index, raw_value)`` pairs where the raw value
    occupies ``value_size(vtype)`` bytes. Unknown sensor indices or
    truncated tails are skipped rather than raised — notifications are
    best-effort and must never take down the consumer.

    Returns a list of ``(sensor_index, scaled_value)`` tuples. Scaling
    uses exponent 0 here; callers that track per-sensor exponents should
    re-scale (the exporter caches exponents from ``GetSensorInfo``).
    """
    out: list[tuple[int, float]] = []
    pos = 0
    while pos < len(payload):
        idx = payload[pos]
        pos += 1
        vtype = vtypes.get(idx)
        size = value_size(vtype) if vtype is not None else None
        if size is None or pos + size > len(payload):
            break  # unknown sensor or truncated pair — stop parsing
        raw = payload[pos : pos + size]
        pos += size
        assert vtype is not None  # guaranteed by the size check above
        val = decode_value(raw, vtype)
        if val is not None:
            out.append((idx, val))
    return out
