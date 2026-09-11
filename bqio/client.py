"""bqio.client — session-aware high-level QLink API.

Wraps the raw transport (``hid_device``) and codec (``protocol``) into a
convenient, safety-conscious client:

* At most one session is ever held; opening while one is active raises.
* Every outgoing packet passes codec validation (length, ranges, CRC).
* ``CloseSession`` is attempted on exit even if the device is unresponsive
  (bounded timeout, so a wedged MCU cannot hang shutdown).
* Convenience methods for device info, serial number and feature bitmap.

Usage::

    with QLinkClient() as qc:
        qc.open_session()
        resp = qc.request(F_SENSORS, S_GETINFO)
        ...
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .hid_device import HidDevice, Transport, find_hidraw
from .protocol import (
    C_CLOSE,
    C_GETDEV,
    C_GETSERIAL,
    C_GETSUPP,
    C_KEEPALIVE,
    C_OPEN,
    CLIENT_WEB,
    F_DEVINFO,
    F_ROOT,
    PID,
    PKT,
    VID,
    ParsedPacket,
    ProtocolError,
    SessionError,
    build_packet,
    is_notification,
    parse_packet,
)

log = logging.getLogger("bqio.client")


class QLinkClient:
    """Session-aware QLink client.

    Parameters
    ----------
    device:
        ``/dev/hidraw`` node. Auto-detected via sysfs when omitted.
    rx_timeout:
        Seconds to wait for a response before retrying.
    retries:
        Number of attempts per request.
    verbose:
        Emit TX/RX debug logging.
    """

    def __init__(
        self,
        device: str | None = None,
        rx_timeout: float = 1.5,
        retries: int = 3,
        verbose: bool = False,
        on_notification: Callable[[ParsedPacket], None] | None = None,
    ) -> None:
        self.device = device or find_hidraw(VID, PID)
        self.rx_timeout = rx_timeout
        self.retries = retries
        self.verbose = verbose
        #: Called with every unsolicited notification packet (request_id == 0)
        #: as soon as it is parsed — including while waiting for a response.
        #: Callbacks must be fast and non-blocking (queue, don't I/O).
        self.on_notification = on_notification
        self._dev: Transport | None = None
        self._req_id = 1
        self._session: int | None = None
        #: Session lifetime reported by the device in OpenSession (seconds).
        #: The firmware expires the session after this interval of inactivity
        #: — afterwards every request is answered with status=1
        #: (INVALID_SESSION_ID). The official QLink app keeps the session
        #: alive with a KeepAlive ping every ``session_timeout / 2``.
        self._session_timeout: float | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> QLinkClient:
        self._dev = HidDevice(self.device).open()
        log.debug("opened %s", self.device)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the session (bounded) and release the device."""
        if self._session is not None:
            try:
                self.request(F_ROOT, C_CLOSE, session=self._session, retries=1, timeout=1.0)
                log.info("session %d closed cleanly", self._session)
            except ProtocolError as e:
                log.warning(
                    "CloseSession failed (%s) — device may hold the session until MCU reset", e
                )
            self._session = None
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    # -- session management -------------------------------------------------

    def open_session(self) -> int:
        """Open a QLink session. Returns the session id.

        Raises SessionError if the device does not acknowledge.
        """
        if self._session is not None:
            raise SessionError(f"session {self._session} already active — close it first")
        resp = self.request(F_ROOT, C_OPEN, data=bytes([CLIENT_WEB]), session=0)
        if resp is None or len(resp["payload"]) < 5:
            raise SessionError(f"OpenSession not acknowledged: {resp!r}")
        self._session = int(resp["payload"][4])
        if self._session == 0:
            raise SessionError("device returned session id 0 (handshake failed)")
        if len(resp["payload"]) > 6:
            self._session_timeout = float(resp["payload"][6])
        log.info(
            "session %d opened (state=%d timeout=%ds)",
            self._session,
            resp["payload"][5] if len(resp["payload"]) > 5 else -1,
            resp["payload"][6] if len(resp["payload"]) > 6 else -1,
        )
        return self._session

    @property
    def session(self) -> int | None:
        return self._session

    @property
    def session_timeout(self) -> float | None:
        """Session lifetime in seconds as reported by the device (OpenSession)."""
        return self._session_timeout

    def keepalive(self, timeout: float | None = None) -> str:
        """Send a KeepAlive ping (official QLink session-keep-alive command).

        The firmware expires the session after ``session_timeout`` seconds
        of inactivity; the official QLink app counters this with a periodic
        ``KeepAlive`` request (ROOT feature, command 3) every
        ``session_timeout / 2``. Without it, every subsequent request is
        answered with status=1 (INVALID_SESSION_ID) and the push stream
        stops — only a session re-open recovers.

        Returns
        -------
        "ok":
            Device acknowledged with SUCCESS — session is alive.
        "expired":
            Device answered with status=1 (INVALID_SESSION_ID) — the
            session timed out. Expected after >``session_timeout`` of
            silence; recover with :meth:`reopen_session` (cheap, no
            device re-enumeration).
        "lost":
            No response at all (timeout) — the device/MCU may be wedged.
        """
        if self._session is None:
            return "lost"
        resp = self.request(F_ROOT, C_KEEPALIVE, session=self._session, retries=1, timeout=timeout)
        if resp is None:
            return "lost"
        if resp["status"] == 0:
            return "ok"
        if resp["status"] == 1:
            return "expired"
        return "lost"

    def reopen_session(self) -> int:
        """Close the current session (bounded) and open a fresh one.

        Cheaper than a full device re-open: the USB file descriptor stays
        open, only the logical QLink session is recycled. Used to recover
        from an expired session (status=1 / INVALID_SESSION_ID) without
        touching the USB layer.

        Raises SessionError/ProtocolError if the new handshake fails.
        """
        old = self._session
        self._session = None
        if old is not None:
            try:
                self.request(F_ROOT, C_CLOSE, session=old, retries=1, timeout=1.0)
                log.info("session %d closed (pre-reopen)", old)
            except ProtocolError as e:
                log.warning("CloseSession before reopen failed: %s", e)
        return self.open_session()

    def usb_reset(self) -> None:
        """Escalation step 2: re-enumerate the USB device.

        Closes the HID file descriptor and re-opens the device node,
        forcing the kernel to re-talk to the endpoint. Recovers from
        transport-level wedges (endpoint stuck, no responses at all)
        where a session re-open is insufficient. More invasive than
        :meth:`reopen_session`; the caller decides when to escalate.

        Raises OSError if the device cannot be re-opened.
        """
        log.warning("USB reset: closing device fd and re-opening")
        if self._dev is not None:
            self._dev.close()
            self._dev = None
        node = find_hidraw()
        self.device = node
        self._dev = HidDevice(node).open()
        log.warning("USB reset complete: %s", node)

    # -- transport -----------------------------------------------------------

    def _next_request_id(self) -> int:
        self._req_id = (self._req_id + 1) % 256 or 1
        return self._req_id

    def _drain(self, timeout: float = 0.0) -> list[bytes]:
        out: list[bytes] = []
        dev = self._dev
        if dev is None:
            return out
        end = time.monotonic() + timeout
        while True:
            try:
                chunk = dev.read(PKT)
                if chunk:
                    out.append(chunk)
            except BlockingIOError:
                pass
            except OSError:
                break
            if time.monotonic() > end:
                break
        return out

    def request(
        self,
        feature: int,
        command: int,
        data: bytes = b"",
        session: int | None = None,
        retries: int | None = None,
        timeout: float | None = None,
    ) -> ParsedPacket | None:
        """Send a request and wait for the matching response.

        Matching is done on (feature, command, request_id) so unsolicited
        notifications (request_id == 0) are never mistaken for our answer.
        Returns None if no valid response arrived within the timeout.
        """
        if self._dev is None:
            raise ProtocolError("client not open (use 'with QLinkClient()')")
        if session is None:
            session = self._session or 0
        attempts = self.retries if retries is None else retries
        wait = self.rx_timeout if timeout is None else timeout

        for _ in range(attempts):
            rid = self._next_request_id()
            pkt = build_packet(session, rid, feature, command, data)
            if self.verbose:
                log.debug(
                    "TX feat=%d cmd=%d sess=%d req=%d data=%s",
                    feature,
                    command,
                    session,
                    rid,
                    data.hex(),
                )
            self._dev.write(pkt)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                for raw in self._drain(0.02):
                    parsed = parse_packet(raw)
                    if parsed is None:
                        continue
                    if self.verbose:
                        log.debug(
                            "RX feat=%d cmd=%d req=%d status=%d crc=ok",
                            parsed["feature"],
                            parsed["command"],
                            parsed["request"],
                            parsed["status"],
                        )
                    if is_notification(parsed):
                        self._dispatch_notification(parsed)
                        continue
                    if (
                        parsed["feature"] == feature
                        and parsed["command"] == command
                        and parsed["request"] == rid
                    ):
                        return parsed
                time.sleep(0.005)
        return None

    def _dispatch_notification(self, pkt: ParsedPacket) -> None:
        """Invoke the notification callback (best effort, never raises)."""
        cb = self.on_notification
        if cb is None:
            return
        try:
            cb(pkt)
        except Exception:
            log.exception("notification callback failed")

    def passive_read(self, timeout: float) -> list[ParsedPacket]:
        """Block up to ``timeout`` seconds collecting incoming packets.

        Sends NOTHING — purely listens. Returns every validly-parsed
        packet (responses AND notifications) in arrival order. Used by
        the hybrid sampler to harvest device-push notifications between
        poll cycles without disturbing the request/response stream.
        """
        if self._dev is None:
            raise ProtocolError("client not open (use 'with QLinkClient()')")
        out: list[ParsedPacket] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for raw in self._drain(0.02):
                parsed = parse_packet(raw)
                if parsed is None:
                    continue
                if is_notification(parsed):
                    self._dispatch_notification(parsed)
                out.append(parsed)
            time.sleep(0.005)
        return out

    # -- convenience wrappers -------------------------------------------------

    def get_supported_features(self) -> bytes | None:
        resp = self.request(F_ROOT, C_GETSUPP, data=b"\x00")
        return resp["payload"] if resp else None

    def get_serial_number(self) -> str | None:
        resp = self.request(F_DEVINFO, C_GETSERIAL)
        if not resp:
            return None
        printable = "".join(chr(b) for b in resp["payload"] if 32 <= b < 127)
        return printable.strip() or None

    def get_device_info(self) -> dict[str, int | bytes] | None:
        import struct

        resp = self.request(F_DEVINFO, C_GETDEV)
        if not resp or len(resp["payload"]) < 4:
            return None
        p = resp["payload"]
        return {
            "model_id": struct.unpack("<H", p[0:2])[0],
            "revision": struct.unpack("<H", p[2:4])[0],
            "raw": p,
        }
