# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for a security vulnerability.

Email **contact@xaloqi.com** with:

- A description of the issue and its potential impact.
- Steps to reproduce, or a proof-of-concept if you have one.
- The version of `xaloqi-tester` affected (`python3 -c "import xaloqi;
  print(xaloqi.__version__)"`).

We're a small team and this isn't a 24/7 operation, but we take security
reports seriously and will acknowledge and start working a confirmed issue
promptly, faster for anything actively exploitable. If you haven't heard
back within a week, please follow up — email can get lost.

## Scope

This policy covers `xaloqi-tester` (this repository): the UDS client,
ISO-TP engine, SecurityAccess (AES-CMAC) key derivation, the in-process
VirtualBus transport, the simulated ECU, and the campaign runner.

A few things worth knowing so you can calibrate severity:

- The **simulator's SecurityAccess key** is a fixed placeholder for
  demonstration and testing — it is not, and is not intended to be, secret.
  Reporting "the simulator's key is guessable" is expected behaviour, not a
  vulnerability.
- This library talks to ECUs over CAN/ISO-TP; it does not itself open
  network listeners or accept untrusted network input in the free tier.
- Real hardware transports, network-facing protocols (DoIP, SOME/IP, SOVD),
  and the license/activation mechanism are part of the commercial
  **Xaloqi TestLab Pro**, developed in a separate private repository.
  Vulnerabilities there should also be reported to contact@xaloqi.com —
  we'll route it internally — but please expect a longer loop since that
  code isn't in this public repository.

## Supported versions

Only the latest released minor version of `xaloqi-tester` receives security
fixes. Please upgrade before reporting if you're on an older release.
