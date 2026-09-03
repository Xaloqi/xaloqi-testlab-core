# Test a UDS firmware-download sequence without hardware

Firmware programming is one of the diagnostic flows most useful to
exercise before an ECU bench is available. TestLab Core supports the
standard UDS transfer sequence:

```text
0x10  DiagnosticSessionControl (programming)
0x27  SecurityAccess
0x34  RequestDownload
0x36  TransferData          (one or more PDUs — chunked automatically)
0x37  RequestTransferExit
```

## Why test this in simulation?

A firmware-download test can catch protocol-level problems such as:

- incorrect programming-session behaviour,
- SecurityAccess requirements not being enforced,
- rejected download parameters,
- incorrect transfer-exit behaviour.

This does **not** replace testing the real flash driver on target
hardware — it moves *protocol* validation earlier, before a bench exists.

## Run

```bash
testlab-run \
  --config testlab_config.yaml \
  --campaign examples/firmware_download/campaign.yaml \
  --job firmware_download \
  --virtual \
  --json reports/firmware-download.json

testlab analyze --results reports/firmware-download.json
```

```text
[01/06] session(programming)       → OK
[02/06] security_access(level=1)   → OK
[03/06] request_download           → OK
[04/06] transfer_data              → OK
[05/06] request_transfer_exit      → OK
[06/06] session(default)           → OK

Result:  PASS
```

## Campaign

```yaml
jobs:
  firmware_download:
    description: Exercise a complete UDS firmware download sequence.
    timeout_ms: 30000
    on_failure: abort

    steps:
      - action: session
        value: programming

      - action: security_access
        level: 1

      - action: request_download
        memory_address: "0x00000000"
        memory_size: "0x00000010"

      - action: transfer_data
        file: examples/firmware_download/firmware.bin
        memory_address: "0x00000000"

      - action: request_transfer_exit

      - action: session
        value: default
```

Note the `transfer_data` step takes a **`file:` path**, not inline bytes —
`testlab-run` reads the file and handles ISO-TP block chunking internally
(block size from `safeboot.max_block_length` in `testlab_config.yaml`, or
a default). One `transfer_data` step covers the whole payload regardless
of how many `0x36` PDUs it takes on the wire.

[`firmware.bin`](firmware.bin) bundled here is a **16-byte synthetic
placeholder** (`00 11 22 33 44 55 66 77 88 99 AA BB CC DD EE FF`) — enough
to exercise the protocol sequence against the virtual ECU. It is not real
firmware; swap in your own binary and adjust `memory_address`/
`memory_size` for your target before running this against real hardware.

## Next step

Once the protocol campaign behaves correctly against the virtual ECU, run
the equivalent campaign against your real ECU and transport. That gives a
useful separation:

- **simulation** → protocol correctness
- **target hardware** → flash-driver and hardware correctness
