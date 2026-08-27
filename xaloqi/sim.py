#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
xaloqi/sim.py — UDS ECU simulator for Xaloqi TestLab.

The free tier's engine: a complete simulated UDS ECU that runs with nothing
but Python. Three ways to use it:

    # Zero-install demo — in-process VirtualBus, no CAN stack, no hardware:
    xaloqi-sim --demo

    # In-process ECU for campaign runs (what testlab-run --virtual does):
    from xaloqi.sim import EcuState, handle_request, run_on_bus

    # SocketCAN process on vcan0 (requires xaloqi-tester-pro; this is the
    # ECU container in the Docker Compose stack):
    xaloqi-sim --interface vcan0 [--rx-id 0x7DF] [--tx-id 0x7E8]

Environment:
    ECU_INTERFACE   CAN interface (default: vcan0)
    ECU_RX_ID       Tester sends to this ID (default: 0x7DF)
    ECU_TX_ID       ECU responds on this ID (default: 0x7E8)
    ECU_VERBOSE     Set to "1" to log every frame
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import logging

from xaloqi.tester._isotp import IsoTpEngine
from xaloqi.tester._security import derive_key
from xaloqi.tester import _plugins

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("ECU_VERBOSE") == "1" else logging.INFO,
    format="%(asctime)s [ECU] %(levelname)s %(message)s",
)
log = logging.getLogger("ecu_sim")

_engine = IsoTpEngine()


# ---------------------------------------------------------------------------
# ECU state
# ---------------------------------------------------------------------------

# Simulated flash memory region — matches harness_flash_mock.c base address.
_MEM_BASE: int = 0x08020000
_MEM_SIZE: int = 4096


class EcuState:
    """Mutable ECU state shared across request handlers."""

    def __init__(self) -> None:
        self.session: int = 0x01          # DEFAULT
        self.security_unlocked: bool = False
        self.dtcs: list[tuple[int, int]] = []          # (code, status_mask)
        self.fault_counters: dict[int, int] = {}       # code → fault detection counter
        self.permanent_dtcs: set[int] = set()          # codes marked permanent
        self.dids: dict[int, bytes] = {
            0xF190: b"1HGBH41JXMN109186",  # VIN (17 bytes)
            0xF18C: b"SIM00000001",          # ECU serial number
            0xF187: b"XALOQI-SIM-1.0  ",   # Part number
            0x2001: (3700).to_bytes(2, "big"),  # Cell voltage mV
        }
        self.memory: bytearray = bytearray(_MEM_SIZE)  # simulated flash buffer

    def reset(self) -> None:
        self.session = 0x01
        self.security_unlocked = False


# ---------------------------------------------------------------------------
# UDS request dispatcher
# ---------------------------------------------------------------------------

