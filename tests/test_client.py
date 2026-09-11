"""Tests for the session-aware client (bqio.client).

Drives QLinkClient against a FakeHidDevice (in-memory transport) so the
session handshake, request/response matching and error handling can be
exercised without a physical PSU.
"""

from __future__ import annotations

import pytest
from conftest import FakeHidDevice

from bqio.client import QLinkClient
from bqio.protocol import (
    C_CLOSE,
    C_GETDEV,
    C_GETSERIAL,
    C_GETSUPP,
    C_OPEN,
    F_DEVINFO,
    F_ROOT,
    F_SENSORS,
    S_GETINFO,
    ProtocolError,
    SessionError,
    build_packet,
)


def _install_fake(client: QLinkClient, fake: FakeHidDevice) -> None:
    """Bypass device auto-detection and wire the fake transport in."""
    client._dev = fake


def _next_rid(client: QLinkClient) -> int:
    """Predict the request id the client will use for its NEXT request.

    The client increments ``_req_id`` before returning it, so the upcoming
    id is ``(current + 1) % 256 or 1``.
    """
    return (client._req_id + 1) % 256 or 1


def test_open_session_handshake():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)

    # Prime the queue with the OpenSession ACK (session id 7 at payload[4]).
    ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
    fake.responses.append(ack)

    sid = client.open_session()
    assert sid == 7
    assert client.session == 7
    # The client must have transmitted an OpenSession request.
    assert len(fake.tx) >= 1


def test_double_open_session_raises():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
    fake.responses.append(ack)
    client.open_session()
    with pytest.raises(SessionError):
        client.open_session()


def test_request_matches_by_feature_command_reqid():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client._session = 7

    # A decoy with the WRONG request id must be ignored.
    decoy = build_packet(7, 99, F_SENSORS, S_GETINFO, data=bytes([13]))
    # The real answer arrives with the request id the client chose.
    answer = build_packet(7, _next_rid(client), F_SENSORS, S_GETINFO, data=bytes([13]))
    fake.responses.extend([decoy, answer])

    resp = client.request(F_SENSORS, S_GETINFO, retries=1, timeout=0.2)
    assert resp is not None
    assert resp["payload"] == bytes([13])


def test_request_times_out_when_unanswered():
    fake = FakeHidDevice()  # no responses queued
    client = QLinkClient(device="/dev/null", rx_timeout=0.05, retries=1)
    _install_fake(client, fake)
    client._session = 7
    assert client.request(F_SENSORS, S_GETINFO, timeout=0.1) is None


def test_get_serial_number_strips_non_printable():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client._session = 7
    serial = b"\x00\x00ABC123\x00"
    fake.responses.append(build_packet(7, _next_rid(client), F_DEVINFO, C_GETSERIAL, data=serial))
    assert client.get_serial_number() == "ABC123"


def test_get_supported_features_passthrough():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client._session = 7
    bitmap = bytes([0xFF, 0x00, 0x01])
    fake.responses.append(build_packet(7, _next_rid(client), F_ROOT, C_GETSUPP, data=bitmap))
    assert client.get_supported_features() == bitmap


def test_close_without_session_is_safe():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client.close()  # must not raise
    assert fake.closed


def test_request_before_open_raises():
    client = QLinkClient(device="/dev/null")
    with pytest.raises(ProtocolError):
        client.request(F_ROOT, C_CLOSE)


# ---------------------------------------------------------------- lifecycle


def test_context_manager_opens_and_closes(monkeypatch):
    """__enter__ wires a real HidDevice; __exit__ closes session + device."""
    import bqio.client as cx

    opened: list[FakeHidDevice] = []

    class FakeHid:
        def __init__(self, path: str) -> None:
            self.fake = FakeHidDevice()

        def open(self) -> FakeHidDevice:
            opened.append(self.fake)
            return self.fake

    monkeypatch.setattr(cx, "HidDevice", FakeHid)
    client = QLinkClient(device="/dev/null")
    with client:
        assert client._dev is not None
        dev = client._dev
        assert isinstance(dev, FakeHidDevice)
        # Queue an OpenSession ACK so close() can send a CleanClose.
        ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
        dev.responses.append(ack)
        client.open_session()
    assert opened[0].closed
    assert client._session is None


def test_close_without_session_skips_close_request():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client.close()
    assert fake.closed
    assert fake.tx == []  # no CloseSession frame sent


