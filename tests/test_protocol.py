"""Offline unit tests for the QLink codec (bqio.protocol).

These cover the pure frame-building/parsing functions — the parts where a
regression would corrupt traffic toward the device MCU. No device needed.
"""

from __future__ import annotations

import struct

import pytest

from bqio.protocol import (
    PKT,
    MalformedPacketError,
    build_packet,
    crc16_ccitt,
    decode_value,
    parse_packet,
    scale,
)

# --------------------------------------------------------------------- CRC


def test_crc16_initial_value():
    assert crc16_ccitt(b"") == 0xFFFF  # initial value, no bytes processed


def test_crc16_deterministic_and_length_sensitive():
    assert crc16_ccitt(b"abc") == crc16_ccitt(b"abc")
    assert crc16_ccitt(b"abc") != crc16_ccitt(b"abcd")


def test_crc16_detects_single_bit_flip():
    good = crc16_ccitt(bytes(range(32)))
    bad = bytearray(range(32))
    bad[5] ^= 1
    assert crc16_ccitt(bytes(bad)) != good


# --------------------------------------------------------------- framing


def test_build_packet_geometry():
    pkt = build_packet(0x00, 0x00, 0x00, 0x00)
    assert len(pkt) == PKT == 64
    assert pkt[0] == 6  # PAYLOAD_LENGTH = len(data)+6 = 0+6
    assert pkt[1] == 0  # sequence
    assert pkt[2] == 0  # session
    assert pkt[3] == 0  # status
    assert pkt[4] == 0  # request id
    assert pkt[5] == 0  # feature
    assert pkt[6] == 0  # command


def test_build_packet_roundtrip():
    pkt = build_packet(0x06, 0x01, 0x30, 0x03, data=bytes([0x0A]))
    parsed = parse_packet(pkt)
    assert parsed is not None
    assert parsed["session"] == 0x06
    assert parsed["request"] == 0x01
    assert parsed["feature"] == 0x30
    assert parsed["command"] == 0x03
    assert parsed["payload"] == bytes([0x0A])


def test_build_packet_with_payload_roundtrip():
    payload = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    pkt = build_packet(0x06, 0x02, 0x31, 0x01, data=payload)
    parsed = parse_packet(pkt)
    assert parsed is not None
    assert parsed["payload"] == payload


def test_build_packet_rejects_overlong_data():
    # The exact failure mode that wedged the production MCU: callers
    # producing malformed frames. The library must refuse locally.
    with pytest.raises(MalformedPacketError):
        build_packet(0, 0, 0, 0, data=b"x" * (PKT - 7 + 1))


def test_build_packet_rejects_out_of_range_fields():
    with pytest.raises(MalformedPacketError):
        build_packet(256, 0, 0, 0)
    with pytest.raises(MalformedPacketError):
        build_packet(0, 256, 0, 0)


def test_parse_packet_rejects_corrupt_crc():
    pkt = bytearray(build_packet(0, 0, 0, 0))
    pkt[62] ^= 0xFF  # corrupt CRC
    assert parse_packet(bytes(pkt)) is None


def test_parse_packet_rejects_wrong_length():
    assert parse_packet(b"\x00" * 63) is None


def test_parse_packet_handles_prefixed_report_id():
    # Some kernels deliver a leading report-ID byte (65-byte read).
    pkt = build_packet(0x06, 0x01, 0x30, 0x03, data=bytes([0x0A]))
    prefixed = b"\x00" + pkt
    parsed = parse_packet(prefixed)
    assert parsed is not None
    assert parsed["payload"] == bytes([0x0A])


# ------------------------------------------------------------ value decode


def test_decode_u8():
    assert decode_value(bytes([0x2A]), 0) == 42


def test_decode_i16_le_signed():
    assert decode_value(bytes([0xFF, 0xFF]), 1) == -1
    assert decode_value(bytes([0x01, 0x00]), 1) == 1


def test_decode_uint16_le():
    assert decode_value(bytes([0x34, 0x12]), 2) == 0x1234


def test_decode_float32_le():
    packed = struct.pack("<f", 12.5)
    assert abs(decode_value(packed, 9) - 12.5) < 1e-6


def test_decode_unknown_type_returns_none():
    assert decode_value(b"\x00", 99) is None


def test_scale_exponent():
    assert scale(1234, -2) == 12.34
    assert scale(100, 0) == 100
    assert scale(5, 1) == 50


# ------------------------------------------------------- notifications


def _notif_payload(sensor_pairs: list[tuple[int, int]]) -> bytes:
    """Build a SensorValueChanged payload: (idx, uint16-value) pairs."""
    out = bytearray()
    for idx, val in sensor_pairs:
        out.append(idx)
        out += val.to_bytes(2, "little")
    return bytes(out)


def test_is_notification_flags_zero_request_and_sequence():
    from bqio.protocol import is_notification

    assert is_notification({"request": 0, "sequence": 0}) is True
    assert is_notification({"request": 5, "sequence": 0}) is False
    assert is_notification({"request": 0, "sequence": 1}) is False


def test_parse_sensor_changed_decodes_multiple_pairs():
    from bqio.protocol import parse_sensor_changed

    # sensor 3 (temp, uint16) = 4550, sensor 5 (AC watts, uint16) = 1234
    payload = _notif_payload([(3, 4550), (5, 1234)])
    vtypes = {3: 2, 5: 2}
    pairs = parse_sensor_changed(payload, vtypes)
    assert pairs == [(3, 4550.0), (5, 1234.0)]


def test_parse_sensor_changed_stops_at_unknown_sensor():
    from bqio.protocol import parse_sensor_changed

    # sensor 99 not in vtypes -> parsing must stop cleanly, no exception
    payload = _notif_payload([(3, 100), (99, 200), (5, 300)])
    vtypes = {3: 2, 5: 2}
    pairs = parse_sensor_changed(payload, vtypes)
    assert pairs == [(3, 100.0)]


def test_parse_sensor_changed_handles_truncated_tail():
    from bqio.protocol import parse_sensor_changed

    # idx byte present but value bytes missing -> skipped, no crash
    payload = bytes([3, 0x01])  # idx 3, only 1 of 2 value bytes
    pairs = parse_sensor_changed(payload, {3: 2})
    assert pairs == []


def test_parse_sensor_changed_empty_payload():
    from bqio.protocol import parse_sensor_changed

    assert parse_sensor_changed(b"", {3: 2}) == []
