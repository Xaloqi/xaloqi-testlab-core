# Test DTCs without an ECU

Diagnostic Trouble Codes are read and cleared through UDS service `0x19`
(ReadDTCInformation) and `0x14` (ClearDiagnosticInformation). This example
exercises that workflow end to end against the built-in TestLab virtual
ECU — no hardware, no CAN interface.

## Install

```bash
pip install xaloqi-tester
```

## Run

From the root of this repository:

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign examples/dtcs/campaign.yaml \
  --job dtcs \
  --virtual
```

```text
  [01/07] read_dtc                           → OK           (0 ms)
  [02/07] assert                             → OK           (0 ms)
  [03/07] read_dtc_fault_counter             → OK           (0 ms)
  [04/07] read_dtc_permanent                 → OK           (0 ms)
  [05/07] clear_dtc                          → OK           (0 ms)
  [06/07] read_dtc                           → OK           (0 ms)
  [07/07] assert                             → OK           (0 ms)

  Result:  PASS
  Steps:   7/7 steps passed
```

## What this is actually testing

**The virtual ECU has no faults set, so it reports zero DTCs.** That is the
correct behaviour for a healthy ECU, and it is worth being clear that this
recipe teaches the *shape* of DTC testing — the request/response workflow
and the assertions around it — not fault injection.

`testlab_config.yaml` declares two codes the ECU *can* report:

```yaml
dtcs:
  - code: "0xC00100"
    description: CAN communication loss — sensor bus
  - code: "0xC00200"
    description: Supply voltage out of range
```

Declaring a DTC is not the same as setting one. A real ECU reports only
codes that are currently stored, which is why the read returns none here.

## The three sub-functions are not interchangeable

`read_dtc`, `read_dtc_fault_counter` and `read_dtc_permanent` are three
different sub-functions of service `0x19`. A real ECU can implement one and
reject another, so the campaign exercises them separately rather than
assuming that supporting `0x19` means supporting all of it:

```yaml
- action: read_dtc
- action: read_dtc_fault_counter
- action: read_dtc_permanent
```

If your ECU rejects one, pin that down explicitly rather than deleting the
step — a rejection you have asserted is information, a step you removed is
not:

```yaml
- action: read_dtc_permanent
  expect_nrc: "0x12"     # subFunctionNotSupported
```

See [`../negative_responses/`](../negative_responses/) for that pattern in
full.

## Why the assert step matters

`read_dtc` on its own tells you *something* came back. The `assert` step
pins down that the saved payload is not itself a negative response:

```yaml
- action: read_dtc
  save_as: dtcs_before

- action: assert
  variable: dtcs_before
  not_nrc: true
```

`assert` also supports `length` and `contains`, which are the useful ones
once your ECU actually has stored codes — `contains: "0xC00100"` asserts a
specific DTC is present in the response.

`dtc_count:` asserts the number of stored codes directly:

```yaml
- action: read_dtc
  dtc_count: 0        # the virtual ECU has no faults set
```

```text
  [01/01] read_dtc      → FAIL  Expected 2 DTC(s), ECU reported 0
```

> It did **not** always work. Until
> [#4](https://github.com/Xaloqi/xaloqi-testlab-core/issues/4), `dtc_count`
> was only ever written as an output field and never compared, so
> `dtc_count: 999` passed against an ECU reporting none. It is a real
> assertion now, on both `read_dtc` and `read_dtc_permanent`.

## Try breaking it

Point an `assert` at a variable that was never saved:

```yaml
- action: assert
  variable: not_a_real_variable
  not_nrc: true
```

```text
  [02/07] assert                             → FAIL  Variable 'not_a_real_variable' not set
```

Or give it a length the response cannot have — `assert` reports both the
real and the expected value:

```text
  [05/05] assert                             → FAIL  length 17 != expected 999
```

## Against real hardware

Run the same campaign unmodified against a real ECU with
[TestLab Pro](https://xaloqi.com): drop `--virtual` and pass your
interface. On hardware with stored faults the reads return real codes, and
`clear_dtc` followed by a re-read becomes a genuine round-trip test.

## See also

- [`../negative_responses/`](../negative_responses/) — asserting rejections correctly
- [`../github_actions/`](../github_actions/) — run this campaign in CI
