# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
tests/test_response_pending.py — NRC 0x78 (requestCorrectlyReceived-
ResponsePending) handling in UdsTester (xaloqi-testlab-core#14).

A fake ECU answers each request with a scripted sequence: zero or more
``7F <SID> 78`` frames, a delay before each frame, then (optionally) the
final response. Timings are scaled down (tens of ms) so the suite stays fast;
P2*client is set per test to keep the margins explicit.
"""
import asyncio
from typing import List, Optional, Sequence, Tuple

import pytest

from xaloqi.tester import (
    UdsTester, NrcError, RoutineControl, ResponsePending, ResponsePendingTimeoutError,
    load_eds_config, load_testlab_config,
)
from xaloqi.tester import TimeoutError as UdsTimeoutError
from xaloqi.tester import ConfigError
from xaloqi.tester.transport.base import CanBus
from xaloqi.tester.transport.virtual import VirtualBus
from xaloqi.tester._isotp import IsoTpEngine

_engine = IsoTpEngine()

# One scripted frame: (delay before sending in seconds, UDS PDU).
Step = Tuple[float, bytes]


def pending(sid: int) -> bytes:
    return bytes([0x7F, sid, 0x78])


async def _recv_request(ecu_bus: VirtualBus) -> Optional[bytes]:
    """Single-Frame requests only, which is all these tests send."""
    result = await ecu_bus.recv(timeout=2.0)
    if result is None:
        return None
    _, frame = result
    return bytes(frame[1:1 + (frame[0] & 0x0F)])


async def _send_pdu(ecu_bus: VirtualBus, pdu: bytes) -> None:
    frames = _engine.encode(pdu)
    await ecu_bus.send(0x7E8, frames[0])
    if len(frames) > 1:
        await ecu_bus.recv(timeout=0.5)          # tester's Flow Control
        for cf in frames[1:]:
            await ecu_bus.send(0x7E8, cf)


async def scripted_can_ecu(ecu_bus: VirtualBus, script: Sequence[Step]) -> None:
    """Answer one request with the script, then keep answering it the same way."""
    while True:
        req = await _recv_request(ecu_bus)
        if req is None or req[0] == 0x3E:
            continue
        for delay, pdu in script:
            await asyncio.sleep(delay)
            await _send_pdu(ecu_bus, pdu)


async def run_can(script: Sequence[Step], request: bytes, **tester_kw):
    """Run request against a scripted CAN ECU; return (result_or_exc, elapsed s)."""
    tester_bus, ecu_bus = VirtualBus.pair(f"pending_{id(script)}")
    ecu_task = asyncio.create_task(scripted_can_ecu(ecu_bus, script))
    tester_kw.setdefault("keepalive", False)
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, **tester_kw) as ecu:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                result = await ecu.raw_request(request)
            except Exception as exc:  # noqa: BLE001 — returned for the test to inspect
                result = exc
            return result, loop.time() - t0, ecu
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# CAN (ISO-TP) path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_pending_unchanged():
    """No 0x78: the final answer comes back and the pending record is empty."""
    resp, _, ecu = await run_can([(0.0, bytes([0x71, 0x01, 0x02, 0x00]))],
                                 bytes([0x31, 0x01, 0x02, 0x00]))
    assert resp == bytes([0x71, 0x01, 0x02, 0x00])
    assert ecu._last_pending == ResponsePending()


@pytest.mark.asyncio
async def test_one_pending_then_final():
    resp, _, ecu = await run_can(
        [(0.0, pending(0x31)), (0.05, bytes([0x71, 0x01, 0x02, 0x00]))],
        bytes([0x31, 0x01, 0x02, 0x00]),
        p2_star_timeout=0.5,
    )
    assert resp == bytes([0x71, 0x01, 0x02, 0x00])
    assert ecu._last_pending.count == 1
    assert len(ecu._last_pending.gaps_ms) == 2
    assert ecu._last_pending.gaps_ms[1] >= 40.0


@pytest.mark.asyncio
async def test_n_pending_each_gap_longer_than_p2():
    """Five 0x78s 120 ms apart against a 50 ms P2: only a P2* restart survives."""
    n = 5
    script = [(0.0, pending(0x31))] + [(0.12, pending(0x31))] * (n - 1) + \
             [(0.12, bytes([0x71, 0x01, 0x02, 0x00]))]
    resp, elapsed, ecu = await run_can(script, bytes([0x31, 0x01, 0x02, 0x00]),
                                       timeout=0.05, p2_star_timeout=0.4)
    assert resp == bytes([0x71, 0x01, 0x02, 0x00]), resp
    assert ecu._last_pending.count == n
    gaps = ecu._last_pending.gaps_ms
    assert len(gaps) == n + 1
    assert all(g >= 100.0 for g in gaps[1:]), gaps
    assert elapsed >= 0.12 * n


@pytest.mark.asyncio
async def test_pending_then_multiframe_final():
    """The final answer after a 0x78 may be multi-frame (ISO-TP FF + CFs)."""
    final = bytes([0x62, 0xF1, 0x90]) + bytes(range(17))
    resp, _, ecu = await run_can([(0.0, pending(0x22)), (0.05, final)],
                                 bytes([0x22, 0xF1, 0x90]), p2_star_timeout=0.5)
    assert resp == final
    assert ecu._last_pending.count == 1


@pytest.mark.asyncio
async def test_pending_then_final_nrc_raises_that_nrc():
    resp, _, ecu = await run_can([(0.0, pending(0x37)), (0.05, bytes([0x7F, 0x37, 0x72]))],
                                 bytes([0x37]), p2_star_timeout=0.5)
    assert isinstance(resp, NrcError)
    assert resp.nrc == 0x72


@pytest.mark.asyncio
async def test_pending_for_other_sid_is_not_an_answer():
    """7F 22 78 while waiting on 0x31 is skipped, not counted, and does not raise."""
    resp, _, ecu = await run_can(
        [(0.0, pending(0x22)), (0.02, bytes([0x71, 0x01, 0x02, 0x00]))],
        bytes([0x31, 0x01, 0x02, 0x00]),
    )
    assert resp == bytes([0x71, 0x01, 0x02, 0x00])
    assert ecu._last_pending.count == 0


@pytest.mark.asyncio
async def test_pending_for_other_sid_does_not_extend_the_deadline():
    """Only other-SID 0x78s and no final answer: the plain P2 timeout fires."""
    script = [(0.0, pending(0x22))] + [(0.04, pending(0x22))] * 10
    resp, elapsed, _ = await run_can(script, bytes([0x31, 0x01, 0x02, 0x00]),
                                     timeout=0.15, p2_star_timeout=2.0)
    assert type(resp) is UdsTimeoutError, resp
    assert resp.timeout == pytest.approx(0.15)
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_p2_star_expiry_after_pending_is_a_plain_timeout():
    """One 0x78, then silence: TimeoutError at P2*client, not the cap error."""
    resp, elapsed, _ = await run_can([(0.0, pending(0x31))], bytes([0x31, 0x01, 0x02, 0x00]),
                                     p2_star_timeout=0.2, max_pending=5.0)
    assert type(resp) is UdsTimeoutError, resp
    assert resp.timeout == pytest.approx(0.2)
    assert 0.15 <= elapsed < 1.0


@pytest.mark.asyncio
async def test_cap_fires_on_endless_pending():
    """An ECU that never stops sending 0x78 hits max_pending."""
    script = [(0.0, pending(0x31))] + [(0.05, pending(0x31))] * 100
    resp, elapsed, _ = await run_can(script, bytes([0x31, 0x01, 0x02, 0x00]),
                                     p2_star_timeout=0.5, max_pending=0.4)
    assert isinstance(resp, ResponsePendingTimeoutError), resp
    assert isinstance(resp, UdsTimeoutError)       # existing handlers still catch it
    assert resp.pending_count >= 5
    assert resp.cap == pytest.approx(0.4)
    assert 0.35 <= elapsed < 1.0
    assert "0x78" in str(resp)


@pytest.mark.asyncio
async def test_cap_bounds_the_wait_for_a_late_frame():
    """The P2* wait after the last 0x78 is clipped at the cap, not run in full."""
    resp, elapsed, _ = await run_can([(0.0, pending(0x31))], bytes([0x31, 0x01, 0x02, 0x00]),
                                     p2_star_timeout=5.0, max_pending=0.3)
    assert isinstance(resp, ResponsePendingTimeoutError), resp
    assert resp.pending_count == 1
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_service_response_exposes_pending():
    """Typed service methods carry the record on the response object."""
    tester_bus, ecu_bus = VirtualBus.pair("pending_service")
    ecu_task = asyncio.create_task(scripted_can_ecu(
        ecu_bus,
        [(0.0, pending(0x31)), (0.03, pending(0x31)),
         (0.03, bytes([0x71, 0x01, 0xFF, 0x00]))],
    ))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF,
                             keepalive=False, p2_star_timeout=0.5) as ecu:
            resp = await ecu.routine_control(0xFF00, RoutineControl.START)
            assert resp.pending.count == 2
            assert len(resp.pending.gaps_ms) == 3
            assert resp.latency_ms >= sum(resp.pending.gaps_ms[1:]) - 1.0
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_raw_exchange_returns_final_after_pending():
    tester_bus, ecu_bus = VirtualBus.pair("pending_exchange")
    ecu_task = asyncio.create_task(scripted_can_ecu(
        ecu_bus, [(0.0, pending(0x31)), (0.03, bytes([0x71, 0x01, 0x02, 0x00]))]))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF,
                             keepalive=False, p2_star_timeout=0.5) as ecu:
            out = await ecu.raw_exchange(bytes([0x31, 0x01, 0x02, 0x00]))
            assert out == bytes([0x71, 0x01, 0x02, 0x00])
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Message-transport (DoIP-shaped) path
# ---------------------------------------------------------------------------

class _MessageBus(CanBus):
    """Whole-PDU transport, as DoipBus presents itself to UdsTester."""

    is_message_transport = True

    def __init__(self, script: Sequence[Step]) -> None:
        self._script = list(script)
        self._rx: "asyncio.Queue[Tuple[int, bytes]]" = asyncio.Queue()
        self.sent: List[bytes] = []
        self._tasks: List[asyncio.Task] = []

    async def send(self, arbitration_id: int, data: bytes) -> None:
        self.sent.append(bytes(data))
        if data[0] != 0x3E:
            self._tasks.append(asyncio.create_task(self._play()))

    async def _play(self) -> None:
        for delay, pdu in self._script:
            await asyncio.sleep(delay)
            await self._rx.put((0x7E8, pdu))

    async def recv(self, timeout: float):
        try:
            return await asyncio.wait_for(self._rx.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()


@pytest.mark.asyncio
async def test_message_transport_pending_then_final():
    bus = _MessageBus([(0.0, pending(0x34)), (0.12, pending(0x34)),
                       (0.12, bytes([0x74, 0x20, 0x0F, 0xFF]))])
    async with UdsTester(bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False,
                         timeout=0.05, p2_star_timeout=0.4) as ecu:
        resp = await ecu.raw_request(bytes([0x34, 0x00, 0x44, 0, 0, 0, 0, 0, 0, 0x10, 0]))
        assert resp == bytes([0x74, 0x20, 0x0F, 0xFF])
        assert ecu._last_pending.count == 2
    await bus.close()


@pytest.mark.asyncio
async def test_message_transport_cap():
    bus = _MessageBus([(0.0, pending(0x31))] + [(0.05, pending(0x31))] * 100)
    async with UdsTester(bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False,
                         p2_star_timeout=0.5, max_pending=0.3) as ecu:
        with pytest.raises(ResponsePendingTimeoutError):
            await ecu.raw_request(bytes([0x31, 0x01, 0x02, 0x00]))
    await bus.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_constructor_rejects_non_positive_values():
    bus, _ = VirtualBus.pair("pending_cfg")
    with pytest.raises(ValueError):
        UdsTester(bus, rx_id=0x7E8, tx_id=0x7DF, p2_star_timeout=0)
    with pytest.raises(ValueError):
        UdsTester(bus, rx_id=0x7E8, tx_id=0x7DF, max_pending=-1)


def test_defaults_are_iso_p2_star_plus_margin():
    bus, _ = VirtualBus.pair("pending_defaults")
    ecu = UdsTester(bus, rx_id=0x7E8, tx_id=0x7DF)
    assert ecu._p2_star_timeout == pytest.approx(5.5)
    assert ecu._max_pending == pytest.approx(120.0)


def test_eds_config_maps_p2_star_server_max(tmp_path):
    cfg = tmp_path / "diagnostics_config.yaml"
    cfg.write_text(
        "metadata:\n  ecu_name: t\n"
        "can:\n  interface: vcan0\n  rx_can_id: '0x7E0'\n  tx_can_id: '0x7E8'\n"
        "timing:\n  p2_server_max_ms: 25\n  p2_star_server_max_ms: 2000\n"
    )
    assert load_eds_config(cfg).p2_star_timeout_ms == 2000
    bus, _ = VirtualBus.pair("pending_from_cfg")
    ecu = UdsTester.from_config(cfg, interface=bus)
    assert ecu._p2_star_timeout == pytest.approx(2.5)


def test_testlab_config_p2_star_key(tmp_path):
    cfg = tmp_path / "testlab_config.yaml"
    cfg.write_text("interface: vcan0\nrx_id: '0x7E8'\ntx_id: '0x7E0'\np2_star_timeout_ms: 3000\n")
    assert load_testlab_config(cfg).p2_star_timeout_ms == 3000
    cfg.write_text("interface: vcan0\nrx_id: '0x7E8'\ntx_id: '0x7E0'\np2_star_timeout_ms: 0\n")
    with pytest.raises(ConfigError):
        load_testlab_config(cfg)
