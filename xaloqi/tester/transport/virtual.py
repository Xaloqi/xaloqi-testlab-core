# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
transport/virtual.py — In-process loopback bus for unit testing.

No hardware required. VirtualBus.pair() returns two buses that are
connected: frames sent on one arrive on the other.
"""
import asyncio
from typing import Optional, Tuple

from .base import CanBus


class VirtualBus(CanBus):
    """
    In-process loopback bus. No hardware. Used for unit testing.

    Use VirtualBus.pair() to create a connected tester/ECU pair.
    """

    def __init__(
        self,
        send_q: asyncio.Queue,
        recv_q: asyncio.Queue,
    ) -> None:
        self._send_q = send_q
        self._recv_q = recv_q

    @classmethod
    def pair(cls, channel: str = "test0") -> Tuple["VirtualBus", "VirtualBus"]:
        """
        Create a connected pair: (tester_bus, ecu_bus).
        Frames sent on tester_bus arrive on ecu_bus and vice versa.
        """
        q_ab: asyncio.Queue = asyncio.Queue()  # A sends, B receives
        q_ba: asyncio.Queue = asyncio.Queue()  # B sends, A receives
        bus_a = cls(send_q=q_ab, recv_q=q_ba)
        bus_b = cls(send_q=q_ba, recv_q=q_ab)
        return bus_a, bus_b

    async def send(self, arbitration_id: int, data: bytes) -> None:
        await self._send_q.put((arbitration_id, bytes(data)))

    async def recv(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        try:
            result = await asyncio.wait_for(self._recv_q.get(), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        pass  # Nothing to release for in-process queues
