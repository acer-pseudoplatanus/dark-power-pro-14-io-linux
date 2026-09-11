"""bqio.hid_device — USB-HID transport layer.

Owns the raw ``/dev/hidrawN`` file descriptor and locates the device node
by USB vendor/product ID. This is the only module that touches the kernel
interface; everything above it is transport-agnostic and testable in
isolation.

Design notes
------------
* **No ``hidapi``.** We read the stock ``hidraw`` character device directly.
  The kernel's ``usbhid`` driver already hands us the 64-byte reports; a
  userspace libusb binding would only add a dependency for no gain.
* **Discovery walks sysfs upward.** ``idVendor``/``idProduct`` live on the
  USB *device* node, an ancestor of the HID *interface* node — so we climb
  the tree until we find them. Assuming ``/dev/hidraw0`` is fragile (node
  numbering shifts after re-enumeration or when other HID devices appear).
"""

from __future__ import annotations

import contextlib
import os
from typing import Protocol, runtime_checkable

from .protocol import PID, PKT, VID


@runtime_checkable
class Transport(Protocol):
    """Structural interface for anything that can carry QLink frames.

    ``HidDevice`` (real ``/dev/hidraw``) and the test double both satisfy
    this, which keeps the client layer transport-agnostic and testable.
    """

    def read(self, n: int = PKT) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


class HidDevice:
    """A thin, context-managed wrapper around a ``/dev/hidrawN`` node.

    Parameters
    ----------
    node:
        Absolute path to the HID character device (e.g. ``/dev/hidraw0``).
    nonblocking:
        Open with ``O_NONBLOCK`` so reads never stall the caller. The
        client layer implements its own bounded waits on top of this.
    """

    def __init__(self, node: str, *, nonblocking: bool = True) -> None:
        self.node = node
        self._nonblocking = nonblocking
        self._fd: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> HidDevice:
        if self._fd is not None:
            return self
        flags = os.O_RDWR
        if self._nonblocking:
            flags |= os.O_NONBLOCK
        self._fd = os.open(self.node, flags)
        return self

    def close(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None

    @property
    def fd(self) -> int | None:
        return self._fd

    def __enter__(self) -> HidDevice:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.close()

    # -- I/O ---------------------------------------------------------------

    def read(self, n: int = PKT) -> bytes:
        """Read up to ``n`` bytes. Raises ``BlockingIOError`` when empty."""
        if self._fd is None:
            raise OSError("device not open")
        return os.read(self._fd, n)

    def write(self, data: bytes) -> int:
        if self._fd is None:
            raise OSError("device not open")
        return os.write(self._fd, data)


def find_hidraw(vid: int = VID, pid: int = PID, base_dir: str = "/sys/class/hidraw") -> str:
    """Locate the ``/dev/hidraw`` node for the given USB device via sysfs.

    Walks up from each HID interface node to the USB device node to read
    ``idVendor``/``idProduct``. Returns the first matching ``/dev/hidrawN``.

    Raises
    ------
    DeviceNotFoundError
        If no HID node matches ``(vid, pid)``.
    """
    from .protocol import DeviceNotFoundError  # local import: avoid cycle

    def _read_int(path: str) -> int:
        with open(path) as fh:
            return int(fh.read().strip(), 16)

    if not os.path.isdir(base_dir):
        raise DeviceNotFoundError(
            f"HID class not available ({base_dir} missing) — "
            "kernel module hidraw not loaded or not a Linux host"
        )

    candidates: list[str] = []
    for entry in sorted(os.listdir(base_dir)):
        path = f"{base_dir}/{entry}/device"
        try:
            cur = os.path.realpath(path)
        except OSError:
            continue
        vendor = product = None
        while cur and cur != "/":
            try:
                if os.path.exists(f"{cur}/idVendor"):
                    vendor = _read_int(f"{cur}/idVendor")
                    product = _read_int(f"{cur}/idProduct")
                    break
            except (OSError, ValueError):
                break
            cur = os.path.dirname(cur)
        if vendor == vid and product == pid:
            candidates.append(f"/dev/{entry}")
    if not candidates:
        raise DeviceNotFoundError(
            f"no /dev/hidraw node for {vid:04x}:{pid:04x} — is the PSU "
            f"plugged in and the kernel HID driver bound?"
        )
    return candidates[0]
