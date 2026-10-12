# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
runner.py — executes a vector suite against a target and judges byte-exact.

Three entry points, one per suite, because each suite asserts at a
different level and needs a different minimal primitive from the caller:

  run_isotp_suite   -- raw CAN frames. Needs only ``xaloqi.tester.transport
                        .base.CanBus.send()/recv()`` -- already free,
                        Apache-2.0, already in this package. Deliberately
                        bypasses UdsTester/IsoTpEngine: these vectors test
                        segmentation and flow control themselves, so
                        nothing may auto-segment on our behalf.
  run_uds_sid_suite -- complete UDS PDUs. Needs ``UdsTester.raw_exchange()``
                        (added to this package alongside this module --
                        unlike the existing ``raw_request()``, it never
                        raises for a negative response or a timeout, both
                        of which are themselves the expected, correct
                        answer for some vectors here) -- transport-
                        agnostic, works over any CanBus or any message-
                        oriented transport your UdsTester wraps.
  run_doip_suite    -- raw TCP bytes. Needs a ``send_bytes``/``recv_bytes``
                        pair -- see ``DoipRawProbe`` in this module, the one
                        genuinely new transport primitive this package
                        adds, because no free transport here exposes DoIP's
                        own TCP header framing (the thing these vectors
                        test) at all -- see this package's README for why
                        that's a minimal, narrowly-scoped addition, not a
                        competing transport.

Every judge is a plain ``==`` on bytes. No tolerance, no normalisation, no
"close enough." That is the entire value proposition of a conformance
vector: either you emit the independently-sourced bytes, or you don't.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import List, Optional, Protocol

from xaloqi.tester.transport.base import CanBus

from .vectors import Suite, Vector, _hex_to_bytes


@dataclass
class VectorResult:
    vector_id: str
    vector_name: str
    passed: bool
    skipped: bool
    detail: str
    upstream_citation: str


