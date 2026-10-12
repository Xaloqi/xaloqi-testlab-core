#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
reference_uds_stub.py -- a minimal, independent UDS server, written fresh
for this package and sharing no code with EDS, used to prove the
conformance corpus's vectors are not secretly keyed to EDS's own
implementation.

This is NOT a product, NOT a general-purpose UDS stack, and NOT a claim
about real-world interoperability -- it implements exactly the handful of
services and exactly the fixture (DID 0x0001, RID 0x0012, a trivial
seed-XOR-0xAA security algorithm) the uds_sid conformance corpus's own
four "requires_fixture" vectors need, nothing more. The point it proves is
narrow and real: a target that implements ISO 14229-1's byte-level
request/response shapes correctly passes these vectors regardless of
whose C code (or Python code) it's running. For a broader "does my
real/independent stack conform" exercise, point the corpus at your own
implementation instead -- that's the whole design, see this package's
README.

Exposed as a CanBus with is_message_transport=True (a complete UDS PDU in,
a complete UDS PDU out -- see xaloqi.tester.UdsTester._request_message),
so it plugs directly into ``UdsTester(interface=ReferenceUdsBus(...), ...)``
with zero new transport code, hardware, or network -- no pairing needed,
this *is* the whole bus. See tests/test_conformance_reference_stub.py for
the dogfood run against the corpus's own vectors.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple

from xaloqi.tester.transport.base import CanBus

_NRC_SERVICE_NOT_SUPPORTED = 0x11
_NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
_NRC_INCORRECT_LENGTH = 0x13
_NRC_CONDITIONS_NOT_CORRECT = 0x22
_NRC_REQUEST_OUT_OF_RANGE = 0x31
_NRC_INVALID_KEY = 0x35
_NRC_SERVICE_NOT_SUPPORTED_IN_SESSION = 0x7F

_TEST_DID = 0x0001
_TEST_RID = 0x0012


