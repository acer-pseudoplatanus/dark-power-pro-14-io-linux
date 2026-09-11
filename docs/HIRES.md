# ⚡ High-Resolution Telemetry (HiRes)

> **Kurzfassung:** Die Firmware der Dark Power Pro 14 IO sampelt intern mit
> **500 ms** — 20–100 ms sind physikalisch nicht erreichbar. bqio nutzt
> deshalb einen **Hybrid-Sampler**: Polling-Backbone (500 ms) +
> Push-Notification-Consumer mit exakten Empfangszeitstempeln.

## Empirische Befunde (am Gerät verifiziert, 2026-09-09)

| Messung | Ergebnis | Bedeutung |
|---------|----------|-----------|
| Roundtrip-Latenz (GetSensorValue) | **3 ms** (min/med/p95) | Transport schafft ~300 Hz |
| Distinct-Value-Test (343 Hz Polling, 10 s) | **1 eindeutiger Wert** | Firmware sampelt intern bei 500 ms |
| `SetSensorConfig` (cmd=5) | **status=3 (INVALID_COMMAND_ID)** | Nicht implementiert — Intervall nicht änderbar |
| Push-Notifications | **aktiv, ~500 ms Cadence** | `SensorValueChanged` (feat=48, cmd=2, req=0) |
| Notification-Payload | Multi-Sensor `(idx, value)*` Pairs | Ein Packet trägt mehrere Sensoren |

**Fazit:** Das Gerät pusht seine Werte aktiv — wir müssen nur lauschen.
Der Nutzen gegenüber reinem Polling:

1. **Exakte Zeitstempel** — Push-Werte tragen den Moment des Empfangs,
   nicht den des nächsten Poll-Zykls (Jitter ±250 ms → ±3 ms).
2. **Weniger Bus-Traffic** — Notifications kommen kostenlos zwischen
   den Polls; der Poll-Backbone bleibt als Sicherheitsnetz.
3. **Event-getrieben** — `bqio notif` zeigt die native Cadence live.

## Architektur

```
PSU ──USB HID──▶ QLinkClient
                   ├── request()  → Poll-Response (Backbone, 500 ms)
                   └── RX-Pfad erkennt request_id==0
                          └──▶ on_notification Callback
                                  └──▶ Collector._on_notification()
                                          └──▶ Cache-Update mit exaktem ts
```

- `protocol.is_notification(pkt)` — `request_id == 0 && sequence == 0`
  (Vendor-Bundle-Kriterium).
- `protocol.parse_sensor_changed(payload, vtypes)` — dekodiert
  `(sensor_idx, raw_value)`-Pairs; unbekannt/truncated → sauber stoppen.
- `client.QLinkClient(on_notification=cb)` — Callback wird im RX-Pfad
  aufgerufen (bei Poll-Responses UND in `passive_read()`). Exceptions
  im Callback werden containment-logged, brechen nie den Request-Loop.
- `client.passive_read(timeout)` — rein passives Lauschen ohne TX.
- `exporter.Collector._on_notification()` — skaliert mit gecachten
  Exponenten, Sanity-Gating, Counting (`_push_updates`).

## Neue Metriken

| Metrik | Typ | Bedeutung |
|--------|-----|-----------|
| `bqio_notifications_received` | counter | konsumierte Push-Notifications |
| `bqio_poll_updates` | counter | Sensorwerte via Polling |
| `bqio_push_updates` | counter | Sensorwerte via Push |
| `bqio_last_update_source` | gauge | 1=poll, 2=push, 0=none |

Gesundes Verhältnis: `push_updates ≈ poll_updates` (jeder Poll-Zyklus
triggert ~eine Notification). `last_update_source` pendelt zwischen 1/2.

## CLI

```bash
# Native Push-Cadence beobachten (10 s, ms-Präzision):
python3 -m bqio notif -d 10

# Nach N Notifications stoppen:
python3 -m bqio notif -d 60 -n 100
```

## Grafana-Visualisierung

Siehe [`dashboards/bqio-hires.json`](../dashboards/bqio-hires.json) —
Panel-Layout:

1. **Leistung (W)** — Time-Series, `min/max` Fill, 500 ms Scraping
2. **Spannungen (V)** — alle Rails, eine Reihe pro Rail
3. **Temperaturen (°C)** — mit Threshold-Annotations
4. **Datenfluss** — `increase(bqio_push_updates[5m])` vs
   `increase(bqio_poll_updates[5m])` (Stacked-Bar) → zeigt Push-Anteil
5. **Freshness** — `bqio_data_age_seconds` (Alert bei > 5 s)

**Scrape-Intervall:** 500 ms (Prometheus `scrape_interval` pro Target
via `additional_scrape_configs` oder recording rules). Bei 500 ms
Scraping entspricht jede Sample genau einem Firmware-Sample —
die Push-Zeitstempel glätten den Jitter zusätzlich.

## Grenzen & Risiken

- **500 ms ist die Firmware-Grenze.** Kein Firmware-Update bekannt, das
  sie hebt; `SetSensorConfig` wird abgewiesen.
- **Wedge-Risiko unverändert:** Notifications ändern nichts an der
  Session-Logik. Recovery bleibt AC-Power-Cut (siehe
  `psu-mcu-wedge-recovery`).
- **Payload-Format ist RE-abgeleitet** (Vendor-Bundle `onSensorValueChanged`),
  nicht dokumentiert. `parse_sensor_changed` ist defensiv gebaut
  (unknown idx → stop, kein Crash).