def test_close_with_failed_close_request_still_clears_session():
    """CloseSession unanswered -> warning logged, session cleared anyway."""
    fake = FakeHidDevice()  # no responses queued
    client = QLinkClient(device="/dev/null", rx_timeout=0.05, retries=1)
    _install_fake(client, fake)
    client._session = 7
    client.close()
    assert client._session is None
    assert fake.closed


def test_drain_without_device_returns_empty():
    client = QLinkClient(device="/dev/null")
    assert client._drain(0.01) == []


def test_drain_survives_oserror():
    class FlakyDev(FakeHidDevice):
        def read(self, n: int = 64) -> bytes:
            raise OSError("device vanished")

    client = QLinkClient(device="/dev/null")
    client._dev = FlakyDev()
    assert client._drain(0.01) == []


def test_verbose_logging_does_not_raise(caplog):
    import logging

    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null", verbose=True)
    _install_fake(client, fake)
    client._session = 7
    fake.responses.append(
        build_packet(7, _next_rid(client), F_SENSORS, S_GETINFO, data=bytes([13]))
    )
    with caplog.at_level(logging.DEBUG, logger="bqio.client"):
        resp = client.request(F_SENSORS, S_GETINFO, timeout=0.2)
    assert resp is not None
    joined = "\n".join(r.message for r in caplog.records)
    assert "TX feat=" in joined
    assert "RX feat=" in joined


def test_open_session_rejects_short_ack():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    # Payload shorter than 5 bytes -> handshake failure.
    fake.responses.append(build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0])))
    with pytest.raises(SessionError):
        client.open_session()


def test_open_session_rejects_session_id_zero():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    # Session id 0 at payload[4] -> device refused the handshake.
    fake.responses.append(
        build_packet(0, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 0, 1, 30]))
    )
    with pytest.raises(SessionError):
        client.open_session()


def test_get_serial_number_returns_none_when_unanswered():
    fake = FakeHidDevice()  # no responses
    client = QLinkClient(device="/dev/null", rx_timeout=0.05, retries=1)
    _install_fake(client, fake)
    client._session = 7
    assert client.get_serial_number() is None


def test_get_device_info_parses_model_and_revision():
    import struct

    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    client._session = 7
    payload = struct.pack("<HH", 0x0023, 0x0001)  # model id, revision
    fake.responses.append(build_packet(7, _next_rid(client), F_DEVINFO, C_GETDEV, data=payload))
    info = client.get_device_info()
    assert info is not None
    assert info["model_id"] == 0x0023
    assert info["revision"] == 1


def test_get_device_info_returns_none_when_unanswered():
    fake = FakeHidDevice()  # no responses
    client = QLinkClient(device="/dev/null", rx_timeout=0.05, retries=1)
    _install_fake(client, fake)
    client._session = 7
    assert client.get_device_info() is None


# ------------------------------------------------------- notifications


def _notif_frame(feature: int, command: int, payload: bytes) -> bytes:
    """Build an unsolicited notification frame (request_id=0, seq=0)."""
    return build_packet(0, 0, feature, command, data=payload)


def test_request_dispatches_interleaved_notification():
    """A notification arriving between TX and the real response must be
    dispatched to the callback AND not swallowed as the response."""
    from bqio.protocol import F_SENSORS, S_NOTIF_CHANGED

    fake = FakeHidDevice()
    got: list[dict] = []
    client = QLinkClient(device="/dev/null", on_notification=lambda p: got.append(p))
    _install_fake(client, fake)

    ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
    fake.responses.append(ack)
    client.open_session()

    # Queue: notification FIRST, then the real GetSensorInfo response.
    notif = _notif_frame(F_SENSORS, S_NOTIF_CHANGED, bytes([3, 0xD2, 0x11]))
    resp = build_packet(7, _next_rid(client), F_SENSORS, S_GETINFO, data=bytes([3, 2, 0]))
    fake.responses.extend([notif, resp])

    out = client.request(F_SENSORS, S_GETINFO, data=bytes([3]))
    assert out is not None
    assert out["request"] != 0  # got the real response, not the notification
    assert len(got) == 1
    assert got[0]["feature"] == F_SENSORS
    assert got[0]["command"] == S_NOTIF_CHANGED
    assert got[0]["payload"] == bytes([3, 0xD2, 0x11])


