# Run UDS tests in GitHub Actions

Run an ISO 14229 regression campaign on every push and pull request —
without ECU hardware attached to the CI runner.

TestLab Core's virtual ECU runs in-process, so the same diagnostic
campaign you use during development becomes a normal CI gate.

## The workflow

Copy [`uds-tests.yml`](uds-tests.yml) into `.github/workflows/uds-tests.yml`
in your own project:

```text
git push
   │
   ▼
GitHub Actions
   │
   ├── install xaloqi-tester
   ├── run UDS campaign against the virtual ECU
   └── save JSON results
   │
   ▼
PASS / FAIL
```

No CAN adapter. No bench reservation. No physical ECU.

## Campaign

This example intentionally keeps the regression suite small — see
[`campaign.yaml`](campaign.yaml):

```yaml
jobs:
  regression:
    timeout_ms: 10000
    on_failure: abort
    steps:
      - action: tester_present
        suppress: false
      - action: read_did
        did: "0xF190"
        save_as: vin
      - action: read_dtc
```

Run it locally first:

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign examples/github_actions/campaign.yaml \
  --job regression \
  --virtual \
  --json reports/run.json

testlab analyze --results reports/run.json
```

If it passes locally, push it.

## What should fail the build?

A diagnostic regression should behave like any other software regression —
for example:

- a required DID stops responding,
- an unexpected NRC appears,
- SecurityAccess no longer works,
- a diagnostic session behaves incorrectly,
- firmware transfer breaks,
- a DTC operation changes unexpectedly.

`testlab-run` returns a non-zero exit code when the campaign fails, so the
workflow becomes a normal pull-request gate.

## Keep the JSON result

The workflow uploads `reports/run.json` as a GitHub Actions artifact. A
failed CI run retains the structured diagnostic result, not just a
terminal screenshot.

## Extend it

Once the basic regression works, add dedicated jobs for SecurityAccess
(see [`examples/security_access/`](../security_access/)), DID access
rules, DTC behaviour, negative-response handling, and firmware download
(see [`examples/firmware_download/`](../firmware_download/)).

The goal isn't to replace hardware-in-the-loop testing. It's to catch
diagnostic regressions before they consume scarce HiL time.