class ReferenceUdsServer:
    """The dispatcher itself -- independent of any transport."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.session = "default"
        self.security_level = 0
        self._pending_seed: Optional[Tuple[int, bytes]] = None  # (level, seed)
        self.did_data = bytearray(b"\x99\x88")
        self.last_write = b""

    def _neg(self, sid: int, nrc: int) -> bytes:
        return bytes([0x7F, sid, nrc])

    def handle(self, req: bytes) -> bytes:
        if not req:
            return self._neg(0x00, _NRC_INCORRECT_LENGTH)
        sid = req[0]
        if sid == 0x00:
            return self._neg(0x00, _NRC_SERVICE_NOT_SUPPORTED)
        if sid == 0x10:
            return self._handle_session_control(req)
        if sid == 0x11:
            return self._handle_ecu_reset(req)
        if sid == 0x14:
            return self._handle_clear_dtc(req)
        if sid == 0x22:
            return self._handle_read_did(req)
        if sid == 0x27:
            return self._handle_security_access(req)
        if sid == 0x28:
            return self._handle_comm_control(req)
        if sid == 0x2E:
            return self._handle_write_did(req)
        if sid == 0x31:
            return self._handle_routine_control(req)
        if sid == 0x3E:
            return self._handle_tester_present(req)
        if sid == 0x85:
            return self._handle_dtc_setting(req)
        return self._neg(sid, _NRC_SERVICE_NOT_SUPPORTED)

    # ── Services ──────────────────────────────────────────────────────────

    def _handle_session_control(self, req: bytes) -> bytes:
        if len(req) != 2:
            return self._neg(0x10, _NRC_INCORRECT_LENGTH)
        subfn = req[1] & 0x7F
        suppress = bool(req[1] & 0x80)
        if subfn == 0x01:
            self.session = "default"
        elif subfn == 0x03:
            self.session = "extended"
        else:
            return self._neg(0x10, _NRC_SUBFUNCTION_NOT_SUPPORTED)
        if suppress:
            return b""
        return bytes([0x50, subfn, 0x00, 0x32, 0x01, 0xF4])

    def _handle_ecu_reset(self, req: bytes) -> bytes:
        if len(req) != 2 or req[1] != 0x01:
            return self._neg(0x11, _NRC_SUBFUNCTION_NOT_SUPPORTED)
        return bytes([0x51, 0x01])

    def _require_non_default(self, sid: int) -> Optional[bytes]:
        if self.session == "default":
            return self._neg(sid, _NRC_SERVICE_NOT_SUPPORTED_IN_SESSION)
        return None

    def _handle_clear_dtc(self, req: bytes) -> bytes:
        err = self._require_non_default(0x14)
        if err:
            return err
        if len(req) != 4:
            return self._neg(0x14, _NRC_INCORRECT_LENGTH)
        return bytes([0x54])

    def _handle_comm_control(self, req: bytes) -> bytes:
        err = self._require_non_default(0x28)
        if err:
            return err
        if len(req) != 3:
            return self._neg(0x28, _NRC_INCORRECT_LENGTH)
        return bytes([0x68, req[1]])

    def _handle_dtc_setting(self, req: bytes) -> bytes:
        err = self._require_non_default(0x85)
        if err:
            return err
        if len(req) != 2:
            return self._neg(0x85, _NRC_INCORRECT_LENGTH)
        return bytes([0xC5, req[1]])

    def _handle_tester_present(self, req: bytes) -> bytes:
        if len(req) != 2 or (req[1] & 0x7F) != 0x00:
            return self._neg(0x3E, _NRC_SUBFUNCTION_NOT_SUPPORTED)
        if req[1] & 0x80:
            return b""
        return bytes([0x7E, 0x00])

    def _handle_security_access(self, req: bytes) -> bytes:
        if len(req) < 2:
            return self._neg(0x27, _NRC_INCORRECT_LENGTH)
        subfn = req[1]
        if subfn % 2 == 1:  # odd: RequestSeed
            level = subfn
            seed = bytes([0x12, 0x34, 0x56, 0x78])
            self._pending_seed = (level, seed)
            return bytes([0x67, subfn]) + seed
        # even: SendKey
        level = subfn - 1
        key = req[2:]
        if self._pending_seed is None or self._pending_seed[0] != level:
            return self._neg(0x27, _NRC_REQUEST_OUT_OF_RANGE)
        expected = bytes(b ^ 0xAA for b in self._pending_seed[1])
        if key != expected:
            self._pending_seed = None
            return self._neg(0x27, _NRC_INVALID_KEY)
        self.security_level = level
        self._pending_seed = None
        return bytes([0x67, subfn])

    def _handle_read_did(self, req: bytes) -> bytes:
        if len(req) != 3:
            return self._neg(0x22, _NRC_INCORRECT_LENGTH)
        did = (req[1] << 8) | req[2]
        if did != _TEST_DID:
            return self._neg(0x22, _NRC_REQUEST_OUT_OF_RANGE)
        return bytes([0x62, req[1], req[2]]) + bytes(self.did_data)

    def _handle_write_did(self, req: bytes) -> bytes:
        err = self._require_non_default(0x2E)
        if err:
            return err
        if self.security_level < 1:
            return self._neg(0x2E, _NRC_CONDITIONS_NOT_CORRECT)
        if len(req) < 3:
            return self._neg(0x2E, _NRC_INCORRECT_LENGTH)
        did = (req[1] << 8) | req[2]
        if did != _TEST_DID:
            return self._neg(0x2E, _NRC_REQUEST_OUT_OF_RANGE)
        data = req[3:]
        if len(data) != len(self.did_data):
            return self._neg(0x2E, _NRC_INCORRECT_LENGTH)
        self.did_data = bytearray(data)
        self.last_write = bytes(data)
        return bytes([0x6E, req[1], req[2]])

    def _handle_routine_control(self, req: bytes) -> bytes:
        if len(req) < 4:
            return self._neg(0x31, _NRC_INCORRECT_LENGTH)
        ctrl_type = req[1]
        rid = (req[2] << 8) | req[3]
        if rid != _TEST_RID:
            return self._neg(0x31, _NRC_REQUEST_OUT_OF_RANGE)
        if ctrl_type != 0x01:
            return self._neg(0x31, _NRC_SUBFUNCTION_NOT_SUPPORTED)
        return bytes([0x71, ctrl_type, req[2], req[3], 0x00, 0x00])


class ReferenceUdsBus(CanBus):
    """Wraps ReferenceUdsServer as a message-oriented CanBus (one complete
    UDS PDU per send()/recv() pair) -- see xaloqi.tester.UdsTester's own
    is_message_transport path, which this plugs into directly."""

    is_message_transport = True

    def __init__(self, server: ReferenceUdsServer, rx_id: int) -> None:
        self._server = server
        self._rx_id = rx_id
        self._pending: Optional[bytes] = None

    async def send(self, arbitration_id: int, data: bytes) -> None:
        self._pending = self._server.handle(bytes(data))

    async def recv(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        if self._pending is None:
            await asyncio.sleep(0)
            return None
        resp, self._pending = self._pending, None
        return self._rx_id, resp

    async def close(self) -> None:
        pass