def handle_request(pdu: bytes, state: EcuState, verbose: bool) -> bytes | None:
    """
    Dispatch a UDS PDU and return the response PDU, or None for no response
    (e.g. TesterPresent with suppressPosRsp).
    """
    if not pdu:
        return None

    sid = pdu[0]

    # 0x10 DiagnosticSessionControl
    if sid == 0x10:
        sub = pdu[1] if len(pdu) > 1 else 0
        if sub == 0x01:  # DEFAULT
            state.session = 0x01
            state.security_unlocked = False
            return bytes([0x50, 0x01, 0x00, 0x19, 0x01, 0xF4])
        elif sub == 0x02:  # PROGRAMMING
            state.session = 0x02
            return bytes([0x50, 0x02, 0x00, 0x19, 0x01, 0xF4])
        elif sub == 0x03:  # EXTENDED
            state.session = 0x03
            return bytes([0x50, 0x03, 0x00, 0x19, 0x01, 0xF4])
        return bytes([0x7F, 0x10, 0x12])  # subFunctionNotSupported

    # 0x11 ECUReset
    if sid == 0x11:
        reset_type = pdu[1] if len(pdu) > 1 else 0x03
        state.reset()
        return bytes([0x51, reset_type])

    # 0x14 ClearDiagnosticInformation
    if sid == 0x14:
        state.dtcs.clear()
        return bytes([0x54])

    # 0x19 ReadDTCInformation
    if sid == 0x19 and len(pdu) >= 2:
        sub = pdu[1]

        if sub == 0x02 and len(pdu) >= 3:
            mask = pdu[2]
            matched = [(code, sm) for code, sm in state.dtcs if sm & mask]
            resp = bytearray([0x59, 0x02, mask])
            for code, sm in matched:
                resp += code.to_bytes(3, "big") + bytes([sm])
            return bytes(resp)

        if sub == 0x0B:
            # reportDTCFaultDetectionCounter — no availabilityMask in response
            resp = bytearray([0x59, 0x0B])
            for code, status in state.dtcs:
                if status & 0x01:           # testFailed — skip confirmed DTCs
                    continue
                counter = state.fault_counters.get(code, 0x00)
                if counter == 0xFF:         # reserved (confirmed) — exclude
                    continue
                resp += code.to_bytes(3, "big") + bytes([counter])
            return bytes(resp)

        if sub == 0x19:
            # reportDTCWithPermanentStatus — same layout as 0x02
            resp = bytearray([0x59, 0x19, 0xFF])  # 0xFF = availability mask
            for code, status in state.dtcs:
                if code in state.permanent_dtcs:
                    resp += code.to_bytes(3, "big") + bytes([status])
            return bytes(resp)

        return bytes([0x7F, 0x19, 0x12])  # subFunctionNotSupported

    # 0x22 ReadDataByIdentifier
    if sid == 0x22:
        if len(pdu) < 3:
            return bytes([0x7F, 0x22, 0x13])
        did_id = int.from_bytes(pdu[1:3], "big")
        if did_id not in state.dids:
            return bytes([0x7F, 0x22, 0x31])  # requestOutOfRange
        if did_id == 0xF187 and not state.security_unlocked:
            return bytes([0x7F, 0x22, 0x33])  # securityAccessDenied
        data = state.dids[did_id]
        return bytes([0x62]) + pdu[1:3] + data

    # 0x23 ReadMemoryByAddress
    if sid == 0x23:
        if len(pdu) < 4:
            return bytes([0x7F, 0x23, 0x13])  # incorrectMessageLength
        if state.session != 0x02 or not state.security_unlocked:
            return bytes([0x7F, 0x23, 0x7F])  # serviceNotSupportedInActiveSession
        alfid    = pdu[1]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        if addr_len == 0 or addr_len > 4 or size_len == 0 or size_len > 4:
            return bytes([0x7F, 0x23, 0x31])  # requestOutOfRange
        if len(pdu) < 2 + addr_len + size_len:
            return bytes([0x7F, 0x23, 0x13])
        address = int.from_bytes(pdu[2:2 + addr_len], "big")
        size    = int.from_bytes(pdu[2 + addr_len:2 + addr_len + size_len], "big")
        if address < _MEM_BASE or address + size > _MEM_BASE + _MEM_SIZE:
            return bytes([0x7F, 0x23, 0x31])  # requestOutOfRange
        offset = address - _MEM_BASE
        return bytes([0x63]) + bytes(state.memory[offset:offset + size])

    # 0x27 SecurityAccess
    if sid == 0x27:
        if len(pdu) < 2:
            return bytes([0x7F, 0x27, 0x13])
        if state.session == 0x01:
            return bytes([0x7F, 0x27, 0x7F])  # serviceNotSupportedInActiveSession
        sub = pdu[1]
        if sub % 2 == 1:
            # Seed request (odd sub-function)
            level = (sub + 1) // 2
            seed = bytes([0x47, 0xAB, 0x09, 0xBB])  # fixed seed for sim
            return bytes([0x67, sub]) + seed
        else:
            # Key response (even sub-function)
            level = sub // 2
            seed = bytes([0x47, 0xAB, 0x09, 0xBB])
            expected = derive_key(seed, level)
            received = pdu[2:6] if len(pdu) >= 6 else b""
            if received != expected:
                return bytes([0x7F, 0x27, 0x35])  # invalidKey
            state.security_unlocked = True
            return bytes([0x67, sub])

    # 0x28 CommunicationControl
    if sid == 0x28:
        return bytes([0x68, pdu[1] if len(pdu) > 1 else 0x00])

    # 0x2E WriteDataByIdentifier
    if sid == 0x2E:
        if len(pdu) < 3:
            return bytes([0x7F, 0x2E, 0x13])
        did_id = int.from_bytes(pdu[1:3], "big")
        if state.session == 0x01:
            return bytes([0x7F, 0x2E, 0x7F])  # serviceNotSupportedInActiveSession
        state.dids[did_id] = pdu[3:]
        return bytes([0x6E]) + pdu[1:3]

    # 0x31 RoutineControl
    if sid == 0x31:
        if len(pdu) < 4:
            return bytes([0x7F, 0x31, 0x13])
        ctrl = pdu[1]
        rid = int.from_bytes(pdu[2:4], "big")
        return bytes([0x71, ctrl]) + pdu[2:4] + b"\x00"

    # 0x34 RequestDownload
    if sid == 0x34:
        if state.session != 0x02:
            return bytes([0x7F, 0x34, 0x7F])
        # maxBlockLen=0x0FFF in lengthAndFormat=0x20
        return bytes([0x74, 0x20, 0x0F, 0xFF])

    # 0x35 RequestUpload
    if sid == 0x35:
        if state.session != 0x02:
            return bytes([0x7F, 0x35, 0x7F])
        # maxBlockLen=0x0FFF in lengthAndFormat=0x20 (same as download)
        return bytes([0x75, 0x20, 0x0F, 0xFF])

    # 0x36 TransferData
    if sid == 0x36:
        seq = pdu[1] if len(pdu) > 1 else 0
        return bytes([0x76, seq])

    # 0x37 RequestTransferExit
    if sid == 0x37:
        return bytes([0x77])

    # 0x3D WriteMemoryByAddress
    if sid == 0x3D:
        if len(pdu) < 4:
            return bytes([0x7F, 0x3D, 0x13])  # incorrectMessageLength
        if state.session != 0x02 or not state.security_unlocked:
            return bytes([0x7F, 0x3D, 0x7F])  # serviceNotSupportedInActiveSession
        alfid    = pdu[1]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        if addr_len == 0 or addr_len > 4 or size_len == 0 or size_len > 4:
            return bytes([0x7F, 0x3D, 0x31])
        if len(pdu) < 2 + addr_len + size_len:
            return bytes([0x7F, 0x3D, 0x13])
        address  = int.from_bytes(pdu[2:2 + addr_len], "big")
        size     = int.from_bytes(pdu[2 + addr_len:2 + addr_len + size_len], "big")
        data     = pdu[2 + addr_len + size_len:]
        if address < _MEM_BASE or address + size > _MEM_BASE + _MEM_SIZE:
            return bytes([0x7F, 0x3D, 0x31])
        if len(data) != size:
            return bytes([0x7F, 0x3D, 0x13])
        offset = address - _MEM_BASE
        state.memory[offset:offset + size] = data
        return (bytes([0x7D, alfid])
                + address.to_bytes(addr_len, "big")
                + size.to_bytes(size_len, "big"))

    # 0x3E TesterPresent
    if sid == 0x3E:
        sub = pdu[1] if len(pdu) > 1 else 0x00
        if (sub & 0x7F) != 0x00:  # only subFunction 0x00 is valid
            return bytes([0x7F, 0x3E, 0x12])  # NRC 0x12 subFunctionNotSupported
        if sub & 0x80:
            return None  # suppress positive response
        return bytes([0x7E, 0x00])

    # 0x85 ControlDTCSetting
    if sid == 0x85:
        setting = pdu[1] if len(pdu) > 1 else 0x01
        return bytes([0xC5, setting])

    # Unknown SID
    return bytes([0x7F, sid, 0x11])  # serviceNotSupported



