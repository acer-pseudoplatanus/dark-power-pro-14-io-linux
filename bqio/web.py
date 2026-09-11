"""bqio.web — tiny stdlib-only web dashboard.

Serves two endpoints on one port:

* ``/metrics`` — the Prometheus text exposition (delegates to the exporter
  renderer, so it stays byte-identical to the standalone exporter).
* ``/``        — a dependency-free HTML dashboard that polls ``/metrics``
  client-side and renders the live values.

It reuses the exporter's :class:`~bqio.exporter.Collector` (persistent
session, cached samples), so the dashboard never adds USB traffic beyond
the native 2 Hz sampling. Everything is the Python standard library — no
framework, no templating engine, no JavaScript dependencies.
"""

from __future__ import annotations

import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .exporter import DEFAULT_PORT, Collector

log = logging.getLogger("bqio.web")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>bqio — PSU telemetry</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;
      background:#0f1115;color:#e6e6e6;padding:24px}}
 h1{{font-size:1.3rem;font-weight:600;margin:0 0 4px}}
 .sub{{color:#8a8f98;font-size:.85rem;margin-bottom:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
 .card{{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:14px 16px}}
 .k{{font-size:.78rem;color:#8a8f98;text-transform:uppercase;letter-spacing:.04em}}
 .v{{font-size:1.5rem;font-weight:600;margin-top:6px;font-variant-numeric:tabular-nums}}
 .u{{color:#6b7280;font-size:.85rem;margin-left:4px}}
 .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;vertical-align:middle}}
 .ok{{background:#3fb950}}.bad{{background:#f85149}}
 footer{{margin-top:24px;color:#5b6270;font-size:.75rem}}
</style></head><body>
<h1><span class="dot {status_class}" id="dot"></span>bqio — PSU telemetry</h1>
<div class="sub">be quiet! IO-series · polled every 2 s · v{version}</div>
<div class="grid" id="grid"></div>
<footer>Served by <code>bqio.web</code> · <a href="/metrics" style="color:#58a6ff">/metrics</a></footer>
<script>
const METRICS = {metrics_json};
async function refresh() {{
  const r = await fetch("/metrics"); const t = await r.text();
  const got = {{}};
  for (const line of t.split("\\n")) {{
    if (!line || line.startsWith("#")) continue;
    const sp = line.indexOf(" ");
    const k = line.slice(0, sp), v = parseFloat(line.slice(sp + 1));
    if (!isNaN(v)) got[k] = v;
  }}
  const grid = document.getElementById("grid"); grid.innerHTML = "";
  for (const m of METRICS) {{
    const raw = got[m.key];
    const el = document.createElement("div"); el.className = "card";
    const val = raw === undefined ? "—" : (m.decimals ? raw.toFixed(m.decimals) : Math.round(raw));
    el.innerHTML = `<div class="k">${{m.label}}</div>
      <div class="v">${{val}}<span class="u">${{m.unit}}</span></div>`;
    grid.appendChild(el);
  }}
  const up = got["bqio_up"] === 1;
  document.getElementById("dot").className = "dot " + (up ? "ok" : "bad");
}}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def _metric_defs() -> list[dict[str, object]]:
    """Human-readable definitions derived from the registry (single source)."""
    from .registry import CONTROLS, SENSORS

    defs: list[dict[str, object]] = []
    for idx in sorted(SENSORS):
        sd = SENSORS[idx]
        if not sd.active:
            continue
        decimals = 0 if sd.unit in ("seconds", "rpm", "") else 2
        defs.append(
            {"key": f"bqio_{sd.metric}", "label": sd.help, "unit": sd.unit, "decimals": decimals}
        )
    for ci in sorted(CONTROLS):
        cd = CONTROLS[ci]
        defs.append({"key": f"bqio_ctrl_{cd.metric}", "label": cd.help, "unit": "", "decimals": 0})
    defs.append(
        {"key": "bqio_dc_output_power_w", "label": "DC output power", "unit": "W", "decimals": 1}
    )
    defs.append(
        {"key": "bqio_efficiency_percent", "label": "Efficiency", "unit": "%", "decimals": 1}
    )
    return defs


class _Handler(BaseHTTPRequestHandler):
    collector: Collector

    def log_message(self, format: str, *args: object) -> None:  # silence stderr
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/metrics"):
            body = self.collector.render().encode("utf-8")
            ctype = "text/plain; version=0.04; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            import json

            html = _PAGE.format(
                version=__version__, status_class="ok", metrics_json=json.dumps(_metric_defs())
            )
            body = html.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(collector: Collector, host: str, port: int) -> None:
    handler = type("_BoundHandler", (_Handler,), {"collector": collector})
    httpd = ThreadingHTTPServer((host, port), handler)
    log.info(
        "bqio web dashboard v%s on http://%s:%d/ (metrics at /metrics)", __version__, host, port
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="bqio web dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--device", default=None, help="/dev/hidraw node (default: auto-detect by VID:PID)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    collector = Collector(device=args.device)
    collector.start()
    serve(collector, args.host, args.port)


if __name__ == "__main__":
    main()
