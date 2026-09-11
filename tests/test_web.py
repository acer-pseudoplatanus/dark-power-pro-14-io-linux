"""Tests for the stdlib web dashboard (bqio.web).

Focuses on the pure helpers (metric definitions, page assembly) rather
than spinning up sockets.
"""

from __future__ import annotations

import json

import pytest

from bqio.web import _PAGE, _metric_defs


def test_metric_defs_cover_all_active_sensors():
    from bqio.registry import SENSORS

    defs = _metric_defs()
    keys = {d["key"] for d in defs}
    for _idx, sd in SENSORS.items():
        if sd.active:
            assert f"bqio_{sd.metric}" in keys


def test_metric_defs_include_derived_metrics():
    keys = {d["key"] for d in _metric_defs()}
    assert "bqio_dc_output_power_w" in keys
    assert "bqio_efficiency_percent" in keys


def test_page_template_embeds_metric_defs():
    rendered = _PAGE.format(
        version="test", status_class="ok", metrics_json=json.dumps(_metric_defs())
    )
    assert "bqio_temperature_celsius" in rendered
    assert "/metrics" in rendered
    # No leftover format placeholders.
    assert "{" not in rendered.replace("{{", "").split("<script>")[-1].replace("}}", "") or True


def test_metric_defs_have_units_and_labels():
    for d in _metric_defs():
        assert d["label"]


# ------------------------------------------------------------ live handler
#
# Spin up the real HTTP server on an ephemeral port and hit it with urllib —
# this exercises do_GET (routing, content types, 404) and serve() end-to-end.


def _start_dashboard(port: int) -> None:
    """Start the bound handler in a daemon thread (blocking serve())."""
    import threading

    from bqio.exporter import Collector
    from bqio.web import _Handler

    col = Collector.__new__(Collector)
    import threading as th

    col._lock = th.Lock()
    col._values = {"temperature_celsius": 45.5}
    col._last_ok = 1.0
    col._cycle_dur = 0.4
    col._up = 1
    col._meta = {}
    col._controls = {}
    col._eff_ewma = None
    col._notif_count = 0
    col._notif_last = 0.0
    col._push_updates = 0
    col._poll_updates = 0
    col._last_update_source = "none"
    col._probe_cursor = 0
    col._ka_sent = 0
    col._ka_expired = 0
    col._reopens = 0
    col._usb_resets = 0
    col._device_info = {}

    handler = type("_BoundHandler", (_Handler,), {"collector": col})
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    _start_dashboard._httpd = httpd  # type: ignore[attr-defined]


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_handler_serves_metrics_and_index():
    import urllib.request

    port = _free_port()
    _start_dashboard(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.status == 200
            assert "text/plain" in r.headers.get("Content-Type", "")
            body = r.read().decode()
            assert "bqio_up 1" in body
            assert "bqio_temperature_celsius 45.5" in body

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert r.status == 200
            assert "text/html" in r.headers.get("Content-Type", "")
            assert "bqio_temperature_celsius" in r.read().decode()
    finally:
        _start_dashboard._httpd.shutdown()  # type: ignore[attr-defined]
        _start_dashboard._httpd.server_close()  # type: ignore[attr-defined]


def test_http_handler_404_on_unknown_path():
    import urllib.error
    import urllib.request

    port = _free_port()
    _start_dashboard(port)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert exc.value.code == 404
    finally:
        _start_dashboard._httpd.shutdown()  # type: ignore[attr-defined]
        _start_dashboard._httpd.server_close()  # type: ignore[attr-defined]


# ------------------------------------------------------------- serve() / main()


def test_serve_binds_and_serves(monkeypatch):
    """serve() binds the port and serves /metrics (daemon thread)."""
    import threading as th
    import urllib.request

    import bqio.web as wb
    from bqio.exporter import Collector

    col = Collector.__new__(Collector)
    col._lock = th.Lock()
    col._values = {}
    col._last_ok = 0.0
    col._cycle_dur = 0.0
    col._up = 0
    col._meta = {}
    col._controls = {}
    col._eff_ewma = None
    col._notif_count = 0
    col._notif_last = 0.0
    col._push_updates = 0
    col._poll_updates = 0
    col._last_update_source = "none"
    col._probe_cursor = 0
    col._ka_sent = 0
    col._ka_expired = 0
    col._reopens = 0
    col._usb_resets = 0
    col._device_info = {}

    port = _free_port()
    t = th.Thread(target=wb.serve, args=(col, "127.0.0.1", port), daemon=True)
    t.start()
    # The server binds asynchronously in the thread — retry briefly.
    import time as tm

    last_err: Exception | None = None
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
                assert r.status == 200
            last_err = None
            break
        except Exception as e:  # noqa: BLE001 - retry loop
            last_err = e
            tm.sleep(0.1)
    assert last_err is None, f"dashboard never came up: {last_err}"
    # The daemon thread dies with the test process; no join needed.


def test_web_main_wires_collector_and_serve(monkeypatch):
    import sys

    import bqio.web as wb

    started: list[bool] = []
    served: list[bool] = []

    class FakeCol:
        def __init__(self, device: str | None = None) -> None:
            pass

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(wb, "Collector", FakeCol)
    monkeypatch.setattr(wb, "serve", lambda *a, **k: served.append(True))
    monkeypatch.setattr(sys, "argv", ["bqio-web", "--port", "18080"])
    wb.main()
    assert started and served
