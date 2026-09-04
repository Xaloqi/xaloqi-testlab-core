# Test negative responses (NRCs) without an ECU

A diagnostic test suite that only checks the happy path proves very little.
The interesting half is whether the ECU **rejects** what it should, with the
**correct** negative response code.

This example asserts three different rejections, then proves the same
operations succeed once their preconditions are met. Everything runs
against the built-in TestLab virtual ECU — no hardware, no CAN interface.

## Install

```bash
pip install xaloqi-tester
```

## Run

From the root of this repository:

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign examples/negative_responses/campaign.yaml \
  --job negative_responses \
  --virtual
```

```text
  [01/08] read_did(0xDEAD)                   → OK           (0 ms)
  [02/08] read_did(0xF187)                   → OK           (0 ms)
  [03/08] security_access(level=1)           → OK           (0 ms)
  [04/08] session(extended)                  → OK           (0 ms)
  [05/08] security_access(level=1)           → OK           (24 ms)
  [06/08] read_did(0xF187)                   → OK           (0 ms)
  [07/08] assert                             → OK           (0 ms)
  [08/08] session(default)                   → OK           (0 ms)

  Result:  PASS
  Steps:   8/8 steps passed
```

Steps 1–3 are **rejections**. They report `OK` because the ECU returned
exactly the negative response the campaign demanded — `expect_nrc` inverts
the pass condition for that step.

## The three rejections

| Step | Request | Expected NRC | Why |
|---|---|---|---|
| 1 | `read_did(0xDEAD)` | `0x31` `requestOutOfRange` | The identifier does not exist on this ECU |
| 2 | `read_did(0xF187)` while locked | `0x33` `securityAccessDenied` | `0xF187` is declared `read_security_level: 1` |
| 3 | `security_access(level=1)` in the default session | `0x7F` `serviceNotSupportedInActiveSession` | `0x27` is not available in the default session |

```yaml
- action: read_did
  did: "0xDEAD"
  expect_nrc: "0x31"
```

## Why the positive control matters

Steps 4–8 repeat the operations *correctly*: open an extended session,
unlock with SecurityAccess, read `0xF187`, and assert the value is not
itself a negative response.

Without that, a campaign of pure rejections passes just as happily against
an ECU that rejects **everything** — including a bricked one. Asserting the
rejection and the success together is what makes the result meaningful.

```yaml
- action: read_did
  did: "0xF187"
  save_as: part_number

- action: assert
  variable: part_number
  not_nrc: true
```

## Try breaking it

Change step 1's expectation to a different code:

```yaml
expect_nrc: "0x33"     # was 0x31
```

and re-run. The campaign fails immediately, and the report names both the
expectation and what the ECU really said:

```text
  [01/08] read_did(0xDEAD)                   → FAIL  NRC 0x31 (requestOutOfRange)

  ABORTED at step 1 (on_failure: abort)

  Result:  FAIL
```

That check is worth doing on your own campaigns. A negative test you have
never seen fail is a negative test you cannot trust.

> ⚠️ **Misspelled assertion keys are currently ignored.** Writing
> `expect_ncr:` instead of `expect_nrc:` does not error — the step simply
> loses its assertion and reports `OK`. Tracked as
> [#4](https://github.com/Xaloqi/xaloqi-testlab-core/issues/4). Until it is
> fixed, deliberately breaking a new assertion once (as above) is the
> reliable way to confirm it is actually wired up.

## Against real hardware

The same campaign runs unmodified against a real ECU with
[TestLab Pro](https://xaloqi.com) — drop `--virtual` and pass your
interface. Expect to adjust the codes: which NRC an ECU returns for an
unknown DID, and whether security is checked before or after the session
precondition, is genuinely implementation-specific.

## See also

- [`../security_access/`](../security_access/) — the seed/key exchange in full
- [`../github_actions/`](../github_actions/) — run this campaign in CI