@dataclass
class Report:
    suite_name: str
    results: List[VectorResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed and not r.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed and not r.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        return (
            f"{self.suite_name}: {self.passed} passed, {self.failed} failed, "
            f"{self.skipped} skipped (of {len(self.results)})"
        )


def _select(suite: Suite, vector_ids: Optional[List[str]]) -> List[Vector]:
    if vector_ids is None:
        return list(suite.vectors)
    return [suite.vector(v) for v in vector_ids]


def _citation(v: Vector) -> str:
    if v.upstream is None:
        return ""
    return f"{v.upstream.project} :: {v.upstream.file} ({v.upstream.license})"


# ---------------------------------------------------------------------------
# ISO-TP suite: raw CAN frames via CanBus.
# ---------------------------------------------------------------------------


async def run_isotp_suite(
    bus: CanBus,
    suite: Suite,
    *,
    default_can_id: int,
    vector_ids: Optional[List[str]] = None,
    include_can_fd: bool = False,
    include_fixture_dependent: bool = True,
) -> Report:
    report = Report(suite_name=suite.name)
    for v in _select(suite, vector_ids):
        if v.requires_can_fd and not include_can_fd:
            report.results.append(
                VectorResult(
                    v.id, v.name, passed=False, skipped=True,
                    detail="requires_can_fd and include_can_fd=False",
                    upstream_citation=_citation(v),
                )
            )
            continue
        if v.requires_fixture and not include_fixture_dependent:
            report.results.append(
                VectorResult(
                    v.id, v.name, passed=False, skipped=True,
                    detail=f"requires_fixture: {v.fixture_note}",
                    upstream_citation=_citation(v),
                )
            )
            continue
        detail = ""
        passed = True
        try:
            for step in v.steps:
                can_id = step.can_id if step.can_id is not None else default_can_id
                if step.op == "send":
                    await bus.send(can_id, step.data_bytes)
                elif step.op == "expect":
                    got = await bus.recv(step.timeout_ms / 1000.0)
                    if got is None:
                        passed = False
                        detail = f"{step.note or step.op}: no frame received (timeout)"
                        break
                    _, got_data = got
                    expected = step.data_bytes
                    if got_data[: len(expected)] != expected:
                        passed = False
                        detail = (
                            f"{step.note or step.op}: expected prefix "
                            f"{expected.hex(' ').upper()}, got "
                            f"{got_data.hex(' ').upper()}"
                        )
                        break
                elif step.op == "expect_none":
                    got = await bus.recv(step.timeout_ms / 1000.0)
                    if got is not None:
                        passed = False
                        _, got_data = got
                        detail = (
                            f"{step.note or step.op}: expected no frame, got "
                            f"{got_data.hex(' ').upper()}"
                        )
                        break
                else:
                    raise ValueError(f"unknown step op {step.op!r}")
        except Exception as exc:  # noqa: BLE001 -- report, don't crash the suite
            passed = False
            detail = f"exception: {exc!r}"

        report.results.append(
            VectorResult(
                v.id, v.name, passed=passed, skipped=False,
                detail=detail if not passed else "OK",
                upstream_citation=_citation(v),
            )
        )
    return report


# ---------------------------------------------------------------------------
# UDS-SID suite: complete PDUs via a raw_request(bytes) -> bytes callable.
# ---------------------------------------------------------------------------


class RawRequestFn(Protocol):
    async def __call__(self, pdu: bytes, timeout: Optional[float] = None) -> bytes: ...


async def run_uds_sid_suite(
    raw_request: RawRequestFn,
    suite: Suite,
    *,
    vector_ids: Optional[List[str]] = None,
    include_fixture_dependent: bool = True,
) -> Report:
    report = Report(suite_name=suite.name)
    for v in _select(suite, vector_ids):
        if v.requires_fixture and not include_fixture_dependent:
            report.results.append(
                VectorResult(
                    v.id, v.name, passed=False, skipped=True,
                    detail=f"requires_fixture: {v.fixture_note}",
                    upstream_citation=_citation(v),
                )
            )
            continue
        detail = ""
        passed = True
        _last_resp = b""
        _vars: dict = {}
        try:
            if v.session == "extended":
                switch_resp = await raw_request(bytes([0x10, 0x03]), 1.0)
                if len(switch_resp) < 2 or switch_resp[0] != 0x50:
                    passed = False
                    detail = (
                        f"precondition failed: could not switch to Extended "
                        f"session, got {switch_resp.hex(' ').upper()}"
                    )
            if passed:
                for step in v.steps:
                    if step.op == "send":
                        # raw_exchange() never raises for a negative response
                        # or a timeout -- both come back as bytes (NRC
                        # reconstructed as 7F/SID/NRC, timeout as b"") so this
                        # loop judges bytes, never exceptions. A real
                        # TransportError still propagates and fails below.
                        _last_resp = await raw_request(step.data_bytes, step.timeout_ms / 1000.0)
                    elif step.op == "expect":
                        expected = step.data_bytes
                        actual = _last_resp if step.exact else _last_resp[: len(expected)]
                        if actual != expected:
                            kind = "exact" if step.exact else "prefix"
                            passed = False
                            detail = (
                                f"{step.note or step.op}: expected {kind} "
                                f"{expected.hex(' ').upper()}, got "
                                f"{_last_resp.hex(' ').upper()}"
                            )
                            break
                    elif step.op == "expect_none":
                        if _last_resp != b"":
                            passed = False
                            detail = (
                                f"{step.note or step.op}: expected no response frame, got "
                                f"{_last_resp.hex(' ').upper()}"
                            )
                            break
                    elif step.op == "expect_dynamic":
                        if step.match != "sid_subfn_then_seed":
                            raise ValueError(f"unknown expect_dynamic match {step.match!r}")
                        want_sid = int(step.sid, 16)
                        want_subfn = int(step.subfn, 16)
                        if (
                            len(_last_resp) <= 2
                            or _last_resp[0] != want_sid
                            or _last_resp[1] != want_subfn
                        ):
                            passed = False
                            detail = (
                                f"{step.note or step.op}: expected SID "
                                f"0x{want_sid:02X} sub-fn 0x{want_subfn:02X} "
                                f"then a seed, got {_last_resp.hex(' ').upper()}"
                            )
                            break
                        _vars["last_seed"] = _last_resp[2:]
                    elif step.op == "compute":
                        if step.algorithm != "xor_0xAA":
                            raise ValueError(f"unknown compute algorithm {step.algorithm!r}")
                        src = _vars[step.from_var]
                        _vars[step.store_as] = bytes(b ^ 0xAA for b in src)
                    elif step.op == "send_computed":
                        prefix = _hex_to_bytes(step.prefix)
                        pdu = prefix + _vars[step.value_var]
                        _last_resp = await raw_request(pdu, step.timeout_ms / 1000.0)
                    else:
                        raise ValueError(f"unknown step op {step.op!r}")
        except Exception as exc:  # noqa: BLE001
            passed = False
            detail = f"exception: {exc!r}"

        report.results.append(
            VectorResult(
                v.id, v.name, passed=passed, skipped=False,
                detail=detail if not passed else "OK",
                upstream_citation=_citation(v),
            )
        )
    return report


# ---------------------------------------------------------------------------
# DoIP suite: raw TCP bytes. This is the one new transport primitive.
# ---------------------------------------------------------------------------


class DoipRawProbe:
    """
    A minimal, deliberately narrow raw-TCP-bytes probe for DoIP conformance
    vectors: open a socket, write exactly these bytes, read up to a
    timeout. Nothing else -- no routing-activation lifecycle, no
    reconnection, no SOVD. This is a conformance PROBE, not a DoIP
    transport for test campaigns; use xaloqi-tester-pro's DoipBus for
    that. See this package's README for why that line is drawn here.
    """

    def __init__(self, host: str, port: int, connect_timeout: float = 2.0) -> None:
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._connect_timeout = connect_timeout

    async def open(self) -> None:
        loop = asyncio.get_event_loop()
        self._sock = await loop.run_in_executor(
            None, socket.create_connection, (self._host, self._port), self._connect_timeout
        )

    async def send_bytes(self, data: bytes) -> None:
        assert self._sock is not None, "DoipRawProbe not open"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._sock.sendall, data)

    async def recv_bytes(self, timeout_ms: int) -> bytes:
        assert self._sock is not None, "DoipRawProbe not open"
        self._sock.settimeout(timeout_ms / 1000.0)
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._sock.recv, 4096)
        except socket.timeout:
            return b""

    async def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


