"""Tests for the HID transport layer (bqio.hid_device).

Uses a synthetic sysfs tree under tmp_path — no real USB involved.
"""

from __future__ import annotations

import pytest

from bqio.hid_device import HidDevice, find_hidraw
from bqio.protocol import DeviceNotFoundError


def _make_sysfs_tree(tmp_path, vendor="373f", product="0023"):
    """Mirror the real sysfs layout:

    <base>/hidraw0/device -> .../usb3/3-2/3-2:1.0/0003:373F:0023.0008
    idVendor/idProduct live on the USB *device* node (.../usb3/3-2).
    """
    usb_dev = tmp_path / "usb3" / "3-2"
    iface = usb_dev / "3-2:1.0" / "0003:373F:0023.0008"
    iface.mkdir(parents=True)
    (usb_dev / "idVendor").write_text(vendor)
    (usb_dev / "idProduct").write_text(product)
    hidraw = tmp_path / "hidraw_class" / "hidraw0"
    hidraw.mkdir(parents=True)
    (hidraw / "device").symlink_to(iface)
    return tmp_path / "hidraw_class"


def test_find_hidraw_walks_up_to_usb_device(tmp_path):
    base = _make_sysfs_tree(tmp_path)
    assert find_hidraw(base_dir=str(base)) == "/dev/hidraw0"


def test_find_hidraw_rejects_unknown_vendor(tmp_path):
    base = _make_sysfs_tree(tmp_path, vendor="dead")
    with pytest.raises(DeviceNotFoundError):
        find_hidraw(base_dir=str(base))


def test_hid_device_read_write_requires_open(tmp_path):
    dev = HidDevice(str(tmp_path / "none"))
    with pytest.raises(OSError):
        dev.read()
    with pytest.raises(OSError):
        dev.write(b"")


def test_hid_device_context_manager(tmp_path):
    node = tmp_path / "hidraw0"
    node.write_bytes(b"")
    with HidDevice(str(node)) as dev:
        assert dev.fd is not None
    assert dev.fd is None  # closed on exit


def test_find_hidraw_missing_class_raises_clean_error(tmp_path):
    """No /sys/class/hidraw (e.g. container without the module) must raise
    DeviceNotFoundError — not a raw FileNotFoundError traceback."""
    from bqio.protocol import DeviceNotFoundError

    with pytest.raises(DeviceNotFoundError, match="HID class not available"):
        find_hidraw(base_dir=str(tmp_path / "does-not-exist"))
