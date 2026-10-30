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

    # DIDs and routines of a real ECU instead of the built-in set (TestLab#95):
    xaloqi-sim --config diagnostics_config.yaml --interface vcan0
    testlab-run --virtual --sim-from-config --config diagnostics_config.yaml ...

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
from dataclasses import dataclass
from pathlib import Path

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


_SESSION_NUM = {"default": 0x01, "programming": 0x02, "extended": 0x03, "safety": 0x04}


@dataclass(frozen=True)
class DidSpec:
    length: int
    readable: bool = True
    writable: bool = False
    min_session: int = 0x01
    read_level: int = 0
    write_level: int = 0


@dataclass(frozen=True)
class RoutineSpec:
    min_session: int = 0x01
    level: int = 0
    support: frozenset = frozenset({"start", "stop", "results"})


class EcuModel:
    """The DIDs and routines of one ECU, from its EDS ``diagnostics_config.yaml``.

    Without a model the simulator keeps its built-in DID set and answers every
    routine. With one it applies the rules the EDS stack applies after the
    access table: unknown / read-only DID (0x31), session too low (0x31),
    SecurityAccess level (0x33), data length (0x13), routine sub-functions
    (0x12) and results before start (0x24). Tied to the compiled stack by
    core/tests/test_sim_config_model.py (TestLab#95).
    """

    def __init__(self, dids: dict[int, DidSpec], routines: dict[int, RoutineSpec]) -> None:
        self.dids = dids
        self.routines = routines

    @classmethod
    def from_config(cls, config) -> "EcuModel":
        """Build from a parsed config dict, or the path of an EDS diagnostics_config.yaml."""
        if isinstance(config, (str, os.PathLike)):
            import yaml
            config = yaml.safe_load(Path(config).read_text()) or {}

        def num(v) -> int:
            return v if isinstance(v, int) else int(str(v), 0)

        def session(v) -> int:
            return _SESSION_NUM[str(v).lower()] if str(v).lower() in _SESSION_NUM else num(v)

        dids = {}
        for d in config.get("dids") or []:
            access = {str(a).lower() for a in d.get("access", ["read"])}
            dids[num(d["id"])] = DidSpec(
                length=int(d["data_length"]), readable="read" in access, writable="write" in access,
                min_session=session(d.get("min_session", "default")),
                read_level=int(d.get("read_security_level", 0)),
                write_level=int(d.get("write_security_level", 0)))
        routines = {}
        for r in config.get("routines") or []:
            support = frozenset(str(x).lower() for x in r.get("support", ("start", "stop", "results")))
            routines[num(r["id"])] = RoutineSpec(
                min_session=session(r.get("min_session", "default")),
                level=int(r.get("security_level", 0)), support=support)
        return cls(dids, routines)


class EcuState:
    """Mutable ECU state shared across request handlers.

    `model` (an EcuModel) makes the DIDs and routines those of a real ECU's
    config; None keeps the built-in set (TestLab#95).
    """

    def __init__(self, model: "EcuModel | None" = None) -> None:
        self.model = model
        self.routines_started: set[int] = set()
        self.session: int = 0x01          # DEFAULT
        self.unlocked_levels: set[int] = set()   # SecurityAccess levels unlocked
        self.pending_seed_level: int | None = None  # seed sent, key not yet received
        self.transfer: dict | None = None  # active 0x34 download: size, received, next_seq
        self.dtcs: list[tuple[int, int]] = []          # (code, status_mask)
        self.fault_counters: dict[int, int] = {}       # code → fault detection counter
        self.permanent_dtcs: set[int] = set()          # codes marked permanent
        self.dids: dict[int, bytes] = {
            0xF190: b"1HGBH41JXMN109186",  # VIN (17 bytes)
            0xF18C: b"SIM00000001",          # ECU serial number
            0xF187: b"XALOQI-SIM-1.0  ",   # Part number
            0x2001: (3700).to_bytes(2, "big"),  # Cell voltage mV
        }
        if model is not None:
            builtin, self.dids = self.dids, {}
            for did, spec in model.dids.items():
                old = builtin.get(did, b"")
                self.dids[did] = old if len(old) == spec.length else bytes(spec.length)
        self.memory: bytearray = bytearray(_MEM_SIZE)  # simulated flash buffer

    @property
    def security_unlocked(self) -> bool:
        """True when level 1 (the level every gated service asks for) is unlocked."""
        return 1 in self.unlocked_levels

    @security_unlocked.setter
    def security_unlocked(self, value: bool) -> None:
        self.unlocked_levels = {1} if value else set()
        if not value:
            self.pending_seed_level = None

    def relock(self) -> None:
        """What a session change does: relock SecurityAccess, forget a pending
        seed, abort a running download."""
        self.unlocked_levels = set()
        self.pending_seed_level = None
        self.transfer = None

    def reset(self) -> None:
        self.session = 0x01
        self.routines_started.clear()
        self.relock()