async def run_doip_suite(
    probe: "DoipRawProbe",
    suite: Suite,
    *,
    vector_ids: Optional[List[str]] = None,
) -> Report:
    report = Report(suite_name=suite.name)
    for v in _select(suite, vector_ids):
        detail = ""
        passed = True
        try:
            for step in v.steps:
                if step.op == "send":
                    await probe.send_bytes(step.data_bytes)
                elif step.op == "expect":
                    got = await probe.recv_bytes(step.timeout_ms)
                    expected = step.data_bytes
                    actual = got if step.exact else got[: len(expected)]
                    if actual != expected:
                        kind = "exact" if step.exact else "prefix"
                        passed = False
                        detail = (
                            f"{step.note or step.op}: expected {kind} "
                            f"{expected.hex(' ').upper()}, got "
                            f"{got.hex(' ').upper()}"
                        )
                        break
                elif step.op == "expect_none":
                    got = await probe.recv_bytes(step.timeout_ms)
                    if got != b"":
                        passed = False
                        detail = (
                            f"{step.note or step.op}: expected no bytes, got "
                            f"{got.hex(' ').upper()}"
                        )
                        break
                elif step.op == "drain":
                    await probe.recv_bytes(step.timeout_ms)  # intentionally unchecked
                else:
                    raise ValueError(f"unknown step op {step.op!r}")
        except Exception as exc:  # noqa: BLE001
            passed = False
            detail = f"exception: {exc!r}"

        report.results.append(
            VectorResult(
                v.id, v.name, passed=passed, skipped=False,
                detail=detail if not passed else "OK",
                upstream_citation=_citation(v),
            )
        )
    return report