def test_passive_read_collects_without_transmitting():
    """passive_read must not write anything and must dispatch notifications."""
    from bqio.protocol import F_SENSORS, S_NOTIF_CHANGED

    fake = FakeHidDevice()
    got: list[dict] = []
    client = QLinkClient(device="/dev/null", on_notification=lambda p: got.append(p))
    _install_fake(client, fake)

    ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
    fake.responses.append(ack)
    client.open_session()
    tx_before = len(fake.tx)

    fake.responses.append(_notif_frame(F_SENSORS, S_NOTIF_CHANGED, bytes([5, 0x01, 0x00])))
    pkts = client.passive_read(timeout=0.15)

    assert len(fake.tx) == tx_before  # nothing transmitted
    assert len(pkts) == 1
    assert pkts[0]["request"] == 0
    assert len(got) == 1


def test_callback_exception_does_not_break_request_loop():
    """A raising callback must be contained — the response still arrives."""
    from bqio.protocol import F_SENSORS, S_NOTIF_CHANGED

    def boom(_pkt: dict) -> None:
        raise RuntimeError("callback exploded")

    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null", on_notification=boom)
    _install_fake(client, fake)

    ack = build_packet(7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7, 1, 30]))
    fake.responses.append(ack)
    client.open_session()

    fake.responses.append(_notif_frame(F_SENSORS, S_NOTIF_CHANGED, bytes([3, 0x01, 0x00])))
    resp = build_packet(7, _next_rid(client), F_SENSORS, S_GETINFO, data=bytes([3, 2, 0]))
    fake.responses.append(resp)

    out = client.request(F_SENSORS, S_GETINFO, data=bytes([3]))
    assert out is not None
    assert out["request"] != 0


# ---------------------------------------------------------------- keepalive


def _open(client: QLinkClient, fake: FakeHidDevice, timeout_bytes: bytes = bytes([1, 30])) -> None:
    """Handshake helper: prime the OpenSession ACK (sid 7, timeout LE16)."""
    ack = build_packet(
        7, _next_rid(client), F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 7]) + timeout_bytes
    )
    fake.responses.append(ack)
    client.open_session()


def test_open_session_parses_session_timeout():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    # payload[6] is parsed as a single unsigned byte today.
    _open(client, fake, bytes([0, 30]))
    assert client.session_timeout == 30.0


def test_keepalive_ok():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    _open(client, fake)
    fake.responses.append(build_packet(7, _next_rid(client), F_ROOT, 3))  # status=0
    assert client.keepalive() == "ok"


def test_keepalive_expired_on_status_one():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    _open(client, fake)
    pkt = bytearray(build_packet(7, _next_rid(client), F_ROOT, 3))
    pkt[3] = 1  # status = INVALID_SESSION_ID
    from bqio.protocol import crc16_ccitt

    cs = crc16_ccitt(bytes(pkt[:62]))
    pkt[62] = cs & 0xFF
    pkt[63] = cs >> 8
    fake.responses.append(bytes(pkt))
    assert client.keepalive() == "expired"


def test_keepalive_lost_when_no_response():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    _open(client, fake)
    # No response queued -> request() times out -> "lost"
    assert client.keepalive(timeout=0.05) == "lost"


def test_keepalive_without_session_is_lost():
    fake = FakeHidDevice()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    assert client.keepalive() == "lost"


def test_reopen_session_recycles_session_id():
    class RidGatedFake(FakeHidDevice):
        """Releases a deferred response only when the TX carrying the SAME
        request id arrives (real device: it answers the Open request only
        once it has received it — the Close request's drain must not eat
        the Open ACK)."""

        def __init__(self) -> None:
            super().__init__()
            self._deferred: list[tuple[int, bytes]] = []

        def defer(self, pkt: bytes) -> None:
            from bqio.protocol import parse_packet

            parsed = parse_packet(pkt)
            assert parsed is not None
            self._deferred.append((parsed["request"], pkt))

        def write(self, data: bytes) -> int:
            self.tx.append(bytes(data))
            tx_rid = data[4]
            for i, (rid, pkt) in enumerate(list(self._deferred)):
                if rid == tx_rid:
                    self.responses.append(pkt)
                    del self._deferred[i]
                    break
            return len(data)

    fake = RidGatedFake()
    client = QLinkClient(device="/dev/null")
    _install_fake(client, fake)
    _open(client, fake)
    assert client.session == 7
    # CloseSession ACK (queued now) + fresh OpenSession ACK (released when
    # the Open request hits the wire, mirroring real device behaviour).
    close_rid = _next_rid(client)
    open_rid = (close_rid + 1) % 256 or 1
    fake.responses.append(build_packet(7, close_rid, F_ROOT, C_CLOSE))
    fake.defer(build_packet(9, open_rid, F_ROOT, C_OPEN, data=bytes([0, 0, 0, 0, 9, 1, 30])))
    sid = client.reopen_session()
    assert sid == 9
    assert client.session == 9