# ---------------------------------------------------------------------------
# Access table (session / security gates)
# ---------------------------------------------------------------------------

_NON_DEFAULT = frozenset({0x02, 0x03, 0x04})
_PROGRAMMING = frozenset({0x02})

# service id -> (sessions the service is available in, needs SecurityAccess).
# Mirrors the default access table of the EDS stack (core/uds_access_table.c).
# The stack checks it after finding the service and before looking at the
# request, so a bare SID in the wrong session is 0x7F, not 0x13. Tied to the
# compiled stack by core/tests/test_sim_acl.py (TestLab#93). Services missing
# here are open in every session, like the stack's own rows for them.
SERVICE_ACL: dict[int, tuple[frozenset[int], bool]] = {
    0x14: (_NON_DEFAULT, False),   # ClearDiagnosticInformation
    0x23: (_PROGRAMMING, True),    # ReadMemoryByAddress
    0x27: (_NON_DEFAULT, False),   # SecurityAccess
    0x28: (_NON_DEFAULT, False),   # CommunicationControl
    0x2A: (_NON_DEFAULT, False),   # ReadDataByPeriodicIdentifier
    0x2E: (_NON_DEFAULT, True),    # WriteDataByIdentifier
    0x2F: (_NON_DEFAULT, True),    # InputOutputControlByIdentifier
    0x34: (_PROGRAMMING, True),    # RequestDownload
    0x35: (_PROGRAMMING, True),    # RequestUpload
    0x36: (_PROGRAMMING, True),    # TransferData
    0x37: (_PROGRAMMING, True),    # RequestTransferExit
    0x3D: (_PROGRAMMING, True),    # WriteMemoryByAddress
    0x85: (_NON_DEFAULT, False),   # ControlDTCSetting
}

# Services that honour the suppressPosRspMsgIndicationBit (bit 7 of the
# sub-function): a positive response is dropped, a negative one is still sent.
_SUPPRESSIBLE = frozenset({0x10, 0x11, 0x19, 0x27, 0x28, 0x3E, 0x85})

# The seed is fixed on purpose: a campaign that replays a recorded key must
# stay deterministic on --virtual. The EDS stack sends a fresh seed per request
# (so a captured key does not replay on the ECU), and an all-zero seed when the
# level is already unlocked, which the simulator also does. A campaign that
# checks seed freshness or key replay needs the real ECU (TestLab#96).
_SEED = bytes([0x47, 0xAB, 0x09, 0xBB])
_SEC_LEVELS = (1, 2)   # levels with a key: seed sub-functions 0x01, 0x03

_DOWNLOAD_MAX_BLOCK = bytes([0x74, 0x20, 0x0F, 0xFF])  # lengthFormat 0x20, maxBlockLen 0x0FFF


def _acl_refusal(sid: int, state: "EcuState") -> bytes | None:
    """The negative response the access table gives, or None to let it through."""
    rule = SERVICE_ACL.get(sid)
    if rule is None:
        return None
    sessions, needs_unlock = rule
    if state.session not in sessions:
        return bytes([0x7F, sid, 0x7F])  # serviceNotSupportedInActiveSession
    if needs_unlock and not state.security_unlocked:
        return bytes([0x7F, sid, 0x33])  # securityAccessDenied
    return None


