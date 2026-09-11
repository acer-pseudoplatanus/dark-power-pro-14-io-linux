# Security Policy

## Supported Versions

| Version | Supported                      |
| ------- | ------------------------------ |
| 1.0.x   | Yes                            |

Only the latest stable release receives security fixes.

## Reporting a Vulnerability

**Please do NOT open a public issue for security vulnerabilities.**

Report security issues privately by opening a
[security advisory](../../security/advisories/new) on this repository
(GitHub's private vulnerability reporting), or contact the maintainer
directly.

Include:

- Affected component (codec, client, exporter, web dashboard)
- Steps to reproduce or a minimal PoC
- Expected vs. observed behavior
- Any workaround you found

We aim to acknowledge reports within 48 hours and ship a fix in the next
release. Credit will be given upon publication unless you prefer anonymity.

## Threat Model Notes

Two properties of this project are worth knowing when reviewing code:

1. **The control plane can power-cycle your machine.** The QLink protocol
   exposes actuator controls (fan mode, rail mode). They are **opt-in**:
   nothing in the default installation writes to the device, and the
   exporter/dashboard are strictly read-only. If you extend the project,
   keep it that way — or gate writes behind an explicit flag.
2. **Malformed packets can wedge the device MCU.** The codec raises
   `MalformedPacketError` instead of emitting invalid frames precisely
   because a corrupted frame can lock the PSU's controller until an AC
   power cut. Never relax the validation in `bqio.protocol.build_packet`.
