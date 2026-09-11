# 🤝 Contributing

Thanks for helping improve **bqio**! This guide covers the development setup, the typical workflow for protocol work, and what we expect from a clean PR.

## 🛠️ Development Setup

```bash
git clone https://github.com/acer-pseudoplatanus/bqio.git
cd bqio

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify your environment talks to the PSU:

```bash
python -m bqio status
pytest
```

Both should succeed before you start changing anything.

## 🔒 Git Identity (Privacy Requirement)

This repository is public. Internal hostnames, LAN addresses, and local
mail domains must **never** appear in commit metadata or file contents.

Set a neutral identity before committing:

```bash
git config user.name "your-neutral-handle"
git config user.email "you@example.com"   # or your GitHub noreply address
```

Never use machine-local identities — combining your username with the
machine hostname or a local domain as the mail address exposes internal
naming conventions. CI enforces this: the `privacy` job scans the full
history and fails the workflow on any hit.

## 🔄 Typical Workflow (New Sensor / Control)

The QLink protocol is reverse-engineered, so most contributions follow this loop:

1. **Capture** — record raw HID traffic from the official vendor tool while exercising the feature you want to add
2. **Decode** — identify the command ID, payload layout, scaling factors and CRC
3. **Implement** — add the frame codec in `bqio/protocol.py`, the accessor in `bqio/client.py`, and the CLI subcommand in `bqio/cli.py`
4. **Expose** — if it's a metric, add it to `bqio/exporter.py` **and** to the sensor table in `README.md` (both must stay in sync)
5. **Verify** — cross-check values against the vendor tool under identical load

Captured frames belong in `tests/fixtures/` so regressions are catchable offline.

## ✅ PR Checklist

- [ ] `pytest` passes locally
- [ ] No hardcoded device paths or bus numbers (always discover by VID/PID)
- [ ] Sensor table in `README.md` updated if a metric changed
- [ ] Exporter metric names follow the `bqio_*` convention
- [ ] No internal hostnames, LAN addresses, or credentials in the diff
- [ ] Commit messages in imperative mood, scoped: `cli: add fan-speed subcommand`

## 📏 Style

- Python 3.10+, PEP 8 (black-compatible)
- Type hints on public APIs
- Docstrings for every public function
- Keep the CLI output aligned and human-readable (see the `status` box)

## 🐛 Reporting Bugs

Use the **[bug report template](.github/ISSUE_TEMPLATE/bug_report.md)**. A good report includes:

- PSU firmware revision (if known)
- `python -m bqio status` output
- The failing command and its traceback
- USB topology (`lsusb`)

## 💬 Questions

Open a discussion or an issue with the `question` label — happy to help.
