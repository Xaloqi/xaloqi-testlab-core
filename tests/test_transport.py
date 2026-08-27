"""
tests/test_transport.py — Sub-phase 1A checkpoint: transport layer tests.

All tests use VirtualBus only — no CAN hardware required.
"""
import asyncio
import pytest
from xaloqi.tester.transport.virtual import VirtualBus


@pytest.mark.asyncio
async def test_virtual_send_recv():
    """Send a frame on bus_a, receive on bus_b."""
    bus_a, bus_b = VirtualBus.pair("test_send_recv")
    await bus_a.send(0x7DF, bytes([0x02, 0x10, 0x03, 0, 0, 0, 0, 0]))
    result = await bus_b.recv(timeout=1.0)
    assert result is not None
    arb_id, data = result
    assert arb_id == 0x7DF
    assert data == bytes([0x02, 0x10, 0x03, 0, 0, 0, 0, 0])


@pytest.mark.asyncio
async def test_virtual_timeout():
    """recv on empty bus returns None after timeout."""
    bus_a, bus_b = VirtualBus.pair("test_timeout")
    result = await bus_b.recv(timeout=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_virtual_pair_isolation():
    """Two separate pairs don't cross-contaminate."""
    bus_a1, bus_b1 = VirtualBus.pair("pair1")
    bus_a2, bus_b2 = VirtualBus.pair("pair2")

    await bus_a1.send(0x111, bytes([0xAA]))
    await bus_a2.send(0x222, bytes([0xBB]))

    r1 = await bus_b1.recv(timeout=0.5)
    r2 = await bus_b2.recv(timeout=0.5)

    assert r1 is not None and r1[0] == 0x111
    assert r2 is not None and r2[0] == 0x222

    # bus_b2 should not have received pair1's frame
    r_extra = await bus_b2.recv(timeout=0.05)
    assert r_extra is None


def test_socketcan_is_pro_lazy_export():
    """Without pro installed, SocketCanBus raises the Pro message; with pro
    installed, the lazy re-export resolves it."""
    try:
        import xaloqi_tester_pro  # noqa: F401
        pro_installed = True
    except ImportError:
        pro_installed = False

    if pro_installed:
        from xaloqi.tester.transport import SocketCanBus
        bus = SocketCanBus("vcan0")
        assert bus._channel == "vcan0"
    else:
        import pytest
        with pytest.raises(ImportError, match="Xaloqi TestLab Pro"):
            from xaloqi.tester.transport import SocketCanBus  # noqa: F401
