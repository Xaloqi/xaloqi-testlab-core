"""
tests/test_uds_tester.py — Sub-phase 1E checkpoint: UdsTester integration tests.

Uses VirtualBus.pair() and minimal ECU simulator coroutines.
No hardware required.
"""
import asyncio
import pytest
from xaloqi.tester import (
    UdsTester, Session, NrcError, TransportError,
    FaultDetectionCounterRecord, FaultDetectionCounterResponse,
    DtcRecord, DtcResponse,
    MemoryResponse,
)
from xaloqi.tester import TimeoutError as UdsTimeoutError
from xaloqi.tester.transport.virtual import VirtualBus
from xaloqi.tester._isotp import IsoTpEngine


# ---------------------------------------------------------------------------
# Minimal ECU simulator helper
# ---------------------------------------------------------------------------

_engine = IsoTpEngine()


def make_sf_response(payload: bytes) -> bytes:
    """Build an ISO-TP Single Frame CAN payload from a UDS PDU."""
    frames = _engine.encode(payload)
    return frames[0]


async def simple_ecu(ecu_bus: VirtualBus, responses: dict, stop_event: asyncio.Event):
    """
    Minimal ECU: maps request PDU bytes → response PDU bytes.
    Handles SF and multi-frame (FF+CF) requests; sends SF or multi-frame responses.
    responses: {request_pdu_bytes: response_pdu_bytes}
    """
    while not stop_event.is_set():
        result = await ecu_bus.recv(timeout=0.05)
        if result is None:
            continue

        arb_id, frame_data = result
        pci_type = (frame_data[0] >> 4) & 0x0F

        if pci_type == 0x0:   # Single Frame
            length = frame_data[0] & 0x0F
            req_pdu = bytes(frame_data[1:1 + length])

        elif pci_type == 0x1:  # First Frame — multi-frame request
            total_len = ((frame_data[0] & 0x0F) << 8) | frame_data[1]
            data_bytes = bytearray(frame_data[2:])
            await ecu_bus.send(0x7E8, _engine.flow_control_cts())
            while len(data_bytes) < total_len:
                res = await ecu_bus.recv(timeout=0.5)
                if res is None:
                    break
                _, cf = res
                if (cf[0] >> 4) == 0x2:
                    data_bytes.extend(cf[1:])
            req_pdu = bytes(data_bytes[:total_len])

        else:
            continue

        for req, resp_pdu in responses.items():
            if req_pdu == bytes(req):
                frames = _engine.encode(bytes(resp_pdu))
                if len(frames) == 1:
                    await ecu_bus.send(0x7E8, frames[0])
                else:
                    await ecu_bus.send(0x7E8, frames[0])      # FF
                    await ecu_bus.recv(timeout=0.5)            # consume FC
                    for cf in frames[1:]:
                        await ecu_bus.send(0x7E8, cf)          # CFs
                break


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_extended():
    """Send 0x10 0x03 (ExtendedSession), ECU responds 0x50 0x03."""
    tester_bus, ecu_bus = VirtualBus.pair("test_session")
    stop = asyncio.Event()

    responses = {
        bytes([0x10, 0x03]): bytes([0x50, 0x03]),
    }
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))

    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.session(Session.EXTENDED)
            assert resp.raw[0] == 0x50
            assert resp.raw[1] == 0x03
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_nrc_raises():
    """ECU responds 0x7F 0x22 0x31 → NrcError(nrc=0x31)."""
    tester_bus, ecu_bus = VirtualBus.pair("test_nrc")
    stop = asyncio.Event()

    responses = {
        bytes([0x22, 0x01, 0x00]): bytes([0x7F, 0x22, 0x31]),
    }
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))

    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError) as exc_info:
                await ecu.read_did(0x0100)
            assert exc_info.value.nrc == 0x31
            assert exc_info.value.sid == 0x22
            assert "requestOutOfRange" in str(exc_info.value)
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_timeout_raises():
    """No ECU response → TimeoutError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_timeout")

    async with UdsTester(
        tester_bus, rx_id=0x7E8, tx_id=0x7DF,
        keepalive=False, timeout=0.1
    ) as ecu:
        with pytest.raises(UdsTimeoutError) as exc_info:
            await ecu.session(Session.EXTENDED)
        assert exc_info.value.sid == 0x10


@pytest.mark.asyncio
async def test_keepalive_sends_tester_present():
    """keepalive=True → 0x3E 0x80 sent within 2.5s."""
    tester_bus, ecu_bus = VirtualBus.pair("test_keepalive")

    async def wait_for_tp():
        """Wait up to 3 seconds for a TesterPresent suppress frame."""
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            result = await ecu_bus.recv(timeout=0.2)
            if result is None:
                continue
            _, frame_data = result
            pci = frame_data[0]
            if (pci >> 4) == 0x0:
                length = pci & 0x0F
                pdu = frame_data[1:1+length]
                if len(pdu) >= 2 and pdu[0] == 0x3E and pdu[1] == 0x80:
                    return True
        return False

    async with UdsTester(
        tester_bus, rx_id=0x7E8, tx_id=0x7DF,
        keepalive=True, timeout=0.1
    ) as ecu:
        found = await wait_for_tp()
        assert found, "TesterPresent suppress frame not sent within 3 seconds"


@pytest.mark.asyncio
async def test_sync_wrapper_session():
    """SyncUdsTester context manager works."""
    tester_bus, ecu_bus = VirtualBus.pair("test_sync")
    stop = asyncio.Event()

    responses = {
        bytes([0x10, 0x01]): bytes([0x50, 0x01]),
    }

    # Run ECU simulator in background thread via asyncio
    async def run_sync_test():
        ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
        try:
            # SyncUdsTester creates its own event loop — test it via async wrapper here
            # to avoid nested event loop issues in pytest
            async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
                resp = await ecu.session(Session.DEFAULT)
                assert resp.raw[0] == 0x50
                assert resp.raw[1] == 0x01
        finally:
            stop.set()
            ecu_task.cancel()
            try:
                await ecu_task
            except asyncio.CancelledError:
                pass

    await run_sync_test()


# ---------------------------------------------------------------------------
# 0x19/0x0B reportDTCFaultDetectionCounter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_dtcs_fault_counter_empty():
    """0x0B — ECU returns no records: response [0x59, 0x0B]."""
    tester_bus, ecu_bus = VirtualBus.pair("test_fdc_empty")
    stop = asyncio.Event()

    responses = {
        bytes([0x19, 0x0B]): bytes([0x59, 0x0B]),
    }
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_dtcs_fault_counter()
            assert isinstance(resp, FaultDetectionCounterResponse)
            assert resp.records == []
            assert resp.raw[0] == 0x59
            assert resp.raw[1] == 0x0B
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_dtcs_fault_counter_two_records():
    """0x0B — ECU returns two pre-confirmed DTCs with their counter values.

    Response: [0x59, 0x0B, 0xC0, 0x01, 0x00, 0x1E, 0xC0, 0x02, 0x00, 0x05]
    No DTCStatusAvailabilityMask byte — records start at byte 2.
    """
    tester_bus, ecu_bus = VirtualBus.pair("test_fdc_two")
    stop = asyncio.Event()

    response_pdu = bytes([
        0x59, 0x0B,
        0xC0, 0x01, 0x00, 0x1E,   # DTC 0xC00100, counter=30
        0xC0, 0x02, 0x00, 0x05,   # DTC 0xC00200, counter=5
    ])
    responses = {bytes([0x19, 0x0B]): response_pdu}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_dtcs_fault_counter()
            assert len(resp.records) == 2
            assert resp.records[0] == FaultDetectionCounterRecord(code=0xC00100, counter=0x1E)
            assert resp.records[1] == FaultDetectionCounterRecord(code=0xC00200, counter=0x05)
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_dtcs_fault_counter_nrc():
    """0x0B — NRC 0x22 (conditionsNotCorrect) raises NrcError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_fdc_nrc")
    stop = asyncio.Event()

    responses = {bytes([0x19, 0x0B]): bytes([0x7F, 0x19, 0x22])}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError) as exc_info:
                await ecu.read_dtcs_fault_counter()
            assert exc_info.value.nrc == 0x22
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 0x19/0x19 reportDTCWithPermanentStatus
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_dtcs_permanent_empty():
    """0x19 — ECU returns no permanent DTCs: [0x59, 0x19, 0xFF]."""
    tester_bus, ecu_bus = VirtualBus.pair("test_perm_empty")
    stop = asyncio.Event()

    responses = {
        bytes([0x19, 0x19]): bytes([0x59, 0x19, 0xFF]),
    }
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_dtcs_permanent()
            assert isinstance(resp, DtcResponse)
            assert resp.dtcs == []
            assert resp.raw[0] == 0x59
            assert resp.raw[1] == 0x19
            assert resp.raw[2] == 0xFF   # availability mask
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_dtcs_permanent_one_record():
    """0x19 — ECU returns one permanent DTC with its status byte.

    Response: [0x59, 0x19, 0xFF, 0xC0, 0x03, 0x00, 0x09]
    Availability mask at byte 2; records start at byte 3 (same layout as 0x02).
    """
    tester_bus, ecu_bus = VirtualBus.pair("test_perm_one")
    stop = asyncio.Event()

    response_pdu = bytes([
        0x59, 0x19, 0xFF,
        0xC0, 0x03, 0x00, 0x09,   # DTC 0xC00300, status=0x09 (testFailed|confirmed)
    ])
    responses = {bytes([0x19, 0x19]): response_pdu}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_dtcs_permanent()
            assert len(resp.dtcs) == 1
            dtc = resp.dtcs[0]
            assert dtc.code == 0xC00300
            assert dtc.status_mask == 0x09
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_dtcs_permanent_nrc():
    """0x19 — NRC 0x12 (subFunctionNotSupported) raises NrcError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_perm_nrc")
    stop = asyncio.Event()

    responses = {bytes([0x19, 0x19]): bytes([0x7F, 0x19, 0x12])}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError) as exc_info:
                await ecu.read_dtcs_permanent()
            assert exc_info.value.nrc == 0x12
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 0x23 ReadMemoryByAddress
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_memory_by_address_basic():
    """0x23 — ECU returns 0x63 + 4 data bytes; MemoryResponse.data contains them."""
    tester_bus, ecu_bus = VirtualBus.pair("test_rmba_basic")
    stop = asyncio.Event()

    # Request: [0x23, ALFID=0x44, addr 0x08020000 (4B), size 4 (4B)]
    req = bytes([0x23, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04])
    # Response: [0x63, 0xDE, 0xAD, 0xBE, 0xEF]
    resp_pdu = bytes([0x63, 0xDE, 0xAD, 0xBE, 0xEF])
    responses = {req: resp_pdu}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_memory_by_address(0x08020000, 4)
            assert isinstance(resp, MemoryResponse)
            assert resp.address == 0x08020000
            assert resp.data == bytes([0xDE, 0xAD, 0xBE, 0xEF])
            assert resp.raw[0] == 0x63
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_memory_by_address_custom_alfid():
    """0x23 — address_len=2, size_len=1 → ALFID=0x12; PDU built correctly."""
    tester_bus, ecu_bus = VirtualBus.pair("test_rmba_alfid")
    stop = asyncio.Event()

    # address=0x1000 (2B), size=2 (1B) → ALFID=(1<<4)|2=0x12
    req      = bytes([0x23, 0x12, 0x10, 0x00, 0x02])
    resp_pdu = bytes([0x63, 0xAB, 0xCD])
    responses = {req: resp_pdu}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_memory_by_address(0x1000, 2, address_len=2, size_len=1)
            assert resp.data == bytes([0xAB, 0xCD])
            assert resp.address == 0x1000
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_memory_by_address_as_int():
    """MemoryResponse.as_int() returns data interpreted as big-endian integer."""
    tester_bus, ecu_bus = VirtualBus.pair("test_rmba_as_int")
    stop = asyncio.Event()

    req      = bytes([0x23, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04])
    resp_pdu = bytes([0x63, 0x00, 0x00, 0x01, 0x00])
    responses = {req: resp_pdu}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.read_memory_by_address(0x08020000, 4)
            assert resp.as_int() == 256
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_read_memory_by_address_nrc():
    """0x23 — NRC 0x33 (securityAccessDenied) raises NrcError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_rmba_nrc")
    stop = asyncio.Event()

    req = bytes([0x23, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04])
    responses = {req: bytes([0x7F, 0x23, 0x33])}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError) as exc_info:
                await ecu.read_memory_by_address(0x08020000, 4)
            assert exc_info.value.nrc == 0x33
            assert exc_info.value.sid == 0x23
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 0x3D WriteMemoryByAddress
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_memory_by_address_basic():
    """0x3D — ECU echoes 0x7D + ALFID + addr + size; UdsResponse returned."""
    tester_bus, ecu_bus = VirtualBus.pair("test_wmba_basic")
    stop = asyncio.Event()

    # Request: [0x3D, 0x44, addr(4B), size(4B)=2, data 0xCA, 0xFE]
    req      = bytes([0x3D, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0xCA, 0xFE])
    # Response echoes: [0x7D, ALFID, addr(4B), size(4B)]
    resp_pdu = bytes([0x7D, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02])
    responses = {req: resp_pdu}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.write_memory_by_address(0x08020000, bytes([0xCA, 0xFE]))
            assert resp.raw[0] == 0x7D
            assert resp.raw[1] == 0x44           # ALFID echoed
            assert resp.raw[2:6] == bytes([0x08, 0x02, 0x00, 0x00])  # address echoed
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_write_memory_by_address_custom_alfid():
    """0x3D — address_len=2, size_len=1 → ALFID=0x12; size derived from data."""
    tester_bus, ecu_bus = VirtualBus.pair("test_wmba_alfid")
    stop = asyncio.Event()

    # address=0x2000 (2B), data=3 bytes → size=3 (1B), ALFID=0x12
    req      = bytes([0x3D, 0x12, 0x20, 0x00, 0x03, 0x01, 0x02, 0x03])
    resp_pdu = bytes([0x7D, 0x12, 0x20, 0x00, 0x03])
    responses = {req: resp_pdu}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.write_memory_by_address(
                0x2000, bytes([0x01, 0x02, 0x03]), address_len=2, size_len=1,
            )
            assert resp.raw[0] == 0x7D
            assert resp.raw[1] == 0x12
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_write_memory_by_address_nrc():
    """0x3D — NRC 0x7F (serviceNotSupportedInActiveSession) raises NrcError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_wmba_nrc")
    stop = asyncio.Event()

    req = bytes([0x3D, 0x44, 0x08, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0xCA, 0xFE])
    responses = {req: bytes([0x7F, 0x3D, 0x7F])}

    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError) as exc_info:
                await ecu.write_memory_by_address(0x08020000, bytes([0xCA, 0xFE]))
            assert exc_info.value.nrc == 0x7F
            assert exc_info.value.sid == 0x3D
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 0x35 RequestUpload (TestLab#25) — mirror of RequestDownload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_upload_happy_path():
    """0x35 with 4-byte addr+size → ECU responds 0x75; method returns it."""
    tester_bus, ecu_bus = VirtualBus.pair("test_upload_ok")
    stop = asyncio.Event()
    req = bytes([0x35, 0x00, 0x44]) + (0x08000000).to_bytes(4, "big") + (0x1000).to_bytes(4, "big")
    responses = {req: bytes([0x75, 0x20, 0x0F, 0xFF])}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            resp = await ecu.request_upload(0x08000000, 0x1000)
            assert resp.raw[0] == 0x75
            assert resp.raw[1] == 0x20  # lengthFormatIdentifier echoed
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_request_upload_wrong_positive_sid_raises():
    """A positive response with the wrong SID (not 0x75) is a TransportError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_upload_badsid")
    stop = asyncio.Event()
    req = bytes([0x35, 0x00, 0x44]) + (0).to_bytes(4, "big") + (0x10).to_bytes(4, "big")
    responses = {req: bytes([0x74, 0x20, 0x0F, 0xFF])}  # 0x74 = download ack, wrong service
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(TransportError):
                await ecu.request_upload(0x00000000, 0x10)
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_request_upload_nrc_raises():
    """NRC 0x7F (not in programming session) surfaces as NrcError."""
    tester_bus, ecu_bus = VirtualBus.pair("test_upload_nrc")
    stop = asyncio.Event()
    req = bytes([0x35, 0x00, 0x44]) + (0).to_bytes(4, "big") + (0x10).to_bytes(4, "big")
    responses = {req: bytes([0x7F, 0x35, 0x7F])}
    ecu_task = asyncio.create_task(simple_ecu(ecu_bus, responses, stop))
    try:
        async with UdsTester(tester_bus, rx_id=0x7E8, tx_id=0x7DF, keepalive=False) as ecu:
            with pytest.raises(NrcError):
                await ecu.request_upload(0x00000000, 0x10)
    finally:
        stop.set()
        ecu_task.cancel()
        try:
            await ecu_task
        except asyncio.CancelledError:
            pass
