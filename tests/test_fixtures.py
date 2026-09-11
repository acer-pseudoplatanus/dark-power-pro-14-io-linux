"""Fixture regression tests against REAL captured QLink traffic.

``tests/fixtures/`` holds 112 frames recorded from a physical PSU plus a
ground-truth manifest. These tests prove the codec still parses the exact
bytes the device emitted — a regression here means we would mis-talk to a
real PSU. They run fully offline.
"""

from __future__ import annotations

import hashlib
import math

from bqio.protocol import decode_value, parse_packet, scale
from tests.conftest import FIXTURES_DIR


def test_all_frames_are_crc_valid_packets(fixture_frames):
    """Every captured frame must parse as a well-formed QLink packet."""
    invalid = []
    for blob in fixture_frames:
        if parse_packet(blob) is None:
            invalid.append(hashlib.sha256(blob).hexdigest()[:12])
    assert not invalid, f"{len(invalid)} frames failed CRC/geometry: {invalid[:5]}"


def test_frame_inventory_integrity(manifest):
    """Files on disk match the manifest (name, size, sha256)."""
    for entry in manifest["frames"]:
        path = FIXTURES_DIR / entry["file"]
        assert path.exists(), f"missing fixture {entry['file']}"
        blob = path.read_bytes()
        assert len(blob) == entry["size"], f"size drift in {entry['file']}"
        digest = hashlib.sha256(blob).hexdigest()
        assert digest == entry["sha256"], f"sha256 drift in {entry['file']}"


def test_ground_truth_sensors_decode_to_finite_values(manifest):
    """Each ground-truth sensor's raw value decodes with its vtype/exponent."""
    sgt = manifest["sensor_ground_truth"]
    assert sgt, "manifest has no sensor ground truth"
    for idx_str, gt in sgt.items():
        if not gt.get("active"):
            continue
        raw = gt["raw_value_at_capture"]
        raw_bytes = raw.to_bytes(4, "little")
        decoded = decode_value(raw_bytes, gt["vtype"])
        assert decoded is not None, f"sensor {idx_str}: vtype {gt['vtype']} undecodable"
        scaled = scale(decoded, gt["exponent"])
        assert math.isfinite(scaled), f"sensor {idx_str}: non-finite {scaled}"


def test_known_sensor_raw_values_match_capture(manifest):
    """Spot-check the raw captures against independently known values.

    Sensor 0 (uptime) captured raw 13512 (0x34C8) and sensor 5 (AC input
    power) captured raw 3459 (0x0D83) — both uint16 LE. Locks the decode
    pipeline to reality.
    """
    sgt = manifest["sensor_ground_truth"]
    assert sgt["0"]["raw_value_at_capture"] == 13512
    assert sgt["5"]["raw_value_at_capture"] == 3459
    # And the codec reproduces them from the little-endian bytes.
    assert decode_value(bytes.fromhex("c834"), 2) == 13512
    assert decode_value(bytes.fromhex("830d"), 2) == 3459


def test_control_ground_truth_present(manifest):
    cgt = manifest["control_ground_truth"]
    assert "0" in cgt and "1" in cgt
    for _ci, gt in cgt.items():
        assert "value_at_capture" in gt
        assert "info_payload_hex" in gt
