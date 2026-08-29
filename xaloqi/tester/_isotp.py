# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
_isotp.py — ISO 15765-2 framing engine.

TX path: payload bytes → sequence of 8-byte CAN frame payloads
RX path: sequence of 8-byte CAN frame payloads → payload bytes

Reference: harness/uds_tester.py isotp_encode_* / isotp_decode functions.
"""
from typing import List

from xaloqi.tester.exceptions import TransportError

_PAD_BYTE = 0x00  # Pad to 8 bytes with 0x00 (spec uses 0x00, not 0xCC)

# ISO 15765-2 N_WFTmax: maximum number of consecutive Flow Control WAIT
# (FS=1) frames a tester will tolerate before giving up on a broken or
# malicious ECU that would otherwise hold a multi-frame TX open forever.
_WFT_MAX = 16

# ISO 15765-2 N_Bs: time the sender waits for a Flow Control frame. This is a
# transport timer and is deliberately NOT the UDS P2 response timeout (default
# 0.15s) — a spec-compliant ECU advertising BlockSize may legitimately take
# longer than P2 to turn around each block's FC, and timing that out mid-message
# would break exactly the paced transfers BlockSize support exists to enable.
_N_BS_TIMEOUT = 1.0


def decode_st_min(raw: int) -> float:
    """ISO 15765-2 §9.6.5.5 STmin encoding → seconds.

    - 0x00-0x7F: raw milliseconds (0-127ms).
    - 0xF1-0xF9: 100-900 microseconds in 100us steps.
    - Everything else (0x80-0xF0, 0xFA-0xFF) is reserved; ISO 15765-2 says
      to treat a reserved value as the safe upper bound 0x7F (127ms).
    """
    if 0x00 <= raw <= 0x7F:
        return raw / 1000.0
    if 0xF1 <= raw <= 0xF9:
        return (raw - 0xF0) / 10000.0
    return 0.127


class IsoTpEngine:
    """
    ISO 15765-2 framing engine.

    Handles:
    - Single Frame (PCI type 0x0): payloads 1–7 bytes
    - First Frame  (PCI type 0x1): start of multi-frame sequence
    - Consecutive Frame (PCI type 0x2): continuation, SN=1..F wraps to 0
    - Flow Control CTS (PCI type 0x3): sent after receiving FF
    """

    def encode(self, payload: bytes) -> List[bytes]:
        """
        Encode payload into a list of 8-byte CAN frame data payloads.

        Args:
            payload: UDS PDU (1–4095 bytes).

        Returns:
            List of exactly-8-byte frame payloads. Length 1 for SF, N+1 for FF+CFs.

        Raises:
            TransportError: If payload exceeds 4095 bytes.
        """
        n = len(payload)
        if n == 0:
            raise TransportError("ISO-TP payload must be at least 1 byte")
        if n > 4095:
            raise TransportError(
                f"ISO-TP payload too large: {n} bytes > 4095 byte maximum"
            )

        if n <= 7:
            # Single Frame: [0x0N, data..., padding]
            frame = bytes([n]) + payload
            frame += bytes([_PAD_BYTE] * (8 - len(frame)))
            return [frame]

        # Multi-frame
        # First Frame: [0x1H, 0xLL, data[0:6]]
        high_nibble = (n >> 8) & 0x0F
        low_byte = n & 0xFF
        ff = bytes([0x10 | high_nibble, low_byte]) + payload[:6]
        frames = [ff]

        # Consecutive Frames
        remaining = payload[6:]
        sn = 1
        while remaining:
            chunk = remaining[:7]
            remaining = remaining[7:]
            cf = bytes([0x20 | (sn & 0x0F)]) + chunk
            cf += bytes([_PAD_BYTE] * (8 - len(cf)))
            frames.append(cf)
            sn = (sn + 1) & 0x0F  # wraps: 0xF → 0x0

        return frames

    def flow_control_cts(self, block_size: int = 0, st_min: int = 0) -> bytes:
        """
        Return a Flow Control CTS frame.

        Args:
            block_size: BS to advertise (0 = no block limit — send all
                remaining CFs before expecting another FC). Default matches
                previous fixed behaviour.
            st_min: Raw STmin byte to advertise, ISO 15765-2 §9.6.5.5 encoded
                (0 = no minimum separation time). Default matches previous
                fixed behaviour.
        """
        return bytes([0x30, block_size & 0xFF, st_min & 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00])

    def decode(self, frames: List[bytes]) -> bytes:
        """
        Reassemble a list of CAN frame data payloads into a UDS PDU.

        Args:
            frames: List of received 8-byte CAN frame data.
                    Single frame: list of 1. Multi-frame: [FF, CF1, CF2, ...].

        Returns:
            Reassembled payload bytes.

        Raises:
            TransportError: On PCI sequence error, wrong SN, or truncated data.
        """
        if not frames:
            raise TransportError("ISO-TP decode: empty frame list")

        first = frames[0]
        pci_type = (first[0] >> 4) & 0x0F

        if pci_type == 0x0:
            # Single Frame
            length = first[0] & 0x0F
            if length == 0 or length > 7:
                raise TransportError(
                    f"ISO-TP SF: invalid length nibble 0x{length:X}"
                )
            if len(first) < 1 + length:
                raise TransportError("ISO-TP SF: frame too short for declared length")
            return bytes(first[1: 1 + length])

        if pci_type == 0x1:
            # First Frame
            total_len = ((first[0] & 0x0F) << 8) | first[1]
            buf = bytearray(first[2:8])
            expected_sn = 1

            for cf in frames[1:]:
                cf_type = (cf[0] >> 4) & 0x0F
                if cf_type != 0x2:
                    raise TransportError(
                        f"ISO-TP multi-frame: expected CF (0x2x), got PCI type 0x{cf_type:X}"
                    )
                sn = cf[0] & 0x0F
                if sn != expected_sn:
                    raise TransportError(
                        f"ISO-TP CF sequence error: expected SN={expected_sn}, got SN={sn}"
                    )
                buf.extend(cf[1:])
                expected_sn = (expected_sn + 1) & 0x0F
                if len(buf) >= total_len:
                    break

            result = bytes(buf[:total_len])
            if len(result) < total_len:
                raise TransportError(
                    f"ISO-TP truncated: expected {total_len} bytes, got {len(result)}"
                )
            return result

        raise TransportError(
            f"ISO-TP decode: unexpected PCI type 0x{pci_type:X} in first frame"
        )
