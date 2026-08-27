"""
tests/test_isotp.py — Sub-phase 1B checkpoint: ISO-TP engine tests.

All tests use IsoTpEngine directly — no CAN bus needed.
"""
import pytest
from xaloqi.tester._isotp import IsoTpEngine
from xaloqi.tester.exceptions import TransportError


@pytest.fixture
def engine():
    return IsoTpEngine()


# ── Encode tests ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7])
def test_encode_single_frame(engine, n):
    payload = bytes(range(n))
    frames = engine.encode(payload)
    assert len(frames) == 1
    frame = frames[0]
    assert len(frame) == 8
    # PCI byte: 0x0N
    assert frame[0] == n
    # Data bytes
    assert frame[1:1+n] == payload
    # Rest is padding
    assert frame[1+n:] == bytes([0x00] * (7 - n))


def test_encode_multi_frame_8bytes(engine):
    """8-byte payload: FF + 1 CF."""
    payload = bytes(range(8))
    frames = engine.encode(payload)
    assert len(frames) == 2

    ff = frames[0]
    assert len(ff) == 8
    assert (ff[0] >> 4) == 0x1  # First Frame PCI type
    total_len = ((ff[0] & 0x0F) << 8) | ff[1]
    assert total_len == 8
    assert ff[2:8] == payload[:6]

    cf = frames[1]
    assert len(cf) == 8
    assert (cf[0] >> 4) == 0x2  # Consecutive Frame
    assert (cf[0] & 0x0F) == 1  # SN=1
    assert cf[1:3] == payload[6:8]


def test_encode_multi_frame_large(engine):
    """100-byte payload: correct CF count."""
    payload = bytes(range(100))
    frames = engine.encode(payload)
    # FF carries 6 bytes, each CF carries 7 bytes
    # remaining = 100 - 6 = 94, ceil(94/7) = 14 CFs
    assert len(frames) == 1 + 14  # FF + 14 CFs
    # Verify all frames are 8 bytes
    for f in frames:
        assert len(f) == 8


def test_encode_too_large(engine):
    """4096 bytes raises TransportError."""
    with pytest.raises(TransportError):
        engine.encode(bytes(4096))


def test_encode_4095_ok(engine):
    """4095 bytes is the maximum — should NOT raise."""
    frames = engine.encode(bytes(4095))
    assert len(frames) > 1


# ── Decode tests ─────────────────────────────────────────────────────────────

def test_decode_single_frame(engine):
    payload = bytes([0xAA, 0xBB, 0xCC])
    frames = engine.encode(payload)
    result = engine.decode(frames)
    assert result == payload


def test_decode_multi_frame(engine):
    payload = bytes(range(50))
    frames = engine.encode(payload)
    result = engine.decode(frames)
    assert result == payload


def test_decode_wrong_sequence_number(engine):
    """Tampered SN raises TransportError."""
    payload = bytes(range(20))
    frames = engine.encode(payload)
    # Corrupt SN of second CF (frames[2])
    bad_cf = bytes([0x23]) + frames[2][1:]  # SN should be 2, put 3
    tampered = frames[:2] + [bad_cf] + frames[3:]
    with pytest.raises(TransportError):
        engine.decode(tampered)


def test_fc_cts_format(engine):
    """Flow Control CTS frame has correct bytes."""
    fc = engine.flow_control_cts()
    assert len(fc) == 8
    assert fc[0] == 0x30   # PCI type = FC, sub-type = CTS
    assert fc[1] == 0x00   # BS = 0 (no block limit)
    assert fc[2] == 0x00   # STmin = 0


def test_sn_wraparound(engine):
    """SN 14→15→0→1 wraps correctly for very large payloads."""
    # Need > 15 CFs: FF=6 bytes + 15*7=105 bytes = 111 bytes minimum for 16+ frames
    # 6 + 16*7 = 118 bytes → 17 frames total (FF + 16 CFs, SN wraps from 15 to 0)
    payload = bytes(range(118))
    frames = engine.encode(payload)
    # Verify SNs: 1,2,3,...,15,0,1,...
    sns = [(f[0] & 0x0F) for f in frames[1:]]  # skip FF
    expected_sns = [(i % 16) if i > 0 else 1 for i in range(len(sns))]
    # Actually: SN starts at 1, increments mod 16 (0x0F=15 then wraps to 0)
    expected = []
    sn = 1
    for _ in range(len(sns)):
        expected.append(sn)
        sn = (sn + 1) & 0x0F
    assert sns == expected

    # Also verify decode round-trips correctly
    result = engine.decode(frames)
    assert result == payload
