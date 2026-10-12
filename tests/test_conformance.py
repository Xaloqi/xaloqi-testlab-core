# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
test_conformance.py -- tests for xaloqi.tester.conformance.

Three things get proven here, deliberately kept separate:

1. The corpus loads and every vector's steps are structurally sane
   (test_corpus_*).
2. The runner's own judging logic is correct -- a deliberately broken
   scripted bus must be reported as failed, not silently passed
   (test_runner_catches_a_wrong_response, mirroring this repo's "a new
   regression test isn't done until it has failed" convention).
3. The FULL uds_sid suite, including every requires_fixture vector, passes
   against examples/conformance_bindings/reference_uds_stub.py -- an
   independent implementation sharing no code with EDS. This is the "not
   EDS-only" proof the corpus's own design exists to make possible; see
   this package's README.
"""
import sys
from pathlib import Path

import pytest

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "conformance_bindings"
sys.path.insert(0, str(_EXAMPLES_DIR))

from xaloqi.tester import UdsTester  # noqa: E402
from xaloqi.tester.transport.base import CanBus  # noqa: E402
from xaloqi.tester.conformance import (  # noqa: E402
    load_suite, run_uds_sid_suite, run_doip_suite, run_isotp_suite,
)
from xaloqi.tester.conformance.runner import DoipRawProbe  # noqa: E402

from reference_uds_stub import ReferenceUdsServer, ReferenceUdsBus  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Corpus structural sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suite_name", ["isotp", "uds_sid", "doip"])
def test_corpus_loads_and_every_vector_has_steps(suite_name):
    suite = load_suite(suite_name)
    assert suite.vectors, f"{suite_name} corpus has no vectors"
    for v in suite.vectors:
        assert v.steps, f"{suite_name}/{v.id} has no steps"
        for step in v.steps:
            assert step.op in (
                "send", "expect", "expect_none", "drain",
                "expect_dynamic", "compute", "send_computed",
            ), f"{suite_name}/{v.id}: unknown op {step.op!r}"


def test_corpus_vector_ids_are_unique_within_each_suite():
    for suite_name in ("isotp", "uds_sid", "doip"):
        suite = load_suite(suite_name)
        ids = [v.id for v in suite.vectors]
        assert len(ids) == len(set(ids)), f"{suite_name} has duplicate vector ids"


# ---------------------------------------------------------------------------
# 2. Runner judging logic -- must catch a wrong response, not just a right one
# ---------------------------------------------------------------------------


class _ScriptedUdsBus(CanBus):
    """Returns canned responses by request, for exercising the runner's
    own pass/fail logic independent of any real or reference target."""

    is_message_transport = True

    def __init__(self, responses: dict, rx_id: int) -> None:
        self._responses = responses
        self._rx_id = rx_id
        self._pending = None

    async def send(self, arbitration_id, data: bytes) -> None:
        self._pending = self._responses.get(bytes(data), b"\x7f\x00\x11")

    async def recv(self, timeout: float):
        if self._pending is None:
            return None
        resp, self._pending = self._pending, None
        return self._rx_id, resp

    async def close(self) -> None:
        pass


class _ScriptedCanBus(CanBus):
    """Raw-frame scripted bus for the isotp suite: each send() queues a
    canned response keyed by the exact frame received, or nothing."""

    def __init__(self, responses_by_request: dict) -> None:
        self._responses = responses_by_request
        self._queue: list = []

    async def send(self, arbitration_id, data: bytes) -> None:
        resp = self._responses.get(bytes(data))
        if resp is not None:
            self._queue.append((0x111, bytes.fromhex(resp) if isinstance(resp, str) else resp))

    async def recv(self, timeout: float):
        if self._queue:
            return self._queue.pop(0)
        return None

    async def close(self) -> None:
        pass


async def test_isotp_runner_passes_a_correctly_behaving_bus():
    suite = load_suite("isotp")
    vec = suite.vector("VEC-RX-MF-003")
    ff_req = vec.steps[0].data_bytes
    bad_cf = vec.steps[2].data_bytes
    bus = _ScriptedCanBus({ff_req: "30 00 00"})  # correct FC after FF; silent after bad CF
    report = await run_isotp_suite(bus, suite, default_can_id=0x222, vector_ids=["VEC-RX-MF-003"])
    assert report.all_passed, report.results[0].detail


async def test_isotp_runner_catches_an_fc_sent_after_a_bad_sequence_number():
    """A target that (incorrectly) sends a second FC after rejecting a bad
    SN must fail this vector, not pass it."""
    suite = load_suite("isotp")
    vec = suite.vector("VEC-RX-MF-003")
    ff_req = vec.steps[0].data_bytes
    bad_cf = vec.steps[2].data_bytes
    bus = _ScriptedCanBus({ff_req: "30 00 00", bad_cf: "30 00 00"})  # wrong: FC after bad CF too
    report = await run_isotp_suite(bus, suite, default_can_id=0x222, vector_ids=["VEC-RX-MF-003"])
    assert not report.all_passed
    assert "expected no frame" in report.results[0].detail


async def test_runner_passes_a_correct_fixture_independent_vector():
    bus = _ScriptedUdsBus({bytes.fromhex("00"): bytes.fromhex("7F0011")}, rx_id=0x01)
    async with UdsTester(interface=bus, rx_id=0x01, tx_id=0x0E, keepalive=False) as tester:
        suite = load_suite("uds_sid")
        report = await run_uds_sid_suite(
            tester.raw_exchange, suite, vector_ids=["VEC-GEN-001"],
            include_fixture_dependent=False,
        )
    assert report.all_passed, report.results[0].detail
    assert report.passed == 1


async def test_runner_catches_a_wrong_response():
    """The whole point of a byte-exact judge: a WRONG byte must fail, not
    pass. Deliberately return the wrong NRC and confirm the runner reports
    a failure naming the mismatch -- not a crash, not a false pass."""
    bus = _ScriptedUdsBus({bytes.fromhex("00"): bytes.fromhex("7F0022")}, rx_id=0x01)  # wrong NRC
    async with UdsTester(interface=bus, rx_id=0x01, tx_id=0x0E, keepalive=False) as tester:
        suite = load_suite("uds_sid")
        report = await run_uds_sid_suite(
            tester.raw_exchange, suite, vector_ids=["VEC-GEN-001"],
            include_fixture_dependent=False,
        )
    assert not report.all_passed
    assert report.failed == 1
    assert "7F 00 11" in report.results[0].detail
    assert "7F 00 22" in report.results[0].detail


# ---------------------------------------------------------------------------
# 3. The "not EDS-only" proof: full uds_sid suite against an independent,
#    freshly-written reference implementation.
# ---------------------------------------------------------------------------


async def test_full_uds_sid_suite_against_independent_reference_stub():
    server = ReferenceUdsServer()
    bus = ReferenceUdsBus(server, rx_id=0x01)
    suite = load_suite("uds_sid")

    async with UdsTester(interface=bus, rx_id=0x01, tx_id=0x0E, keepalive=False, timeout=1.0) as tester:
        report = await run_uds_sid_suite(
            tester.raw_exchange, suite, include_fixture_dependent=True,
        )

    failures = [r for r in report.results if not r.passed]
    assert not failures, "\n".join(f"{r.vector_id}: {r.detail}" for r in failures)
    assert report.passed == len(suite.vectors)


async def test_security_access_vector_resets_cleanly_between_runs():
    """SecurityAccess state (pending seed, unlocked level) must not leak
    between vectors -- reset() exists for exactly this, mirroring the C
    conformance test's own per-vector setup()."""
    server = ReferenceUdsServer()
    bus = ReferenceUdsBus(server, rx_id=0x01)
    suite = load_suite("uds_sid")

    async with UdsTester(interface=bus, rx_id=0x01, tx_id=0x0E, keepalive=False, timeout=1.0) as tester:
        report1 = await run_uds_sid_suite(
            tester.raw_exchange, suite, vector_ids=["VEC-SID-27-001"],
        )
        server.reset()
        report2 = await run_uds_sid_suite(
            tester.raw_exchange, suite, vector_ids=["VEC-SID-27-001"],
        )
    assert report1.all_passed, report1.results[0].detail
    assert report2.all_passed, report2.results[0].detail


# ---------------------------------------------------------------------------
# DoIP runner mechanics -- a local test double, NOT a conformance claim
# about any real DoIP implementation (see corpus/doip.json's own scope_note).
# ---------------------------------------------------------------------------


async def test_doip_runner_mechanics_against_a_local_test_double():
    import asyncio
    import socket

    suite = load_suite("doip")
    vec = suite.vector("VEC-RA-001")
    req = vec.steps[0].data_bytes
    expected = vec.steps[1].data_bytes

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    async def echo_server():
        loop = asyncio.get_event_loop()
        conn, _ = await loop.run_in_executor(None, server_sock.accept)
        data = await loop.run_in_executor(None, conn.recv, 4096)
        assert data == req
        await loop.run_in_executor(None, conn.sendall, expected)
        conn.close()

    server_task = asyncio.create_task(echo_server())
    probe = DoipRawProbe("127.0.0.1", port)
    await probe.open()
    try:
        report = await run_doip_suite(probe, suite, vector_ids=["VEC-RA-001"])
    finally:
        await probe.close()
        server_sock.close()
        await server_task

    assert report.all_passed, report.results[0].detail
