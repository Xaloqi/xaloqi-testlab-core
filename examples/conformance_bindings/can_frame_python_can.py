#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
can_frame_python_can.py -- a reference implementation of
xaloqi.tester.transport.base.CanBus, backed by python-can
(LGPL-3.0-only -- a separate project, under a separate licence, from
python-can-isotp, the MIT-licensed project EDS's own ISO-TP conformance
vectors were themselves ported from; python-can-isotp uses python-can as
its own transport dependency, which is the connection between the two).

This is deliberately the whole adapter: xaloqi.tester.conformance never
asks you to adopt its own transport. Implement CanBus's two methods --
send() and recv() -- against whatever you already use to talk to your
own stack, and every suite in this package's corpus works against it.
python-can is one obvious, already-independent choice; this file proves
the contract is actually that simple by being a real, runnable one, not
a sketch. Used here only as an ordinary dependency (import, no
modification, no redistribution of its source) -- the ordinary way any
project depends on an LGPL library without itself being bound by LGPL.

Tested directly against real EDS over a real vxcan pair -- see
scripts/dogfood_against_eds.sh (repo root).

Usage:
    bus = PythonCanBus(channel="vxcan1", interface="socketcan")
    await bus.send(0x7E0, bytes.fromhex("023E00"))
    frame = await bus.recv(timeout=0.3)  # -> (arbitration_id, data) or None
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple

import can

from xaloqi.tester.transport.base import CanBus


class PythonCanBus(CanBus):
    """CanBus backed by python-can. Works with any python-can interface
    string ("socketcan", "pcan", "vector", ...) -- see python-can's own
    documentation for what your hardware needs."""

    def __init__(self, channel: str, interface: str = "socketcan", **kwargs) -> None:
        self._bus = can.interface.Bus(channel=channel, interface=interface, **kwargs)

    async def send(self, arbitration_id: int, data: bytes) -> None:
        msg = can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=False)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._bus.send, msg)

    async def recv(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, self._bus.recv, timeout)
        if msg is None:
            return None
        return msg.arbitration_id, bytes(msg.data)

    async def close(self) -> None:
        self._bus.shutdown()
