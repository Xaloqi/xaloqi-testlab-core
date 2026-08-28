# Xaloqi TestLab — core

[![CI](https://github.com/Xaloqi/xaloqi-testlab-core/actions/workflows/ci.yml/badge.svg)](https://github.com/Xaloqi/xaloqi-testlab-core/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/xaloqi-tester.svg?cacheSeconds=3600&v=1.5.1)](https://pypi.org/project/xaloqi-tester/)

Try a full UDS ECU with nothing but Python — no hardware, no CAN stack, no
config file:

```bash
pipx run --spec xaloqi-tester xaloqi-sim --demo
```

or, if you'd rather install it:

```bash
pip install xaloqi-tester
xaloqi-sim --demo
```

That runs a real UDS conversation against an in-process simulated ECU: VIN
read, session control, AES-CMAC SecurityAccess unlock, a security-gated DID
read, DTC read — the same protocol traffic a real bench would see.

## Run a full campaign against it

```bash
testlab-run --config testlab_config.yaml \
            --campaign campaigns/standalone_validation.yaml \
            --job basic_validation --virtual --json reports/run.json
testlab analyze --results reports/run.json
```

`campaigns/*.yaml` define test jobs — session control, SecurityAccess,
DID/memory read-write, DTC handling, firmware transfer — as data, not code.
`--virtual` runs them against the built-in simulator; the same YAML runs
unmodified against real hardware once you add
[Xaloqi TestLab Pro](https://xaloqi.com).

## What's in core (Apache-2.0)

- **UDS client** (`xaloqi.tester.UdsTester`) — async and sync, all major UDS
  services including 0x27 SecurityAccess (AES-CMAC) and the
  0x34/0x35/0x36/0x37 firmware up/download sequence.
- **VirtualBus** — the in-process transport and the **simulated ECU**
  (`xaloqi.sim`, also the `xaloqi-sim` command).
- **Campaign runner** (`testlab-run`) — 20 UDS actions, `expect_nrc`,
  variable capture/interpolation, JSON output (schema shared with the
  Xaloqi EDS diagnostics stack, so the same result format works with
  either).
- **`testlab analyze`** — terminal summary of a run's JSON results.

## What's in Pro

Real transports (SocketCAN, PCAN/Kvaser, DoIP, SOME/IP), the SOVD client,
multi-ECU workspace mode, RTOS comparison, HTML reports/trends/dashboard,
and AI-assisted failure analysis are part of
[Xaloqi TestLab Pro](https://xaloqi.com) — installed as an additional wheel
alongside this package. Core discovers Pro automatically through Python
entry points: install Pro and every `testlab` command and campaign action
above works unchanged, plus the paid ones. Without Pro installed, a paid
transport, action, or subcommand fails with one clear message telling you
what it needs.

## Installing from source

```bash
git clone https://github.com/Xaloqi/xaloqi-testlab-core
cd xaloqi-testlab-core
pip install -e ".[dev]"
XALOQI_LICENSE_SKIP=1 pytest tests/ -v
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Found a security issue? See
[SECURITY.md](SECURITY.md) — please don't open a public issue for it.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
