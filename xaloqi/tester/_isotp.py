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

    def flow_control_cts(self) -> bytes:
        """
        Return a Flow Control CTS frame.
        BS=0 (no block limit), STmin=0 (no delay).
        """
        return bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

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