# ---------------------------------------------------------------------------
# In-process serving loop (VirtualBus)
# ---------------------------------------------------------------------------

async def run_on_bus(bus, state_box: dict, stop: asyncio.Event,
                     tx_id: int, verbose: bool = False) -> None:
    """Serve UDS requests on an already-open bus until `stop` is set.

    `state_box` is a one-slot dict {"state": EcuState()} so the caller can
    swap in a fresh EcuState between jobs (job-state isolation — the runner's
    --virtual mode relies on this; validation report Bug 10).
    """
    while not stop.is_set():
        result = await bus.recv(timeout=0.05)
        if result is None:
            continue
        _, frame = result
        pci = (frame[0] >> 4) & 0x0F
        if pci == 0x0:
            length = frame[0] & 0x0F
            pdu = bytes(frame[1:1 + length])
        elif pci == 0x1:
            expected_len = ((frame[0] & 0x0F) << 8) | frame[1]
            chunks = [bytes(frame[2:])]
            await bus.send(tx_id, _engine.flow_control_cts())
            while sum(len(c) for c in chunks) < expected_len:
                res = await bus.recv(timeout=0.5)
                if res is None:
                    break
                _, cf = res
                if (cf[0] >> 4) == 0x2:
                    chunks.append(bytes(cf[1:]))
            pdu = (b"".join(chunks))[:expected_len]
        else:
            continue
        resp = handle_request(pdu, state_box["state"], verbose)
        if resp is None:
            continue
        frames = _engine.encode(resp)
        if len(frames) == 1:
            await bus.send(tx_id, frames[0])
        else:
            await bus.send(tx_id, frames[0])
            await bus.recv(timeout=0.5)
            for cf in frames[1:]:
                await bus.send(tx_id, cf)


