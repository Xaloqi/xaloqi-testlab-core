# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
tests/test_isotp_flow_control.py — O-13/O-14 checkpoint: ISO-TP multi-frame
TX now honours the ECU's real Flow Control (BlockSize, STmin, WAIT,
OVERFLOW), and SecurityAccess seed length is no longer hardcoded to 4 bytes.

Uses VirtualBus.pair() and hand-written ECU-side coroutines (some driven off
a fixed responses dict, some deliberately scripted to control exactly when
Flow Control frames arrive) — no hardware required. Matches the style of
core/tests/test_isotp.py and core/tests/test_uds_tester.py.
"""
import asyncio
import time

import pytest

from xaloqi.tester import (
    UdsTester, TransportError, FlowControlOverflowError,
)
from xaloqi.tester import TimeoutError as UdsTimeoutError
from xaloqi.tester.transport.virtual import VirtualBus
from xaloqi.tester._isotp import IsoTpEngine, decode_st_min

_engine = IsoTpEngine()

_ECU_ID = 0x7E8   # ECU perspective TX id (tester's rx_id in these tests)
_TESTER_ID = 0x7DF  # ECU perspective RX id (tester's tx_id in these tests)


# ---------------------------------------------------------------------------
# decode_st_min — boundary table (ISO 15765-2 §9.6.5.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0x00, 0.0),      # ms range, lower bound
    (0x01, 0.001),     # ms range, 1ms
    (0x7F, 0.127),     # ms range, upper bound
    (0xF0, 0.127),     # reserved (just below the 100-900us band)
    (0xF1, 0.0001),    # 100-900us band, lower bound (100us)
    (0xF9, 0.0009),    # 100-900us band, upper bound (900us)
    (0xFA, 0.127),     # reserved (just above the 100-900us band)
    (0xFF, 0.127),     # reserved, top of byte range
    (0x80, 0.127),     # reserved, just above the ms range
])
def test_decode_st_min_boundaries(raw, expected):
    assert decode_st_min(raw) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fc(fs: int, bs: int = 0, st_min: int = 0) -> bytes:
    """Build a raw 8-byte Flow Control CAN frame with the given FS."""
    return bytes([0x30 | (fs & 0x0F), bs, st_min, 0x00, 0x00, 0x00, 0x00, 0x00])


async def _recv_ff(ecu_bus: VirtualBus, timeout: float = 1.0):
    """Wait for and return the (total_len, ff_frame) of an incoming First Frame."""
    result = await ecu_bus.recv(timeout=timeout)
    assert result is not None, "expected First Frame, got nothing"
    _, frame = result
    assert (frame[0] >> 4) == 0x1, f"expected First Frame, got PCI 0x{frame[0] >> 4:X}"
    total_len = ((frame[0] & 0x0F) << 8) | frame[1]
    return total_len, frame


async def _generic_ecu(bus: VirtualBus, responses: dict, stop: asyncio.Event):
    """Responses-dict-driven ECU: {request_pdu_bytes: response_pdu_bytes}.

    Handles SF and multi-frame (FF+CF) requests with default (BS=0,
    STmin=0) Flow Control, and SF/multi-frame responses. Runs until `stop`.
    """
    while not stop.is_set():
        result = await bus.recv(timeout=0.05)
        if result is None:
            continue
        _, frame = result
        pci_type = (frame[0] >> 4) & 0x0F

        if pci_type == 0x0:
            length = frame[0] & 0x0F
            req_pdu = bytes(frame[1:1 + length])
        elif pci_type == 0x1:
            total_len = ((frame[0] & 0x0F) << 8) | frame[1]
            data = bytearray(frame[2:])
            await bus.send(_ECU_ID, _engine.flow_control_cts())
            while len(data) < total_len:
                res = await bus.recv(timeout=0.5)
                if res is None:
                    break
                _, cf = res
                if (cf[0] >> 4) == 0x2:
                    data.extend(cf[1:])
            req_pdu = bytes(data[:total_len])
        else:
            continue

        for req, resp_pdu in responses.items():
            if req_pdu == bytes(req):
                frames = _engine.encode(bytes(resp_pdu))
                if len(frames) == 1:
                    await bus.send(_ECU_ID, frames[0])
                else:
                    await bus.send(_ECU_ID, frames[0])
                    await bus.recv(timeout=0.5)  # consume FC CTS
                    for cf in frames[1:]:
                        await bus.send(_ECU_ID, cf)
                break


def _multi_frame_payload(n_cfs: int, sid: int = 0x36) -> bytes:
    """Build a UDS PDU that ISO-TP encodes into exactly `n_cfs` consecutive
    frames after the First Frame (each CF carries 7 bytes; FF carries 6).

    Default SID 0x36 (TransferData) so a run against the real simulator
    (`xaloqi.sim.handle_request`) gets a deterministic single-byte-echo
    positive response regardless of active session — unlike e.g. 0x2E
    WriteDataByIdentifier, which is session-gated.
    """
    total_len = 6 + n_cfs * 7
    payload = bytes([sid]) + bytes((i % 256) for i in range(total_len - 1))
    frames = _engine.encode(payload)
    assert len(frames) - 1 == n_cfs, "test payload sizing is wrong"
    return payload


# ---------------------------------------------------------------------------
# End-to-end: STmin actually paces the TX, via xaloqi.sim.run_on_bus
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stmin_paces_tx_end_to_end():
    """A simulated ECU advertising BS=2/STmin=10ms over VirtualBus: the
    response must be correct AND the request must have taken at least
    n_cfs * STmin seconds — proving STmin was honoured, not just parsed."""
    from xaloqi import sim

    tester_bus, ecu_bus = VirtualBus.pair("stmin_e2e")
    state_box = {"state": sim.EcuState()}
    stop = asyncio.Event()
    task = asyncio.create_task(
        sim.run_on_bus(ecu_bus, state_box, stop, tx_id=_ECU_ID,
                        fc_block_size=2, fc_st_min=0x0A)  # STmin = 10ms
    )

    n_cfs = 5
    payload = _multi_frame_payload(n_cfs)  # SID 0x36 TransferData

    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=2.0) as ecu:
            t0 = time.monotonic()
            resp = await ecu.raw_request(payload)
            elapsed = time.monotonic() - t0
        assert resp[0] == 0x76
        assert elapsed >= n_cfs * 0.010, (
            f"elapsed {elapsed:.4f}s < {n_cfs * 0.010:.4f}s — STmin was not honoured"
        )
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# BlockSize honoured: exactly BS CFs sent, then the tester stops and waits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_block_size_honoured():
    bs_val = 2
    n_cfs = 5
    payload = _multi_frame_payload(n_cfs, sid=0x2E)
    received_cfs = []

    async def controlled_ecu():
        await _recv_ff(ecu_bus)
        await ecu_bus.send(_ECU_ID, _fc(fs=0x0, bs=bs_val, st_min=0x00))

        for _ in range(bs_val):
            res = await ecu_bus.recv(timeout=1.0)
            assert res is not None
            _, cf = res
            assert (cf[0] >> 4) == 0x2
            received_cfs.append(cf)

        # The tester must now STOP and wait — no more CFs until we send a
        # second FC.
        res = await ecu_bus.recv(timeout=0.15)
        assert res is None, "tester sent more than BlockSize frames without waiting for FC"

        await ecu_bus.send(_ECU_ID, _fc(fs=0x0, bs=0, st_min=0x00))
        while len(received_cfs) < n_cfs:
            res = await ecu_bus.recv(timeout=1.0)
            assert res is not None
            _, cf = res
            received_cfs.append(cf)

        resp_frames = _engine.encode(bytes([0x6E, 0x00, 0x00]))
        await ecu_bus.send(_ECU_ID, resp_frames[0])

    tester_bus, ecu_bus = VirtualBus.pair("bs_test")
    ecu_task = asyncio.create_task(controlled_ecu())
    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=2.0) as ecu:
            resp = await ecu.raw_request(payload)
        assert resp[0] == 0x6E
        assert len(received_cfs) == n_cfs
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# WAIT (FS=1): several WAITs, then CTS — transfer still completes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_frames_then_cts_completes():
    n_cfs = 2
    payload = _multi_frame_payload(n_cfs, sid=0x2E)

    async def wait_then_cts_ecu():
        await _recv_ff(ecu_bus)
        for _ in range(3):
            await ecu_bus.send(_ECU_ID, _fc(fs=0x1))  # WAIT
        await ecu_bus.send(_ECU_ID, _fc(fs=0x0))       # CTS

        received = 0
        while received < n_cfs:
            res = await ecu_bus.recv(timeout=1.0)
            assert res is not None
            received += 1

        resp_frames = _engine.encode(bytes([0x6E, 0x00, 0x00]))
        await ecu_bus.send(_ECU_ID, resp_frames[0])

    tester_bus, ecu_bus = VirtualBus.pair("wait_test")
    ecu_task = asyncio.create_task(wait_then_cts_ecu())
    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=2.0) as ecu:
            resp = await ecu.raw_request(payload)
        assert resp[0] == 0x6E
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# WFTmax: too many WAITs gives up with a named error, quickly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wft_max_exceeded_raises_named_error():
    payload = _multi_frame_payload(2, sid=0x2E)

    async def flood_wait_ecu():
        await _recv_ff(ecu_bus)
        for _ in range(17):  # N_WFTmax is 16 — the 17th must trip the guard
            await ecu_bus.send(_ECU_ID, _fc(fs=0x1))

    tester_bus, ecu_bus = VirtualBus.pair("wftmax_test")
    ecu_task = asyncio.create_task(flood_wait_ecu())

    async def run_request():
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=2.0) as ecu:
            await ecu.raw_request(payload)

    try:
        with pytest.raises(TransportError) as exc_info:
            # Guards CI against a hang if the WFTmax bound regresses.
            await asyncio.wait_for(run_request(), timeout=5.0)
        message = str(exc_info.value)
        assert "WFTmax" in message
        assert "17" in message
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# OVERFLOW (FS=2): distinguishable from a plain timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overflow_raises_distinct_from_timeout():
    payload = _multi_frame_payload(2, sid=0x2E)

    async def overflow_ecu():
        await _recv_ff(ecu_bus)
        await ecu_bus.send(_ECU_ID, _fc(fs=0x2))  # OVERFLOW

    tester_bus, ecu_bus = VirtualBus.pair("overflow_test")
    ecu_task = asyncio.create_task(overflow_ecu())
    try:
        with pytest.raises(FlowControlOverflowError) as exc_info:
            async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                                 keepalive=False, timeout=1.0) as ecu:
                await ecu.raw_request(payload)
        assert not isinstance(exc_info.value, UdsTimeoutError), (
            "OVERFLOW must be distinguishable from a plain timeout"
        )
        assert isinstance(exc_info.value, TransportError)  # existing catch sites still work
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Reserved FC type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reserved_fc_type_raises_transport_error():
    payload = _multi_frame_payload(2, sid=0x2E)

    async def reserved_fc_ecu():
        await _recv_ff(ecu_bus)
        await ecu_bus.send(_ECU_ID, _fc(fs=0x3))  # 0x33 — reserved

    tester_bus, ecu_bus = VirtualBus.pair("reserved_fc_test")
    ecu_task = asyncio.create_task(reserved_fc_ecu())
    try:
        with pytest.raises(TransportError):
            async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                                 keepalive=False, timeout=1.0) as ecu:
                await ecu.raw_request(payload)
    finally:
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# REGRESSION: BS=0/STmin=0 VirtualBus path (every current free-tier user)
# must be byte-identical — no sleeps, no extra FC waits, same frame sequence.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bs0_stmin0_regression_unchanged(monkeypatch):
    from xaloqi import sim

    sleep_calls = []
    real_sleep = asyncio.sleep

    async def counting_sleep(delay, *args, **kwargs):
        sleep_calls.append(delay)
        return await real_sleep(0)  # don't actually slow the test down

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)

    tester_bus, ecu_bus = VirtualBus.pair("regression_test")
    state_box = {"state": sim.EcuState()}
    stop = asyncio.Event()
    task = asyncio.create_task(
        sim.run_on_bus(ecu_bus, state_box, stop, tx_id=_ECU_ID)  # defaults: BS=0, STmin=0
    )

    n_cfs = 4
    payload = _multi_frame_payload(n_cfs)  # SID 0x36 TransferData
    expected_frames = _engine.encode(payload)

    sent_frames = []
    real_send = tester_bus.send

    async def spy_send(arb_id, data):
        sent_frames.append((arb_id, bytes(data)))
        await real_send(arb_id, data)

    tester_bus.send = spy_send

    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=2.0) as ecu:
            resp = await ecu.raw_request(payload)
        assert resp[0] == 0x76
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sleep_calls == [], f"asyncio.sleep called {len(sleep_calls)} time(s), expected 0"
    # Exact TX sequence: FF then all CFs back-to-back, nothing else — no
    # extra Flow Control waits interleaved (BS=0 means one FC for the
    # whole message, sent by the ECU and not re-requested by the tester).
    assert sent_frames == [(_TESTER_ID, f) for f in expected_frames]


# ---------------------------------------------------------------------------
# O-14: SecurityAccess seed is taken as whatever length the ECU returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [
    bytes([0xAA, 0xBB]),                                          # 2 bytes
    bytes([0x47, 0xAB, 0x09, 0xBB]),                               # 4 bytes (pins existing sim seed)
    bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]),        # 8 bytes
])
async def test_request_seed_any_length_round_trips(seed):
    from xaloqi.tester._security import derive_key

    expected_key = derive_key(seed, 1, quiet=True)
    responses = {
        (0x27, 0x01): bytes([0x67, 0x01]) + seed,
        tuple(bytes([0x27, 0x02]) + expected_key): (0x67, 0x02),
    }

    tester_bus, ecu_bus = VirtualBus.pair("seed_len_test")
    stop = asyncio.Event()
    ecu_task = asyncio.create_task(_generic_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=1.0) as ecu:
            seed_resp = await ecu.request_seed(level=1)
            assert seed_resp.seed == seed
            assert len(seed_resp.seed) == len(seed)

            # Full security_access() round-trip still succeeds: derive_key
            # takes any seed length and the ECU accepts the resulting key.
            key_resp = await ecu.security_access(level=1)
            assert key_resp.raw[0] == 0x67
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Regressions found by code review on PR #54. Each of these three passed
# before the review and would have shipped a real defect.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keepalive_never_interleaves_into_multiframe_tx():
    """The background keepalive must not inject TesterPresent mid-transfer.

    Before flow control, a multi-frame TX finished in one uninterrupted burst,
    so a 2s keepalive could never land between frames. STmin pacing makes the
    CF loop yield for seconds, so an unsynchronised TesterPresent Single Frame
    on the same tx_id would arrive mid-reassembly and abort the ECU's message.
    """
    from xaloqi import sim

    tester_bus, ecu_bus = VirtualBus.pair("keepalive_race")
    state_box = {"state": sim.EcuState()}
    stop = asyncio.Event()
    # STmin=0x0A (10ms) x 9 CFs keeps the CF loop busy well past the shortened
    # keepalive period below.
    task = asyncio.create_task(
        sim.run_on_bus(ecu_bus, state_box, stop, tx_id=_ECU_ID,
                       fc_block_size=0, fc_st_min=0x0A)
    )

    sent: list = []
    orig_send = tester_bus.send

    async def recording_send(can_id, data):
        sent.append(bytes(data))
        return await orig_send(can_id, data)

    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=True, timeout=2.0) as ecu:
            # Force the keepalive to fire aggressively during the transfer.
            if ecu._keepalive_task is not None:
                ecu._keepalive_task.cancel()

            async def fast_keepalive():
                while True:
                    await asyncio.sleep(0.01)
                    try:
                        await ecu.tester_present(suppress_response=True)
                    except Exception:
                        pass

            ka = asyncio.create_task(fast_keepalive())
            tester_bus.send = recording_send
            try:
                # SID 0x36 so the simulator answers regardless of session
                # (see _multi_frame_payload). 9 CFs at STmin=10ms keeps the
                # CF loop yielding far longer than the keepalive period.
                await ecu._request(_multi_frame_payload(9), timeout=3.0)
            finally:
                ka.cancel()
                tester_bus.send = orig_send
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Walk the recorded TX stream: between a First Frame and the CF that
    # completes it, nothing but Consecutive Frames may appear.
    in_multiframe = False
    remaining = 0
    for frame in sent:
        pci = (frame[0] >> 4) & 0x0F
        if pci == 0x1:
            total = ((frame[0] & 0x0F) << 8) | frame[1]
            remaining = total - 6
            in_multiframe = True
        elif in_multiframe:
            assert pci == 0x2, (
                f"frame {frame.hex()} (PCI 0x{pci:X}) interleaved into an "
                f"in-progress multi-frame TX — keepalive corrupted reassembly"
            )
            remaining -= 7
            if remaining <= 0:
                in_multiframe = False


@pytest.mark.asyncio
async def test_empty_pdu_raises_transport_error_not_index_error():
    """Hoisting `sid = pdu[0]` above the encode guard leaked a bare IndexError."""
    tester_bus, _ecu_bus = VirtualBus.pair("empty_pdu")
    async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                         keepalive=False) as ecu:
        with pytest.raises(TransportError):
            await ecu._request(b"")


@pytest.mark.asyncio
async def test_block_fc_uses_n_bs_not_p2_timeout():
    """A per-block FC slower than P2 but within N_Bs must not time out.

    BlockSize exists to let an ECU pace a transfer; timing the FC out at the
    UDS P2 response timeout (0.15s default) would break exactly the paced
    transfers this feature enables.
    """
    tester_bus, ecu_bus = VirtualBus.pair("n_bs_timeout")
    engine = IsoTpEngine()
    stop = asyncio.Event()

    async def slow_paced_ecu():
        """Sends FC BS=2, then stalls 0.4s (> P2, < N_Bs) before each next FC."""
        received = 0
        expected = None
        while not stop.is_set():
            res = await ecu_bus.recv(timeout=0.05)
            if res is None:
                continue
            _, frame = res
            pci = (frame[0] >> 4) & 0x0F
            if pci == 0x1:
                expected = ((frame[0] & 0x0F) << 8) | frame[1]
                received = 6
                await ecu_bus.send(_ECU_ID, engine.flow_control_cts(block_size=2, st_min=0))
            elif pci == 0x2:
                received += 7
                if received >= expected:
                    await ecu_bus.send(_ECU_ID, engine.encode(bytes([0x6E, 0xF1, 0x90]))[0])
                    return
                if (received - 6) % 14 == 0:
                    await asyncio.sleep(0.4)   # > P2 (0.15s), < N_Bs (1.0s)
                    await ecu_bus.send(_ECU_ID, engine.flow_control_cts(block_size=2, st_min=0))

    ecu_task = asyncio.create_task(slow_paced_ecu())
    try:
        async with UdsTester(tester_bus, rx_id=_ECU_ID, tx_id=_TESTER_ID,
                             keepalive=False, timeout=0.15) as ecu:
            resp = await ecu.write_did(0xF190, b"B" * 30)
            assert resp.raw[0] == 0x6E
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass
