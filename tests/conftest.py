"""Shared test fixtures and helpers.

Loads the captured QLink traffic from ``tests/fixtures/`` (real frames
recorded from a physical PSU) and provides a fake HID transport for
client-layer tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


@pytest.fixture(scope="session")
def manifest() -> dict:
    """Parsed ground-truth manifest (sensor vtypes, frame inventory)."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def fixture_frames(manifest) -> list[bytes]:
    """All captured frames, in manifest order (each 64 bytes, CRC-valid)."""
    out: list[bytes] = []
    for entry in manifest["frames"]:
        blob = (FIXTURES_DIR / entry["file"]).read_bytes()
        assert len(blob) == entry["size"], f"size mismatch for {entry['file']}"
        out.append(blob)
    return out


class FakeHidDevice:
    """In-memory stand-in for :class:`bqio.hid_device.HidDevice`.

    Feeds queued response frames to ``read()`` and records everything
    written. Used to exercise the client layer without a physical PSU.
    """

    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses: list[bytes] = list(responses or [])
        self.tx: list[bytes] = []
        self.closed = False

    def read(self, n: int = 64) -> bytes:
        if not self.responses:
            raise BlockingIOError("fake device drained")
        return self.responses.pop(0)

    def write(self, data: bytes) -> int:
        self.tx.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.closed = True

    def open(self) -> FakeHidDevice:
        return self

    def __enter__(self) -> FakeHidDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