# ---------------------------------------------------------------------------
# Zero-install demo (the wedge): sim + tester in one process, no hardware
# ---------------------------------------------------------------------------

async def run_demo(verbose: bool = False) -> int:
    """Run a short UDS conversation against the in-process simulator."""
    from xaloqi.tester import UdsTester, Session
    from xaloqi.tester.transport.virtual import VirtualBus

    rx_id, tx_id = 0x7DF, 0x7E8  # ECU perspective
    tester_bus, ecu_bus = VirtualBus.pair("xaloqi-sim-demo")
    state_box = {"state": EcuState()}
    stop = asyncio.Event()
    task = asyncio.create_task(run_on_bus(ecu_bus, state_box, stop, tx_id, verbose))

    print("\nXaloqi TestLab — simulated ECU demo (no hardware, no CAN stack)")
    print("─" * 64)
    ok = True
    try:
        async with UdsTester(tester_bus, rx_id=tx_id, tx_id=rx_id,
                             keepalive=False, verbose=verbose) as ecu:
            vin = await ecu.read_did(0xF190)
            print(f"  read_did(0xF190)  VIN            → {vin.data.decode(errors='replace')}")
            await ecu.session(Session.EXTENDED)
            print("  session(extended)                → OK")
            await ecu.security_access(level=1)
            print("  security_access(level=1)         → unlocked (AES-CMAC)")
            part = await ecu.read_did(0xF187)
            print(f"  read_did(0xF187)  part number   → {part.data.decode(errors='replace').strip()}")
            dtcs = await ecu.read_dtcs()
            print(f"  read_dtc                         → {len(dtcs.dtcs)} DTC(s)")
    except Exception as exc:
        ok = False
        print(f"  FAILED: {type(exc).__name__}: {exc}")
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("─" * 64)
    if ok:
        print("  All exchanges OK. Next: run a full YAML campaign against this ECU")
        print("  with `testlab-run --virtual` — quickstart and example campaigns:")
        print("    https://xaloqi.com/testlab/docs\n")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main loop (SocketCAN process — requires xaloqi-tester-pro)