def _nrc(sid: int, code: int) -> bytes:
    return bytes([0x7F, sid, code])


# ---------------------------------------------------------------------------
# UDS request dispatcher
# ---------------------------------------------------------------------------

def handle_request(pdu: bytes, state: EcuState, verbose: bool) -> bytes | None:
    """
    Dispatch a UDS PDU and return the response PDU, or None for no response
    (e.g. TesterPresent with suppressPosRsp).

    Order of checks, as in the EDS stack: service known (else 0x11), access
    table (0x7F / 0x33), then the request itself (0x13 length, 0x12 sub-function,
    0x31 value), then the service's state (0x24 sequence, 0x22 conditions).
    """
    if not pdu:
        return None

    sid = pdu[0]

    refusal = _acl_refusal(sid, state)
    if refusal is not None:
        return refusal

    if sid in _SUPPRESSIBLE and len(pdu) >= 2 and pdu[1] & 0x80:
        resp = _dispatch(bytes([sid, pdu[1] & 0x7F]) + pdu[2:], state)
        return None if resp is not None and resp[0] == sid + 0x40 else resp
    return _dispatch(pdu, state)


def _dispatch(pdu: bytes, state: EcuState) -> bytes | None:
    sid = pdu[0]

    # 0x10 DiagnosticSessionControl
    if sid == 0x10:
        if len(pdu) < 2:
            return _nrc(0x10, 0x13)
        sub = pdu[1]
        if sub not in (0x01, 0x02, 0x03):  # session 0x04 is opt-in per ECU, off here
            return _nrc(0x10, 0x12)        # subFunctionNotSupported
        if state.session == 0x02 and sub == 0x03:
            return _nrc(0x10, 0x22)        # programming -> extended is not allowed
        # Every session change relocks SecurityAccess, not only the return to
        # default (ISO 14229-1:2026 §9.2.1; EDS SEC-DSC-RELOCK-01), and ends a
        # download (EDS#442).
        state.session = sub
        state.relock()
        return bytes([0x50, sub, 0x00, 0x19, 0x01, 0xF4])

    # 0x11 ECUReset
    if sid == 0x11:
        if len(pdu) < 2:
            return _nrc(0x11, 0x13)
        if pdu[1] not in (0x01, 0x02, 0x03):
            return _nrc(0x11, 0x12)
        state.reset()
        return bytes([0x51, pdu[1]])

    # 0x14 ClearDiagnosticInformation
    if sid == 0x14:
        if len(pdu) != 4:
            return _nrc(0x14, 0x13)
        group = int.from_bytes(pdu[1:4], "big")
        if group != 0xFFFFFF and group not in {code for code, _ in state.dtcs}:
            return _nrc(0x14, 0x31)
        state.dtcs.clear()
        return bytes([0x54])

    # 0x19 ReadDTCInformation
    if sid == 0x19:
        if len(pdu) < 2:
            return _nrc(0x19, 0x13)
        sub = pdu[1]

        if sub == 0x02:
            if len(pdu) < 3:
                return _nrc(0x19, 0x13)
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

        return _nrc(0x19, 0x12)  # subFunctionNotSupported

    # 0x22 ReadDataByIdentifier: one or more 2-byte DIDs. A DID the ECU does
    # not have is skipped; 0x31 only if none is supported (ISO 14229-1:2026
    # Figure 21, EDS#465).
    if sid == 0x22:
        if len(pdu) < 3 or (len(pdu) - 1) % 2:
            return _nrc(0x22, 0x13)
        out = bytearray([0x62])
        for i in range(1, len(pdu), 2):
            did_id = int.from_bytes(pdu[i:i + 2], "big")
            if did_id not in state.dids:
                continue
            spec = state.model.dids[did_id] if state.model else None
            if spec is not None:
                if not spec.readable or state.session < spec.min_session:
                    continue
                if spec.read_level and spec.read_level not in state.unlocked_levels:
                    return _nrc(0x22, 0x33)
            elif did_id == 0xF187 and not state.security_unlocked:
                return _nrc(0x22, 0x33)  # securityAccessDenied
            out += pdu[i:i + 2] + state.dids[did_id]
        if len(out) == 1:
            return _nrc(0x22, 0x31)  # requestOutOfRange
        return bytes(out)

    # 0x23 ReadMemoryByAddress
    if sid == 0x23:
        if len(pdu) < 4:
            return bytes([0x7F, 0x23, 0x13])  # incorrectMessageLength
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
            return _nrc(0x27, 0x13)
        sub = pdu[1]
        if sub % 2 == 1:
            # Seed request (odd sub-function)
            level = (sub + 1) // 2
            if level not in _SEC_LEVELS:
                return _nrc(0x27, 0x12)  # subFunctionNotSupported
            if level in state.unlocked_levels:
                return bytes([0x67, sub]) + bytes(len(_SEED))  # already unlocked: zero seed
            state.pending_seed_level = level
            return bytes([0x67, sub]) + _SEED
        # Key response (even sub-function): needs a key after the sub-function
        if len(pdu) < 3:
            return _nrc(0x27, 0x13)
        level = sub // 2
        if state.pending_seed_level != level:
            return _nrc(0x27, 0x24)  # requestSequenceError: no seed was sent
        state.pending_seed_level = None
        # quiet=True: this *is* the simulator checking its own key —
        # never a misconfigured-production-ECU situation (O-12).
        expected = derive_key(_SEED, level, quiet=True)
        received = pdu[2:6] if len(pdu) >= 6 else b""
        if received != expected:
            return _nrc(0x27, 0x35)  # invalidKey
        state.unlocked_levels.add(level)
        return bytes([0x67, sub])

    # 0x28 CommunicationControl: controlType 0-3, communicationType validated
    # against ISO 14229-1:2026 Annex B Table B.1 (bits 1-0 = 0, or any of bits
    # 3-2 set, is 0x31). Exactly [SID, controlType, communicationType].
    if sid == 0x28:
        if len(pdu) < 2:
            return _nrc(0x28, 0x13)
        if pdu[1] > 0x03:
            return _nrc(0x28, 0x12)
        if len(pdu) != 3:
            return _nrc(0x28, 0x13)
        comm = pdu[2]
        if (comm & 0x03) == 0 or (comm & 0x0C):
            return _nrc(0x28, 0x31)
        return bytes([0x68, pdu[1]])

    # 0x2A ReadDataByPeriodicIdentifier. The simulator has no periodic DIDs
    # (0xF2xx), so stop-sending works and starting is out of range, as on an
    # EDS ECU with none configured. No periodic frames are ever pushed.
    if sid == 0x2A:
        if len(pdu) < 2:
            return _nrc(0x2A, 0x13)
        if not 0x01 <= pdu[1] <= 0x04:
            return _nrc(0x2A, 0x31)
        if pdu[1] == 0x04:
            return bytes([0x6A])
        if len(pdu) < 3:
            return _nrc(0x2A, 0x13)
        return _nrc(0x2A, 0x31)

    # 0x2E WriteDataByIdentifier
    if sid == 0x2E:
        if len(pdu) < 4:
            return _nrc(0x2E, 0x13)
        did_id = int.from_bytes(pdu[1:3], "big")
        if state.model is not None:
            spec = state.model.dids.get(did_id)
            if spec is None or not spec.writable or state.session < spec.min_session:
                return _nrc(0x2E, 0x31)
            if spec.write_level and spec.write_level not in state.unlocked_levels:
                return _nrc(0x2E, 0x33)
            if len(pdu) - 3 != spec.length:
                return _nrc(0x2E, 0x13)
        state.dids[did_id] = pdu[3:]
        return bytes([0x6E]) + pdu[1:3]

    # 0x2F InputOutputControlByIdentifier. The simulator has no IO-controllable
    # DID, so a well-formed request is out of range, as on an EDS ECU with none.
    if sid == 0x2F:
        if len(pdu) < 4:
            return _nrc(0x2F, 0x13)
        return _nrc(0x2F, 0x31)

    # 0x31 RoutineControl
    if sid == 0x31:
        if len(pdu) < 4:
            return bytes([0x7F, 0x31, 0x13])
        ctrl = pdu[1]
        rid = int.from_bytes(pdu[2:4], "big")
        if state.model is not None:
            spec = state.model.routines.get(rid)
            if spec is None or state.session < spec.min_session:
                return _nrc(0x31, 0x31)
            if spec.level and spec.level not in state.unlocked_levels:
                return _nrc(0x31, 0x33)
            kind = {0x01: "start", 0x02: "stop", 0x03: "results"}.get(ctrl)
            if kind is None or kind not in spec.support:
                return _nrc(0x31, 0x12)
            if kind == "results" and rid not in state.routines_started:
                return _nrc(0x31, 0x24)
            if kind == "start":
                state.routines_started.add(rid)
        return bytes([0x71, ctrl]) + pdu[2:4] + b"\x00"

    # 0x34 RequestDownload
    if sid == 0x34:
        if len(pdu) < 3:
            return _nrc(0x34, 0x13)
        if pdu[1] != 0x00:                 # dataFormatIdentifier: no compression/encryption
            return _nrc(0x34, 0x31)
        alfid    = pdu[2]
        addr_len = alfid & 0x0F
        size_len = (alfid >> 4) & 0x0F
        if addr_len == 0 or addr_len > 4 or size_len == 0 or size_len > 4:
            return _nrc(0x34, 0x31)
        if len(pdu) != 3 + addr_len + size_len:
            return _nrc(0x34, 0x13)
        size = int.from_bytes(pdu[3 + addr_len:], "big")
        # No address check: the memory map is the target ECU's, which the
        # simulator cannot know, and the bundled examples download to 0x08000000.
        if size == 0:
            return _nrc(0x34, 0x31)
        if state.transfer is not None:
            return _nrc(0x34, 0x22)        # a download is already running
        state.transfer = {"size": size, "received": 0, "next_seq": 1}
        return _DOWNLOAD_MAX_BLOCK

    # 0x35 RequestUpload
    if sid == 0x35:
        if len(pdu) < 3:
            return _nrc(0x35, 0x13)
        # maxBlockLen=0x0FFF in lengthAndFormat=0x20 (same as download)
        return bytes([0x75, 0x20, 0x0F, 0xFF])

    # 0x36 TransferData
    if sid == 0x36:
        if len(pdu) < 3:
            return _nrc(0x36, 0x13)
        t = state.transfer
        if t is None or t["received"] == t["size"]:
            return _nrc(0x36, 0x24)        # no download, or all data is in: only 0x37 is next
        seq = pdu[1]
        if seq == (t["next_seq"] - 1) & 0xFF and t["received"]:
            return bytes([0x76, seq])      # the previous block again: acknowledge
        if seq != t["next_seq"]:
            return _nrc(0x36, 0x73)        # wrongBlockSequenceCounter
        if t["received"] + len(pdu) - 2 > t["size"]:
            return _nrc(0x36, 0x71)        # transferDataSuspended: more than announced
        t["received"] += len(pdu) - 2
        t["next_seq"] = (seq + 1) & 0xFF
        return bytes([0x76, seq])

    # 0x37 RequestTransferExit
    if sid == 0x37:
        t = state.transfer
        state.transfer = None              # an early 0x37 ends the download too
        if t is None or t["received"] != t["size"]:
            return _nrc(0x37, 0x24)        # nothing running, or not all data sent
        return bytes([0x77])

    # 0x3D WriteMemoryByAddress
    if sid == 0x3D:
        if len(pdu) < 4:
            return bytes([0x7F, 0x3D, 0x13])  # incorrectMessageLength
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
        if len(pdu) < 2:
            return _nrc(0x3E, 0x13)
        if pdu[1] != 0x00:  # only subFunction 0x00 is valid
            return _nrc(0x3E, 0x12)  # subFunctionNotSupported
        return bytes([0x7E, 0x00])

    # 0x85 ControlDTCSetting
    if sid == 0x85:
        if len(pdu) < 2:
            return _nrc(0x85, 0x13)
        if pdu[1] not in (0x01, 0x02):
            return _nrc(0x85, 0x12)
        return bytes([0xC5, pdu[1]])

    # Unknown SID
    return bytes([0x7F, sid, 0x11])  # serviceNotSupported



