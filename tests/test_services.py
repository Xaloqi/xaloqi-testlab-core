"""
tests/test_services.py — Sub-phase 1F checkpoint: service module tests.

Uses VirtualBus + multi-frame-capable ECU simulator.
No hardware required.
"""
import asyncio
import pytest
from xaloqi.tester import (
    UdsTester, Session, ResetType, RoutineControl, DtcStatusMask,
    NrcError, DidResponse, DtcResponse
)
from xaloqi.tester.transport.virtual import VirtualBus
from xaloqi.tester._isotp import IsoTpEngine

_engine = IsoTpEngine()


async def ecu_sim(bus: VirtualBus, responses: dict, stop: asyncio.Event):
    """
    ECU simulator: handles SF requests, sends SF or multi-frame responses.
    Supports multi-frame TX (sends FF, waits for FC, then sends CFs).
    """
    while not stop.is_set():
        result = await bus.recv(timeout=0.05)
        if result is None:
            continue
        _, frame = result
        pci_type = (frame[0] >> 4) & 0x0F
        if pci_type != 0x0:
            continue  # only handle SF requests
        length = frame[0] & 0x0F
        req = bytes(frame[1:1+length])

        for k, v in responses.items():
            if req == bytes(k):
                resp_pdu = bytes(v)
                frames = _engine.encode(resp_pdu)
                if len(frames) == 1:
                    await bus.send(0x7E8, frames[0])
                else:
                    # Multi-frame: FF → wait FC → CFs
                    await bus.send(0x7E8, frames[0])
                    await bus.recv(timeout=0.5)  # consume FC CTS
                    for cf in frames[1:]:
                        await bus.send(0x7E8, cf)
                break