# ---------------------------------------------------------------------------

async def run(interface: str, rx_id: int, tx_id: int, verbose: bool) -> None:
    log.info("ECU simulator starting on %s (rx=0x%X tx=0x%X)", interface, rx_id, tx_id)
    state = EcuState()

    socketcan_factory = _plugins.get_transport("socketcan")
    async with socketcan_factory(interface) as bus:
        log.info("CAN bus open. Waiting for requests...")
        while True:
            try:
                result = await bus.recv(timeout=5.0)
                if result is None:
                    continue

                arb_id, frame = result
                if arb_id != rx_id:
                    continue

                pci_type = (frame[0] >> 4) & 0x0F

                # Receive full ISO-TP message
                if pci_type == 0x0:  # SF
                    length = frame[0] & 0x0F
                    pdu = bytes(frame[1: 1 + length])
                elif pci_type == 0x1:  # FF — multi-frame request
                    expected_len = ((frame[0] & 0x0F) << 8) | frame[1]
                    frames_rx = [bytes(frame[2:])]
                    expected_sn = 1
                    # Send FC CTS
                    await bus.send(tx_id, _engine.flow_control_cts())
                    # Collect CFs
                    while sum(len(f) for f in frames_rx) < expected_len:
                        res = await bus.recv(timeout=1.0)
                        if res is None:
                            break
                        _, cf = res
                        if (cf[0] >> 4) == 0x2:
                            frames_rx.append(bytes(cf[1:]))
                    raw = b"".join(frames_rx)
                    pdu = raw[:expected_len]
                else:
                    continue

                if verbose:
                    log.debug("REQ  %s", pdu.hex())

                resp = handle_request(pdu, state, verbose)

                if resp is None:
                    continue  # no response (suppress)

                if verbose:
                    log.debug("RESP %s", resp.hex())

                frames = _engine.encode(resp)
                if len(frames) == 1:
                    await bus.send(tx_id, frames[0])
                else:
                    await bus.send(tx_id, frames[0])  # FF
                    await bus.recv(timeout=1.0)        # wait for FC CTS from tester
                    for cf in frames[1:]:
                        await bus.send(tx_id, cf)

            except Exception as exc:
                log.error("Error in main loop: %s", exc, exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="xaloqi-sim",
        description="Xaloqi TestLab ECU simulator — try a UDS server with nothing but Python",
    )
    parser.add_argument("--demo", action="store_true",
                        help="Run an in-process UDS demo against the simulator "
                             "(no hardware, no CAN stack)")
    parser.add_argument("--interface", default=os.environ.get("ECU_INTERFACE", "vcan0"),
                        help="SocketCAN interface to serve on (requires "
                             "xaloqi-tester-pro; default: vcan0)")
    parser.add_argument("--rx-id", type=lambda x: int(x, 0),
                        default=int(os.environ.get("ECU_RX_ID", "0x7DF"), 0))
    parser.add_argument("--tx-id", type=lambda x: int(x, 0),
                        default=int(os.environ.get("ECU_TX_ID", "0x7E8"), 0))
    parser.add_argument("--verbose", action="store_true",
                        default=os.environ.get("ECU_VERBOSE") == "1")
    args = parser.parse_args()

    if args.demo:
        return asyncio.run(run_demo(args.verbose))

    try:
        asyncio.run(run(args.interface, args.rx_id, args.tx_id, args.verbose))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Most likely: no SocketCAN transport installed (core-only install)
        print(f"ERROR: {exc}", file=sys.stderr)
        print("\nHint: `xaloqi-sim --demo` runs a full in-process demo with no "
              "hardware and no extra install.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
