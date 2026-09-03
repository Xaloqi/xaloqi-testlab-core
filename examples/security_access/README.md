# Test UDS SecurityAccess without an ECU

This example verifies that a security-protected diagnostic resource is:

1. **inaccessible** before SecurityAccess,
2. **unlocked** using UDS service `0x27`,
3. **accessible** after a successful unlock.

Everything runs against the built-in TestLab virtual ECU. No ECU hardware
or CAN interface is required.

## Install

```bash
pip install xaloqi-tester
```

## Run

From the root of this repository:

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign examples/security_access/campaign.yaml \
  --job security_access \
  --virtual
```

```text
[01/05] session(extended)          → OK
[02/05] read_did(0xF187)           → OK
[03/05] security_access(level=1)   → OK
[04/05] read_did(0xF187)           → OK
[05/05] session(default)           → OK

Result:  PASS
```

## What we're testing

The virtual ECU exposes DID `0xF187` (`VehicleManufacturerSparePartNumber`,
see `testlab_config.yaml`) as a security-protected resource: `min_session:
extended`, `read_security_level: 1`. The campaign first attempts to read it
while the ECU is locked:

```yaml
- action: read_did
  did: "0xF187"
  expect_nrc: "0x33"
```

`0x33` is `securityAccessDenied`. That negative response matters: a
security test should prove that unauthorized access is *rejected*, not
merely that authorized access works.

We then request SecurityAccess level 1 — TestLab performs the real
seed/key (AES-CMAC) exchange:

```yaml
- action: security_access
  level: 1
```

And read the same DID again. This time it must succeed:

```yaml
- action: read_did
  did: "0xF187"
  save_as: part_number
```

## Try breaking it

Change:

```yaml
expect_nrc: "0x33"
```

to:

```yaml
expect_nrc: "0x31"
```

and re-run. The campaign now fails — the ECU still returns `0x33`
(`securityAccessDenied`), not the `0x31` (`requestOutOfRange`) the step now
expects:

```text
[02/05] read_did(0xF187)  → FAIL  NRC 0x33 (securityAccessDenied)

ABORTED at step 2 (on_failure: abort)
Result:  FAIL
```

That's the point: `expect_nrc` validates the *exact* failure behaviour,
not just "some error happened" — useful in CI, where a locked DID
returning the wrong NRC is itself a regression worth catching.

## Next

Try adding another security-protected DID, or a second SecurityAccess
level. For a larger security-oriented flow, see
[`campaigns/standalone_validation.yaml`](../../campaigns/standalone_validation.yaml)'s
`security_audit` and `eol_production_check` jobs.
