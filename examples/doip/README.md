# Test UDS over DoIP

TestLab's UDS layer is transport-independent. The same diagnostic
concepts you exercise against the built-in virtual ECU apply over
Diagnostics over Internet Protocol (DoIP) once a DoIP transport is
available.

**This one is different from the other recipes: real DoIP transport is
part of [TestLab Pro](https://xaloqi.com), not Core.** This page explains
the boundary rather than pretending it's another free `--virtual` recipe.

## Why DoIP?

As ECU diagnostics move from CAN toward Automotive Ethernet, the
application-level UDS workflow stays familiar:

```text
UDS
 │
 ├── CAN / ISO-TP
 │
 └── DoIP / Ethernet
```

The transport changes. Your diagnostic intent — sessions, SecurityAccess,
DIDs, DTCs, firmware transfer — should not have to.

## Cross-implementation tested

`xaloqi-tester`'s `DoipBus` (this package, Python) is run in CI against
the [Xaloqi EDS](https://github.com/Xaloqi/EDS) DoIP server (C, Zephyr
`native_sim`) on every EDS push:

```text
┌────────────────────────────┐
│ TestLab                    │
│ Python DoIP client         │
└─────────────┬──────────────┘
              │ DoIP / TCP, UDS
              ▼
┌────────────────────────────┐
│ Xaloqi EDS                 │
│ C, Zephyr native_sim        │
│ DoIP server                │
└────────────────────────────┘
```

That's deliberately not Python talking to Python — TestLab's client
implementation is exercised against EDS's server, implemented in C. See
[EDS's CI workflow](https://github.com/Xaloqi/EDS/blob/main/.github/workflows/ci.yml)
(job: `DoIP Integration (native_sim + DoipBus)`). This is TestLab's own
test suite validating interoperability, not a `--interface doip` flag
available in Core.

## Try the UDS side for free

If you're new to TestLab, start with the built-in virtual ECU:

```bash
pipx run --spec xaloqi-tester xaloqi-sim --demo
```

Then run a YAML campaign:

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign campaigns/standalone_validation.yaml \
  --job basic_validation \
  --virtual
```

The campaign expresses *what* diagnostic operations should happen. The
transport determines *how* those UDS messages reach the ECU.

## Test a real DoIP endpoint

Real-network DoIP transport is part of
[Xaloqi TestLab Pro](https://xaloqi.com) — installed as an additional
wheel alongside Core, discovered automatically through Python entry
points. Without Pro installed, the `doip` transport fails with one clear
message telling you what it needs.

The intended progression:

```text
virtual ECU
    ↓  develop campaign, run in CI
real DoIP ECU
    ↓  Pro transport, same YAML
```

You can build diagnostic intent before the target network or ECU is
ready, then reuse the same campaigns as integration matures.

## Want an open ECU to test against?

[Xaloqi EDS](https://github.com/Xaloqi/EDS) includes a C DoIP
implementation and Zephyr `native_sim` examples (`examples/basic_ecu_doip/`).
That makes the two Xaloqi repositories useful together: EDS builds the
ECU diagnostics, TestLab validates them.

## What to test over DoIP

Useful integration scenarios once you have a real DoIP endpoint:

- routing activation,
- diagnostic-session changes,
- DID reads and writes,
- SecurityAccess,
- DTC operations,
- larger diagnostic payloads,
- firmware transfer,
- reconnect/error behaviour.

Keep protocol-level campaigns transport-independent where possible — that
makes it straightforward to run the same diagnostic behaviour during
simulation, CI, CAN integration, and Ethernet integration.