# ---------------------------------------------------------------------------
# In-process serving loop (VirtualBus)
# ---------------------------------------------------------------------------

async def run_on_bus(bus, state_box: dict, stop: asyncio.Event,
                     tx_id: int, verbose: bool = False, *,
                     fc_block_size: int = 0, fc_st_min: int = 0) -> None:
    """Serve UDS requests on an already-open bus until `stop` is set.

    `state_box` is a one-slot dict {"state": EcuState()} so the caller can
    swap in a fresh EcuState between jobs (job-state isolation — the runner's
    --virtual mode relies on this; validation report Bug 10).

    `fc_block_size` / `fc_st_min` (keyword-only) let a caller — tests, or
    code driving the sim in-process — make this simulated ECU pace a
    multi-frame request the way a real ECU can: advertise a non-zero
    BlockSize/STmin in its Flow Control, and (when fc_block_size != 0) send
    a further Flow Control after every fc_block_size consecutive frames
    received, until the request is fully reassembled. Defaults (0, 0) leave
    the original single-FC, no-pacing behaviour byte-identical — this is the
    path every current free-tier user (and the SocketCAN `run()` path) hits.
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
            await bus.send(tx_id, _engine.flow_control_cts(fc_block_size, fc_st_min))
            cf_since_fc = 0
            while sum(len(c) for c in chunks) < expected_len:
                res = await bus.recv(timeout=0.5)
                if res is None:
                    break
                _, cf = res
                if (cf[0] >> 4) == 0x2:
                    chunks.append(bytes(cf[1:]))
                    cf_since_fc += 1
                    if (fc_block_size != 0 and cf_since_fc == fc_block_size
                            and sum(len(c) for c in chunks) < expected_len):
                        await bus.send(tx_id, _engine.flow_control_cts(fc_block_size, fc_st_min))
                        cf_since_fc = 0
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
        print("  with the example bundled in this install (no download needed):\n")
        for line in _demo_next_command():
            print(f"    {line}")
        print("\n  More campaigns and quickstart docs: https://xaloqi.com/testlab/docs\n")
    return 0 if ok else 1


def _demo_next_command() -> list[str]:
    """The literal `testlab-run` invocation to print after a successful demo.

    Resolves the config + campaign shipped in `xaloqi.examples` (installed
    alongside this package — see that module's docstring) so the command is
    copy-pasteable and actually works for a pip-installed user with no repo
    checkout (O-11: the previous text pointed at `testlab-run --virtual`
    alone, which fails with "--config or --workspace is required").
    """
    from importlib.resources import files as _resource_files

    examples = _resource_files("xaloqi.examples")
    config = examples.joinpath("testlab_config.yaml")
    campaign = examples.joinpath("campaigns", "standalone_validation.yaml")
    return [
        f"testlab-run --config {config} \\",
        f"            --campaign {campaign} \\",
        "            --job basic_validation --virtual",
    ]


# ---------------------------------------------------------------------------
# Main loop (SocketCAN process — requires xaloqi-tester-pro)
# ---------------------------------------------------------------------------

async def run(interface: str, rx_id: int, tx_id: int, verbose: bool,
              model: "EcuModel | None" = None) -> None:
    log.info("ECU simulator starting on %s (rx=0x%X tx=0x%X)", interface, rx_id, tx_id)
    state = EcuState(model)

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
    parser.add_argument("--config", default=None, metavar="diagnostics_config.yaml",
                        help="Build the simulated ECU's DIDs and routines from an EDS "
                             "diagnostics_config.yaml (default: the built-in set)")
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
        model = EcuModel.from_config(args.config) if args.config else None
        asyncio.run(run(args.interface, args.rx_id, args.tx_id, args.verbose, model))
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
