# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
transport/base.py — CanBus abstract base class.
"""
import abc
from typing import Optional, Tuple


class CanBus(abc.ABC):
    """Abstract CAN bus interface. Implement to add new hardware backends."""

    #: True for message-oriented transports (e.g. DoIP over TCP) that carry
    #: whole UDS PDUs of any length. UdsTester skips ISO-TP framing, flow
    #: control, and reassembly entirely for these — send()/recv() exchange
    #: raw UDS PDUs, not 8-byte CAN frames.
    is_message_transport: bool = False

    @abc.abstractmethod
    async def send(self, arbitration_id: int, data: bytes) -> None:
        """Send a CAN frame. data must be 8 bytes or fewer."""

    @abc.abstractmethod
    async def recv(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        """
        Receive a CAN frame.

        Returns:
            (arbitration_id, data) tuple, or None on timeout.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Release hardware resources."""
