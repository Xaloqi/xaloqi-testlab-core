# Xaloqi TestLab Core

## Run UDS tests without an ECU.

[![CI](https://github.com/Xaloqi/xaloqi-testlab-core/actions/workflows/ci.yml/badge.svg)](https://github.com/Xaloqi/xaloqi-testlab-core/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/xaloqi-tester.svg?cacheSeconds=3600&v=1.5.2)](https://pypi.org/project/xaloqi-tester/)

**Free, open-source Python UDS (ISO 14229) client, ECU simulator, and
campaign runner.** No hardware. No CAN interface. No commercial diagnostic
tool required.

```bash
pipx run --spec xaloqi-tester xaloqi-sim --demo
```

That's it. The command starts an in-process simulated ECU and runs a real
UDS conversation against it: session control, VIN read, AES-CMAC
SecurityAccess unlock, a security-gated DID read, DTC read — the same
protocol traffic a real bench would see.

Or install it:

```bash
pip install xaloqi-tester
xaloqi-sim --demo
```

> **Build diagnostics?** → [Xaloqi EDS](https://github.com/Xaloqi/EDS) —
> generate production UDS implementations for Zephyr and FreeRTOS.
> **Test diagnostics?** You're in the right place.

![Xaloqi TestLab Core demo — pipx run --spec xaloqi-tester xaloqi-sim --demo, showing a real UDS conversation: VIN read, extended session, AES-CMAC SecurityAccess unlock, a security-gated DID read, and a DTC read, ending "All exchanges OK."](docs/assets/testlab-demo.gif)

*Recorded from the actual published package — not a mockup. Command and
output are real; only the reveal pacing of the (near-instantaneous) result
lines was adjusted for legibility.*

---

## Why TestLab?

Exercising UDS normally means an ECU on the bench, a CAN interface, a
diagnostic tool, and hand-maintained test scripts. TestLab Core gives you a
test environment before any of that exists:

```text
your test campaign (YAML)
          │
          ▼
   ┌────────────────┐
   │  TestLab Core   │
   │  UDS client     │
   │  campaign runner│
   │  ECU simulator  │
   └────────┬────────┘
            │
            ▼
       virtual ECU
      — no hardware —
```

The same campaign YAML runs against the simulator locally and in CI, then
against real hardware unmodified once you add
[Xaloqi TestLab Pro](https://xaloqi.com).

---

## Run a full campaign against it

```bash
testlab-run --config testlab_config.yaml \
            --campaign campaigns/standalone_validation.yaml \
            --job basic_validation --virtual --json reports/run.json
testlab analyze --results reports/run.json
```

`campaigns/*.yaml` define test jobs — session control, SecurityAccess,
DID/memory read-write, DTC handling, firmware transfer, expected NRCs,
variable capture/interpolation — as data, not code. `--virtual` runs them
against the built-in simulator.

## What's in core (Apache-2.0)

- **UDS client** (`xaloqi.tester.UdsTester`) — async and sync, all major UDS
  services including 0x27 SecurityAccess (AES-CMAC) and the
  0x34/0x35/0x36/0x37 firmware up/download sequence. ISO-TP multi-frame
  sends honour the ECU's real Flow Control: BlockSize, STmin (both the
  millisecond and the 100–900us encodings), WAIT with a bounded WFTmax,
  and OVERFLOW.
- **VirtualBus** — the in-process transport and the **simulated ECU**
  (`xaloqi.sim`, also the `xaloqi-sim` command).
- **Campaign runner** (`testlab-run`) — 20 UDS actions, `expect_nrc`,
  variable capture/interpolation, JSON output (schema shared with the
  Xaloqi EDS diagnostics stack, so the same result format works with
  either).
- **`testlab analyze`** — terminal summary of a run's JSON results.

Core is licensed under **Apache-2.0** — use it for learning UDS, local
diagnostic development, virtual ECU testing, campaign development,
automated CI validation, and building your own integrations.

---

## How this compares to udsoncan, python-can and can-isotp

If you are doing UDS in Python, you have almost certainly found the
established stack: **[python-can](https://github.com/hardbyte/python-can)**
(the CAN bus layer) + **[can-isotp](https://github.com/pylessard/python-can-isotp)**
(ISO 15765-2 transport) + **[udsoncan](https://github.com/pylessard/python-udsoncan)**
(the ISO 14229 client). They are good, mature libraries and this project
does not try to replace them.

They answer a different question. That stack is a **client** — it speaks
UDS *to an ECU that already exists*. TestLab Core is a **test harness** —
a campaign runner plus an ECU that answers, so you can write and run tests
before any hardware does.

| | udsoncan + python-can + can-isotp | TestLab Core |
|---|---|---|
| **What it is** | UDS client library stack | Campaign runner + simulated ECU |
| **Something that answers you** | Needs a real ECU. `FakeConnection` replays a 2-entry static dict — enough to smoke-test your own plumbing, not an ECU | Stateful simulator: sessions, SecurityAccess seed/key (AES-CMAC), DTCs, NRCs |
| **Real CAN / DoIP hardware** | **Yes, free** — socketcan, J2534, and every python-can interface | **Not in core.** Core is virtual-transport only; real transports are [Pro](https://xaloqi.com) |
| **Raw UDS service coverage** | **Broader** — 27 services, 91 client methods, incl. the full ReadDTCInformation subfunction set, 0x29 Authentication, DynamicallyDefineDataIdentifier | Focused on the ~20 actions a validation campaign uses |
| **How you express a test** | Write Python | Declarative YAML campaign |
| **Pass/fail + timing report** | Build it yourself | Built in (`testlab-run`, `testlab analyze`, JSON out) |
| **Time to first result** | A CAN interface and an ECU — or write your own responder | `pip install xaloqi-tester && xaloqi-sim --demo` |
| **License** | MIT (udsoncan, can-isotp), **LGPL-3.0-only** (python-can) | Apache-2.0 |

### Use udsoncan instead when

- You have hardware on the bench **now** and want to drive it for free —
  core's real transports are a paid tier, theirs are not.
- You need UDS services outside a validation campaign's usual set —
  `ResponseOnEvent`, `RequestFileTransfer`, the deeper
  `ReadDTCInformation` subfunctions, `LinkControl`.
- You are building an application that happens to speak UDS, rather than
  running repeatable tests against an ECU.

### Use TestLab Core instead when

- The ECU does not exist yet, or the bench is occupied, and you still want
  to write and run the tests.
- You want the tests to run in CI on every commit, with no hardware and no
  self-hosted runner.
- You want tests as reviewable YAML with a pass/fail report, rather than a
  growing pile of scripts.

They also compose. Nothing stops you developing campaigns against the
simulator here and driving real hardware with udsoncan — the JSON result
schema is documented, and the same campaign YAML runs unmodified on real
transports if you later move to Pro.

> Comparison verified 2026-09-04 against udsoncan 1.26.1, python-can 4.6.1,
> can-isotp 2.0.7 and xaloqi-tester 1.5.2, by installing each from PyPI and
> inspecting the actual APIs — not from documentation. If something here is
> out of date or wrong,
> [open an issue](https://github.com/Xaloqi/xaloqi-testlab-core/issues) and
> it will be corrected.

---

## Use it in CI

A virtual ECU is useful in CI precisely because the test doesn't depend on
a bench being available. A minimal GitHub Actions job:

```yaml
name: UDS tests
on: [push, pull_request]
jobs:
  uds:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install xaloqi-tester
      - run: |
          mkdir -p reports
          testlab-run --config testlab_config.yaml \
            --campaign campaigns/standalone_validation.yaml \
            --job basic_validation --virtual --json reports/run.json
      - run: testlab analyze --results reports/run.json
```

Repeatable diagnostic tests on every push and pull request, no ECU hardware
allocated to the CI runner.

---

## Learn by doing

| Recipe | What you'll prove | Hardware |
|---|---|---|
| [SecurityAccess](examples/security_access/) | Protected resources stay locked until UDS `0x27` unlock | None |
| [GitHub Actions](examples/github_actions/) | Run UDS regression tests on every PR | None |
| [Firmware download](examples/firmware_download/) | Exercise the `0x34` → `0x36` → `0x37` programming flow | None |
| [DoIP](examples/doip/) | Move the same UDS testing model to Automotive Ethernet | DoIP endpoint (Pro) for real-network testing |

**Start here:** `pipx run --spec xaloqi-tester xaloqi-sim --demo`

Every recipe above is run and verified against the real published package
before being committed — not just described.

## Try the examples

The bundled config and campaigns are meant to be changed, not just run:

```text
examples/diagnostics_config.yaml
campaigns/standalone_validation.yaml
campaigns/basic_validation.yaml
testlab_config.yaml
```

Run the bundled campaign first:

```bash
testlab-run --config testlab_config.yaml \
            --campaign campaigns/standalone_validation.yaml \
            --job basic_validation --virtual
```

Then fork this repository and adapt it to your own ECU: change a DID or
expected value, add an expected NRC, add another diagnostic step, run it
locally, then put it in CI. If you build a useful campaign for a common ECU
workflow, contributions are welcome.

---

## Cross-implementation tested, not just self-tested

[Xaloqi Compatibility Tests](https://github.com/Xaloqi/xaloqi-compatibility-tests)
runs the same UDS campaign against every combination of transport (CAN,
DoIP) and RTOS (Zephyr, FreeRTOS) `Xaloqi EDS` ships examples for, using
`xaloqi-tester`'s Python client against EDS's C runtime — a genuine
cross-language, cross-implementation check, not two halves of one
codebase validating each other. **Honestly:** the virtual-ECU check
there is green and reproducible by anyone with no license; the
real-transport matrix is still being wired up (tracked openly at
[xaloqi-compatibility-tests#2](https://github.com/Xaloqi/xaloqi-compatibility-tests/issues/2),
including [EDS#230](https://github.com/Xaloqi/EDS/issues/230) — EDS's own
`DoIP Integration` CI job currently skips this exact check rather than
proving it).

## Build → Test with Xaloqi

TestLab is the **test** side of the Xaloqi workflow. [Xaloqi EDS](https://github.com/Xaloqi/EDS)
is the **build** side — generate production ISO 14229 diagnostics for
Zephyr and FreeRTOS from YAML.

```text
diagnostics_config.yaml
          │
          ▼
   ┌──────────────┐
   │  Xaloqi EDS  │  BUILD
   └──────┬───────┘
          │ generated ECU diagnostics
          ▼
   ┌──────────────┐
   │   TestLab    │  TEST
   └──────────────┘
          │
          ▼
  virtual ECU / CI / real hardware
```

Need to implement ISO 14229 diagnostics on Zephyr or FreeRTOS? →
[Xaloqi EDS](https://github.com/Xaloqi/EDS). Already have an ECU and need
to test it? Stay here.

---

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

## Who is this for?

Automotive ECUs, software-defined vehicles, battery management systems,
motor controllers, gateways, embedded controllers, diagnostic bootloaders,
Zephyr or FreeRTOS projects, CI pipelines for embedded software — and
learning ISO 14229 without buying hardware first.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). If you've built something with
TestLab, opening a discussion or linking your project helps us understand
which workflows deserve more attention. Found a security issue? See
[SECURITY.md](SECURITY.md) — please don't open a public issue for it.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

---

If TestLab saved you from setting up a bench just to exercise a UDS flow,
consider starring the repository. More usefully: fork it, change a
campaign, and make it test your ECU.

**Build diagnostics:** [Xaloqi EDS](https://github.com/Xaloqi/EDS) ·
**Test diagnostics:** Xaloqi TestLab Core ·
**Commercial tooling:** [xaloqi.com](https://xaloqi.com)
