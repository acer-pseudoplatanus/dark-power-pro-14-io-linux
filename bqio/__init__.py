"""bqio — QLink USB telemetry for be quiet! IO-series power supplies.

A zero-runtime-dependency Python library and Prometheus exporter that talks
to the PSU's proprietary QLink protocol directly over USB-HID. No kernel
modules, no vendor software, no third-party packages — just the standard
library and the stock ``/dev/hidraw`` interface.

Layers (see ``docs/ARCHITECTURE.md``)::

    hid_device  ->  transport   (/dev/hidrawN I/O, device discovery)
    protocol    ->  codec       (frame build/parse, CRC-16-CCITT)
    registry    ->  data        (sensor/control definitions, decoding)
    client      ->  API         (session-aware high-level wrapper)
    cli / exporter / web        ->  front-ends

.. warning::
   The QLink protocol is **reverse-engineered** from packet captures of the
   official vendor tool. It is undocumented and may change without notice.
   This project is **not affiliated with be quiet! GmbH**.
"""

from __future__ import annotations

__version__ = "1.3.0"

__all__ = ["__version__"]