async def run_with_ecu(responses: dict, test_fn):
    tester_bus, ecu_bus = VirtualBus.pair("svc_test")
    stop = asyncio.Event()
    task = asyncio.create_task(ecu_sim(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            await test_fn(ecu)
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Session (0x10)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_default():
    async def fn(ecu):
        resp = await ecu.session(Session.DEFAULT)
        assert resp.raw[0] == 0x50
    await run_with_ecu({(0x10, 0x01): (0x50, 0x01)}, fn)


@pytest.mark.asyncio
async def test_session_nrc():
    async def fn(ecu):
        with pytest.raises(NrcError) as exc:
            await ecu.session(Session.PROGRAMMING)
        assert exc.value.nrc == 0x22
    await run_with_ecu({(0x10, 0x02): (0x7F, 0x10, 0x22)}, fn)


# ---------------------------------------------------------------------------
# ECUReset (0x11)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reset_soft():
    async def fn(ecu):
        resp = await ecu.reset(ResetType.SOFT)
        assert resp.raw[0] == 0x51
    await run_with_ecu({(0x11, 0x03): (0x51, 0x03)}, fn)


# ---------------------------------------------------------------------------
# ReadDataByIdentifier (0x22) — multi-frame response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_did():
    did_id = 0xF190
    vin_data = b"1HGBH41JXMN109186"  # 17 bytes → 20-byte PDU → multi-frame
    response_pdu = tuple(bytes([0x62, 0xF1, 0x90]) + vin_data)

    async def fn(ecu):
        resp = await ecu.read_did(did_id)
        assert isinstance(resp, DidResponse)
        assert resp.did_id == did_id
        assert resp.data == vin_data

    await run_with_ecu({(0x22, 0xF1, 0x90): response_pdu}, fn)


@pytest.mark.asyncio
async def test_read_did_nrc_security():
    async def fn(ecu):
        with pytest.raises(NrcError) as exc:
            await ecu.read_did(0xF187)
        assert exc.value.nrc == 0x33
        assert "securityAccessDenied" in str(exc.value)
    await run_with_ecu({(0x22, 0xF1, 0x87): (0x7F, 0x22, 0x33)}, fn)


@pytest.mark.asyncio
async def test_read_did_nrc_session():
    async def fn(ecu):
        with pytest.raises(NrcError) as exc:
            await ecu.read_did(0xF187)
        assert exc.value.nrc == 0x7F
    await run_with_ecu({(0x22, 0xF1, 0x87): (0x7F, 0x22, 0x7F)}, fn)


# ---------------------------------------------------------------------------
# WriteDataByIdentifier (0x2E)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_did():
    data = b"123"  # short data — keeps request as single frame
    wr_key = bytes([0x2E, 0xF1, 0x90]) + data

    async def fn(ecu):
        resp = await ecu.write_did(0xF190, data)
        assert resp.raw[0] == 0x6E

    await run_with_ecu({wr_key: (0x6E, 0xF1, 0x90)}, fn)


@pytest.mark.asyncio
async def test_write_did_nrc_conditions():
    data = b"123"
    wr_key = bytes([0x2E, 0xF1, 0x90]) + data

    async def fn(ecu):
        with pytest.raises(NrcError) as exc:
            await ecu.write_did(0xF190, data)
        assert exc.value.nrc == 0x22

    await run_with_ecu({wr_key: (0x7F, 0x2E, 0x22)}, fn)


# ---------------------------------------------------------------------------
# ReadDTCInformation (0x19)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_dtcs_empty():
    async def fn(ecu):
        resp = await ecu.read_dtcs()
        assert isinstance(resp, DtcResponse)
        assert resp.dtcs == []
    await run_with_ecu({(0x19, 0x02, 0xFF): (0x59, 0x02, 0xFF)}, fn)


@pytest.mark.asyncio
async def test_read_dtcs_one_dtc():
    resp_pdu = (0x59, 0x02, 0xFF, 0xC0, 0x01, 0x00, 0x08)

    async def fn(ecu):
        resp = await ecu.read_dtcs()
        assert len(resp.dtcs) == 1
        assert resp.dtcs[0].code == 0xC00100
        assert resp.dtcs[0].status_mask == 0x08

    await run_with_ecu({(0x19, 0x02, 0xFF): resp_pdu}, fn)


# ---------------------------------------------------------------------------
# ClearDiagnosticInformation (0x14)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_dtcs():
    async def fn(ecu):
        resp = await ecu.clear_dtcs()
        assert resp.raw[0] == 0x54
    await run_with_ecu({(0x14, 0xFF, 0xFF, 0xFF): (0x54,)}, fn)


# ---------------------------------------------------------------------------
# SecurityAccess (0x27)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_security_access():
    from xaloqi.tester._security import derive_key
    seed = bytes([0x47, 0xAB, 0x09, 0xBB])
    expected_key = derive_key(seed, 1)
    seed_resp = bytes([0x67, 0x01]) + seed + bytes([0x00, 0x00])
    key_req = bytes([0x27, 0x02]) + expected_key

    async def fn(ecu):
        resp = await ecu.security_access(level=1)
        assert resp.raw[0] == 0x67

    await run_with_ecu(
        {(0x27, 0x01): tuple(seed_resp), tuple(key_req): (0x67, 0x02)},
        fn
    )


@pytest.mark.asyncio
async def test_security_access_nrc_invalid_key():
    from xaloqi.tester._security import derive_key
    seed = bytes([0x11, 0x22, 0x33, 0x44])
    key = derive_key(seed, 1)
    seed_resp = bytes([0x67, 0x01]) + seed + bytes([0x00, 0x00])
    key_req = bytes([0x27, 0x02]) + key

    async def fn(ecu):
        with pytest.raises(NrcError) as exc:
            await ecu.security_access(level=1)
        assert exc.value.nrc == 0x35
        assert "invalidKey" in str(exc.value)

    await run_with_ecu(
        {(0x27, 0x01): tuple(seed_resp), tuple(key_req): (0x7F, 0x27, 0x35)},
        fn
    )


# ---------------------------------------------------------------------------
# TesterPresent (0x3E)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tester_present():
    async def fn(ecu):
        resp = await ecu.tester_present(suppress_response=False)
        assert resp.raw[0] == 0x7E
    await run_with_ecu({(0x3E, 0x00): (0x7E, 0x00)}, fn)


@pytest.mark.asyncio
async def test_tester_present_suppress():
    async def fn(ecu):
        resp = await ecu.tester_present(suppress_response=True)
        assert resp.raw == b""
    await run_with_ecu({}, fn)
