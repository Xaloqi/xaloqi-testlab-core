# xaloqi.tester.conformance

A UDS transport/service conformance vector corpus and a byte-exact judge.
Published to establish a neutral measuring stick for the OpenSOVD/CDA/SoDeV
ecosystem — not a product, and not a claim about any particular stack's
quality beyond exactly what each vector below asserts.

## What this is, in one sentence

Three suites of byte-exact vectors — ISO 15765-2 (ISO-TP), ISO 14229-1 (UDS
SID framing), ISO 13400-2 (DoIP) — each independently sourced from a
real-world open-source UDS toolchain's own test fixtures, plus a runner
that judges `==` on bytes. You bring the transport.

## Why "you bring the transport" is the design, not a limitation

The free, Apache-2.0 core of this package ships `CanBus` (raw CAN frame
send/recv) and `UdsTester` (complete-PDU request/response, over any
`CanBus` or message-oriented transport) — nothing in `xaloqi.tester.conformance`
depends on `xaloqi-tester-pro`'s real-hardware transports (SocketCAN, DoIP,
PCAN/Kvaser). Implement `CanBus`'s two methods against whatever you already
use to talk to your own stack — `python-can`, a raw socket, hardware you
already have working — and every suite here runs against it.
`xaloqi/tester/conformance/python_can_bus.py` is a real, runnable one,
shipped in the package and used by the CLI unless you pass
`--bindings-dir`, using `python-can` (LGPL-3.0-only, an optional extra
used as an ordinary dependency — not modified or redistributed) as a convenient
already-independent transport. Not to be confused with
`python-can-isotp`, a separate MIT-licensed project that uses `python-can`
as its own dependency and is the actual project the ISO-TP vectors below
were ported from.

This was a real decision, not a default: an earlier draft of this corpus
assumed it could lean on this package's own real transports, which turned
out not to exist in the free tier (they're `xaloqi-tester-pro`'s headline
differentiator). Giving a third party no choice but to either pay for a
transport or use ours would make this "a test suite with marketing
attached" — the outcome everyone involved explicitly wanted to avoid. The
fix: publish the vectors and a judge, require nothing but a transport
contract any serious implementer already satisfies by having a stack at
all.

## What a pass asserts, and what it refuses to claim

**A pass asserts, per vector:** this exact request, over this transport,
produced this exact response (or this exact absence/timing of one) — cited
to the independent upstream project each vector's `upstream` field names.

**This is not:**

- **An ISO conformance certification.** No suite here is a complete
  implementation of its ISO standard. Each suite's own `scope_note` and
  `not_ported` list (in `corpus/*.json`) name specific upstream behaviors
  deliberately not ported, and why.
- **A check on DID, DTC, or routine *content*.** Several vectors assert
  only the request/response *envelope* (echoed SID, sub-function, DID,
  length) — never what's inside. Each such vector's `description` says so.
- **A check on the SOVD layer.** Despite this corpus's origin as "the
  SOVD→UDS conformance harness," nothing here touches SOVD's HTTP API —
  that's a different claim, exercised instead by a real interop demo
  against a real SOVD gateway (see Xaloqi/EDS's
  `scripts/demo_can_sovd_cda_interop.sh`). Renamed for exactly this reason:
  a name promising a SOVD-layer check this corpus doesn't do would be
  worse than an unglamorous one that's accurate.
- **A safety case.** A byte-exact pass on these vectors is evidence of
  protocol mechanics, not a certification that a target is safe to deploy.

## requires_fixture — read this before a vector "fails" against your target

Several vectors assert against a *specific* test DID (`0x0001`), routine
(`0x0012`), DTC (`0x123456`), flow-control configuration
(`block_size`/`stmin_ms`), or DoIP logical addresses (entity `0x0001`,
tester `0x0E00`) that the original EDS C conformance test
registers as its own throwaway fixture — not a universal ISO requirement,
and not present in most real targets' production configuration. Each such
vector is tagged `"requires_fixture": true` with a `fixture_note` saying
exactly what's needed. Running one against a target that doesn't have that
fixture will correctly report a mismatch (often a legitimate
`requestOutOfRange` or `invalidKey`) — that is the target behaving
correctly, not a defect, and not what the vector is measuring in that
context.

The CLI skips them unless you pass `--include-fixture-dependent` (every
suite, DoIP included); the library's `run_*_suite()` functions run them
unless you pass `include_fixture_dependent=False`.

`examples/conformance_bindings/reference_uds_stub.py` (in a clone of this repository) registers exactly
these fixtures in ~250 lines of plain Python sharing no code with EDS,
specifically so every vector — including the five `requires_fixture` ones
— has somewhere real to run and pass. `tests/test_conformance.py`
runs the full 15-vector `uds_sid` suite against it.

## Suites

| Suite | Primitive needed | Vectors | requires_fixture |
|---|---|---|---|
| `isotp` | `CanBus.send()`/`recv()` (raw frames) | 7 | 6 |
| `uds_sid` | `UdsTester.raw_exchange()` (complete PDUs, any transport) | 15 | 5 |
| `doip` | `DoipRawProbe` (this package's one new primitive — raw TCP bytes) | 5 | 5 |

## The one new transport primitive: `DoipRawProbe`

DoIP vectors test the DoIP TCP header's own framing/addressing (routing
activation, diagnostic message ACK, alive check) — one level below a
complete UDS PDU, which is as far down as `UdsTester`'s message-oriented
path goes. No free transport here exposes that. `runner.DoipRawProbe` is
deliberately narrow: open a socket, write exactly these bytes, read up to
a timeout. No routing-activation lifecycle, no reconnection, no SOVD — a
conformance *probe*, not a transport for test campaigns. Use
`xaloqi-tester-pro`'s `DoipBus` for that; this package's free tier is not
trying to be a smaller version of it.

## Running it

```bash
# The CAN suites need python-can, an optional extra (the DoIP suite does not):
pip install "xaloqi-tester[can]"        # from PyPI
pip install -e ".[can]"                  # or from a clone of this repository

# Against your own stack, however you reach it:
python3 -m xaloqi.tester.conformance isotp --interface vcan0 --can-id 0x7E0
python3 -m xaloqi.tester.conformance uds-sid --interface vcan0 --rx-id 0x7E8 --tx-id 0x7E0
python3 -m xaloqi.tester.conformance doip --host 192.0.2.10 --port 13400

# Against real, unmodified EDS over a real vxcan pair (no sudo, Docker only
# -- reuses Xaloqi/EDS's own proven vxcan container pattern):
bash scripts/dogfood_against_eds.sh /path/to/EDS/checkout
```

The CAN suites use the python-can binding shipped in the package. To use
your own transport, write `can_frame_python_can.py` defining `PythonCanBus`
(a `CanBus`: `send()`, `recv()`, `close()`, constructed with `channel=`) in a
directory of your own and add `--bindings-dir <that directory>`;
`examples/conformance_bindings/can_frame_python_can.py` shows the contract.

## Verifying the corpus itself

`scripts/verify_conformance_corpus_bytes.py` mechanically cross-checks
every byte literal in `corpus/*.json` against the EDS C source it was
transcribed from, catching the transcription errors a mechanical check
can catch (byte sequences that exist as C literals in both places). It
cannot, and does not claim to, validate the editorial decision of *which*
vectors were ported, reimagined, or disclosed as not-portable — that's
reviewed by reading each vector's `description`, same as any other code.
