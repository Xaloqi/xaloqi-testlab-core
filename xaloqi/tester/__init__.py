# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
xaloqi/tester/__init__.py — Public API for xaloqi-tester.

Usage:
    async with UdsTester("vcan0", rx_id=0x7E8, tx_id=0x7DF) as ecu:
        await ecu.session(Session.EXTENDED)
        vin = await ecu.read_did(0xF190)

Pro features (real transports, SOVD, multi-ECU workspaces) are provided by
the separately-installed ``xaloqi-tester-pro`` package and re-exported here
for API compatibility: ``from xaloqi.tester import DoipBus`` keeps working
when pro is installed, and raises with an informative message when it is not
(see __getattr__ at the end of this module).
"""
from __future__ import annotations

import asyncio
import enum
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable, Dict, List, Optional, Tuple, Union
)

from .exceptions import (
    UdsError, NrcError, TimeoutError, TransportError, ConfigError, LicenseError,
    SovdError,
)
from ._isotp import IsoTpEngine
from ._security import derive_key
from ._config import TesterConfig, load_config, load_eds_config, load_testlab_config
from .transport.base import CanBus
from .transport.virtual import VirtualBus
from . import _plugins

__all__ = [
    # Main classes
    "UdsTester", "SyncUdsTester",
    # Workspace (pro, lazy re-export)
    "UdsWorkspace", "SyncUdsWorkspace",
    # Enumerations
    "Session", "ResetType", "RoutineControl", "DtcStatusMask", "CommControlType",
    # Response types
    "UdsResponse", "DidResponse", "MemoryResponse", "DtcRecord", "DtcResponse",
    "FaultDetectionCounterRecord", "FaultDetectionCounterResponse",
    "SecuritySeedResponse", "RoutineResponse",
    # Exceptions
    "UdsError", "NrcError", "TimeoutError", "TransportError", "ConfigError", "LicenseError",
    "SovdError",
    # SOVD (pro, lazy re-export)
    "SovdTester",
    # Config
    "TesterConfig", "load_config", "load_eds_config", "load_testlab_config",
    # Transport (paid ones are pro, lazy re-export)
    "CanBus", "VirtualBus", "SocketCanBus", "HardwareBus", "PcanBus", "KvaserBus",
    "DoipBus",
]

# v1.4.x API names that now live in xaloqi-tester-pro. Resolved lazily via
# module __getattr__ so `from xaloqi.tester import DoipBus` keeps working in
# a core+pro install without core importing pro at load time.
_PRO_EXPORTS = {
    "SovdTester":       "sovd_tester:SovdTester",
    "UdsWorkspace":     "workspace:UdsWorkspace",
    "SyncUdsWorkspace": "workspace:SyncUdsWorkspace",
    "SocketCanBus":     "transport.socketcan:SocketCanBus",
    "HardwareBus":      "transport.hardware:HardwareBus",
    "PcanBus":          "transport.hardware:PcanBus",
    "KvaserBus":        "transport.hardware:KvaserBus",
    "DoipBus":          "transport.doip:DoipBus",
}


def __getattr__(name: str):
    target = _PRO_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'xaloqi.tester' has no attribute '{name}'")
    mod_path, _, attr = target.partition(":")
    try:
        import importlib
        mod = importlib.import_module(f"xaloqi_tester_pro.{mod_path}")
    except ImportError as exc:
        raise ImportError(
            f"'{name}' is part of Xaloqi TestLab Pro — {_plugins.PRO_URL}\n"
            f"Install the xaloqi-tester-pro wheel from your TestLab ZIP to enable it."
        ) from exc
    return getattr(mod, attr)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Session(enum.IntEnum):
    DEFAULT     = 0x01
    PROGRAMMING = 0x02
    EXTENDED    = 0x03


class ResetType(enum.IntEnum):
    HARD    = 0x01
    KEY_OFF = 0x02
    SOFT    = 0x03


class RoutineControl(enum.IntEnum):
    START          = 0x01
    STOP           = 0x02
    REQUEST_RESULT = 0x03


class DtcStatusMask(enum.IntFlag):
    TEST_FAILED                    = 0x01
    TEST_FAILED_THIS_CYCLE         = 0x02
    PENDING_DTC                    = 0x04
    CONFIRMED_DTC                  = 0x08
    TEST_NOT_COMPLETED_SINCE_CLEAR = 0x10
    TEST_FAILED_SINCE_CLEAR        = 0x20
    TEST_NOT_COMPLETED_THIS_CYCLE  = 0x40
    WARNING_INDICATOR_REQUESTED    = 0x80
    ALL                            = 0xFF


class CommControlType(enum.IntEnum):
    ENABLE_RX_TX          = 0x00
    ENABLE_RX_DISABLE_TX  = 0x01
    DISABLE_RX_ENABLE_TX  = 0x02
    DISABLE_RX_TX         = 0x03


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------

@dataclass
class UdsResponse:
    """Base response. All service responses inherit from this."""
    raw: bytes
    latency_ms: float


@dataclass
class DidResponse(UdsResponse):
    did_id: int
    data: bytes

    def as_int(self, signed: bool = False) -> int:
        """Interpret data as big-endian integer."""
        return int.from_bytes(self.data, "big", signed=signed)

    def as_int8_offset(self, offset: int = 0) -> int:
        """Single byte decoded as T = raw - offset. Used for temp DIDs."""
        return self.data[0] - offset

    def as_uint16_be(self) -> int:
        """Two bytes, big-endian unsigned."""
        return int.from_bytes(self.data[:2], "big")

    def as_float_scaled(self, scale: float, offset: float = 0.0) -> float:
        """Linear scaling: value = raw * scale + offset."""
        return self.as_int() * scale + offset


@dataclass
class MemoryResponse(UdsResponse):
    """Response to 0x23 ReadMemoryByAddress."""
    address: int
    data: bytes

    def as_int(self, signed: bool = False) -> int:
        """Interpret data as big-endian integer."""
        return int.from_bytes(self.data, "big", signed=signed)


@dataclass
class DtcRecord:
    code: int
    status_mask: int
    description: str


@dataclass
class DtcResponse(UdsResponse):
    dtcs: List[DtcRecord] = field(default_factory=list)


@dataclass
class FaultDetectionCounterRecord:
    code: int
    counter: int


@dataclass
class FaultDetectionCounterResponse(UdsResponse):
    records: List[FaultDetectionCounterRecord] = field(default_factory=list)


@dataclass
class SecuritySeedResponse(UdsResponse):
    level: int
    seed: bytes


@dataclass
class RoutineResponse(UdsResponse):
    routine_id: int
    control_type: int
    status_record: bytes


# ---------------------------------------------------------------------------
# NRC name table
# ---------------------------------------------------------------------------

_NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLength",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


# ---------------------------------------------------------------------------
# UdsTester
# ---------------------------------------------------------------------------

class UdsTester:
    """
    Async UDS tester. Use as an async context manager.

    Args:
        interface:  CAN interface name for SocketCAN (e.g. "vcan0", "can0")
                    OR a CanBus instance for testing with VirtualBus.
        rx_id:      CAN ID to receive responses from (ECU TX ID).
        tx_id:      CAN ID to send requests to (ECU RX ID / functional address).
        timeout:    Default response timeout in seconds. Default: 0.15 (150ms).
        keepalive:  If True, send TesterPresent every 2s in background.
        p2_timeout: ISO-TP P2 server max timeout override. Default: 0.15s.
        verbose:    If True, log every CAN frame to stderr.
    """

    def __init__(
        self,
        interface: Union[str, CanBus],
        rx_id: int,
        tx_id: int,
        timeout: float = 0.15,
        keepalive: bool = True,
        p2_timeout: float = 0.15,
        verbose: bool = False,
    ) -> None:
        self._interface = interface
        self._rx_id = rx_id
        self._tx_id = tx_id
        self._timeout = timeout
        self._keepalive = keepalive
        self._p2_timeout = p2_timeout
        self._verbose = verbose

        self._bus: Optional[CanBus] = None
        self._isotp = IsoTpEngine()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_latency_ms: float = 0.0

        # DID config for multi-DID parsing (populated by from_config)
        self._did_defs: Dict[int, "DidDef"] = {}  # type: ignore[name-defined]

    @classmethod
    def from_config(
        cls,
        config_path: Union[str, Path],
        **kwargs,
    ) -> "UdsTester":
        """
        Create UdsTester pre-configured from diagnostics_config.yaml or testlab_config.yaml.
        """
        config = load_config(config_path)
        tester = cls(
            interface=kwargs.pop("interface", config.interface),
            rx_id=kwargs.pop("rx_id", config.rx_id),
            tx_id=kwargs.pop("tx_id", config.tx_id),
            p2_timeout=kwargs.pop("p2_timeout", config.p2_timeout_ms / 1000.0),
            **kwargs,
        )
        # Store DID definitions for multi-DID parsing
        from ._config import DidDef
        tester._did_defs = {d.id: d for d in config.dids}
        return tester

    @classmethod
    def sync(
        cls,
        interface: Union[str, CanBus],
        rx_id: int,
        tx_id: int,
        **kwargs,
    ) -> "SyncUdsTester":
        """Return a synchronous facade. Use as a regular context manager (not async)."""
        async_tester = cls(interface, rx_id, tx_id, **kwargs)
        return SyncUdsTester(async_tester)

    # ── Context manager ──────────────────────────────────────────────────────

    async def __aenter__(self) -> "UdsTester":
        # Open bus. A string interface means SocketCAN — provided by
        # xaloqi-tester-pro via the transport registry. There is no runtime
        # license gate in core: the free tier (VirtualBus instances) simply
        # has nothing to gate; pro transports enforce licensing themselves.
        if isinstance(self._interface, str):
            factory = _plugins.get_transport("socketcan")
            bus = factory(self._interface)
            if hasattr(bus, "_open"):
                await bus._open()
            elif hasattr(bus, "open") and callable(bus.open):
                await bus.open()
            self._bus = bus
        else:
            self._bus = self._interface
            # Call open() if the bus has it (e.g. DoipBus needs TCP connect + routing activation)
            if hasattr(self._bus, "open") and callable(self._bus.open):
                await self._bus.open()

        # Start keepalive
        if self._keepalive:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        return self

    async def __aexit__(self, *args) -> None:
        # Cancel keepalive
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            self._keepalive_task = None

        # Close bus (only if we opened it)
        if self._bus is not None and isinstance(self._interface, str):
            await self._bus.close()
            self._bus = None

    # ── Internal request/response ────────────────────────────────────────────

    def _log(self, direction: str, can_id: int, data: bytes) -> None:
        if self._verbose:
            hex_data = " ".join(f"{b:02X}" for b in data)
            import sys
            print(f"  {direction} [{can_id:03X}] {hex_data}", file=sys.stderr)

    async def _request(self, pdu: bytes, timeout: Optional[float] = None) -> bytes:
        """
        Core request/response method. All service methods call this.

        1. Encode pdu via IsoTpEngine
        2. Send CAN frames (handling multi-frame FC handshake)
        3. Receive response frames (handling multi-frame assembly)
        4. Decode response via IsoTpEngine
        5. If response[0] == 0x7F: raise NrcError
        6. Return response bytes (positive response)
        """
        if self._bus is None:
            raise TransportError("UdsTester not open — use 'async with UdsTester(...) as ecu'")

        t_start = time.monotonic()
        effective_timeout = timeout if timeout is not None else self._timeout

        # Message-oriented transports (DoIP) carry whole UDS PDUs — no ISO-TP.
        if getattr(self._bus, "is_message_transport", False):
            return await self._request_message(pdu, effective_timeout, t_start)

        # TX
        frames = self._isotp.encode(pdu)
        if len(frames) == 1:
            self._log("TX", self._tx_id, frames[0])
            await self._bus.send(self._tx_id, frames[0])
        else:
            # Multi-frame TX: send FF, wait for FC, send CFs
            self._log("TX-FF", self._tx_id, frames[0])
            await self._bus.send(self._tx_id, frames[0])

            # Wait for Flow Control
            fc = await self._bus.recv(timeout=effective_timeout)
            if fc is None:
                raise TimeoutError(sid=pdu[0], timeout=effective_timeout)
            _, fc_data = fc
            if len(fc_data) < 1 or (fc_data[0] >> 4) != 0x3:
                raise TransportError(f"Expected Flow Control frame, got: {fc_data.hex()}")
            fc_type = fc_data[0] & 0x0F
            if fc_type != 0x00:  # 0x00 = CTS
                raise TransportError(f"Flow Control type {fc_type:#x} not supported (expected CTS=0)")

            for cf in frames[1:]:
                self._log("TX-CF", self._tx_id, cf)
                await self._bus.send(self._tx_id, cf)

        # RX — collect frames
        rx_frames: List[bytes] = []
        total_len: Optional[int] = None
        received: int = 0
        expected_sn: int = 1
        sid = pdu[0]

        deadline = time.monotonic() + effective_timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            result = await self._bus.recv(timeout=max(0.001, remaining))
            if result is None:
                continue

            frame_id, frame_data = result
            if frame_id != self._rx_id:
                continue

            self._log("RX", frame_id, frame_data)

            if not frame_data:
                continue

            pci_type = (frame_data[0] >> 4) & 0x0F

            if pci_type == 0x0:  # Single Frame
                self._last_latency_ms = (time.monotonic() - t_start) * 1000.0
                response = self._isotp.decode([frame_data])
                return self._check_nrc(response, sid)

            elif pci_type == 0x1:  # First Frame
                total_len = ((frame_data[0] & 0x0F) << 8) | frame_data[1]
                rx_frames = [frame_data]
                received = 6
                expected_sn = 1
                # Send FC CTS immediately
                fc_cts = self._isotp.flow_control_cts()
                self._log("TX-FC", self._tx_id, fc_cts)
                await self._bus.send(self._tx_id, fc_cts)
                # Extend deadline for multi-frame reception
                deadline = time.monotonic() + effective_timeout

            elif pci_type == 0x2:  # Consecutive Frame
                if total_len is None:
                    continue
                sn = frame_data[0] & 0x0F
                if sn != expected_sn:
                    raise TransportError(
                        f"ISO-TP SN error: expected {expected_sn}, got {sn}"
                    )
                expected_sn = (expected_sn + 1) & 0x0F
                rx_frames.append(frame_data)
                cf_bytes = min(7, total_len - received)
                received += cf_bytes
                if received >= total_len:
                    self._last_latency_ms = (time.monotonic() - t_start) * 1000.0
                    response = self._isotp.decode(rx_frames)
                    return self._check_nrc(response, sid)

        raise TimeoutError(sid=sid, timeout=effective_timeout)

    async def _request_message(self, pdu: bytes, timeout: float, t_start: float) -> bytes:
        """
        Request/response over a message-oriented transport (is_message_transport).

        The transport (e.g. DoIP over TCP) carries whole UDS PDUs of any length,
        so there is no ISO-TP framing, flow control, or reassembly: send the raw
        PDU, return the first raw response from our rx_id.
        """
        sid = pdu[0]

        self._log("TX", self._tx_id, pdu)
        await self._bus.send(self._tx_id, pdu)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            result = await self._bus.recv(timeout=max(0.001, remaining))
            if result is None:
                continue

            frame_id, response = result
            if frame_id != self._rx_id or not response:
                continue

            self._log("RX", frame_id, response)
            self._last_latency_ms = (time.monotonic() - t_start) * 1000.0
            return self._check_nrc(response, sid)

        raise TimeoutError(sid=sid, timeout=timeout)

    def _check_nrc(self, response: bytes, sid: int) -> bytes:
        """Raise NrcError if response is negative (0x7F ...), else return."""
        if response and response[0] == 0x7F:
            resp_sid = response[1] if len(response) > 1 else sid
            nrc = response[2] if len(response) > 2 else 0x00
            name = _NRC_NAMES.get(nrc, f"unknownNrc_0x{nrc:02X}")
            raise NrcError(sid=resp_sid, nrc=nrc, name=name)
        return response

    async def _keepalive_loop(self) -> None:
        """Send TesterPresent (suppress response) every 2 seconds."""
        while True:
            await asyncio.sleep(2.0)
            try:
                await self.tester_present(suppress_response=True)
            except Exception:
                pass  # Never crash the keepalive

    # ── Session management ───────────────────────────────────────────────────

    async def session(self, session: Session) -> UdsResponse:
        """0x10 DiagnosticSessionControl."""
        pdu = bytes([0x10, int(session)])
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def reset(self, reset_type: ResetType = ResetType.SOFT) -> UdsResponse:
        """0x11 ECUReset."""
        pdu = bytes([0x11, int(reset_type)])
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    # ── Security ─────────────────────────────────────────────────────────────

    async def security_access(self, level: int) -> UdsResponse:
        """0x27 SecurityAccess — full seed/key exchange in one call."""
        seed_resp = await self.request_seed(level)
        seed = seed_resp.seed
        key = derive_key(seed, level)
        return await self.send_key(level, key)

    async def request_seed(self, level: int) -> SecuritySeedResponse:
        """0x27 SecurityAccess — seed request only."""
        pdu = bytes([0x27, level * 2 - 1])
        resp = await self._request(pdu)
        if resp[0] != 0x67:
            raise TransportError(f"SecurityAccess seed response: expected 0x67, got 0x{resp[0]:02X}")
        seed = resp[2:6]
        return SecuritySeedResponse(
            raw=resp,
            latency_ms=self._last_latency_ms,
            level=level,
            seed=seed,
        )

    async def send_key(self, level: int, key: bytes) -> UdsResponse:
        """0x27 SecurityAccess — key response only."""
        pdu = bytes([0x27, level * 2]) + key
        resp = await self._request(pdu)
        if resp[0] != 0x67:
            raise TransportError(f"SecurityAccess key response: expected 0x67, got 0x{resp[0]:02X}")
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    # ── Data ─────────────────────────────────────────────────────────────────

    async def read_did(self, did_id: int) -> DidResponse:
        """0x22 ReadDataByIdentifier — single DID."""
        pdu = bytes([0x22, (did_id >> 8) & 0xFF, did_id & 0xFF])
        resp = await self._request(pdu)
        if resp[0] != 0x62:
            raise TransportError(f"ReadDID response: expected 0x62, got 0x{resp[0]:02X}")
        echo_id = int.from_bytes(resp[1:3], "big")
        if echo_id != did_id:
            raise TransportError(f"ReadDID echo mismatch: expected 0x{did_id:04X}, got 0x{echo_id:04X}")
        return DidResponse(
            raw=resp,
            latency_ms=self._last_latency_ms,
            did_id=did_id,
            data=resp[3:],
        )

    async def read_dids(self, did_ids: List[int]) -> Dict[int, DidResponse]:
        """0x22 ReadDataByIdentifier — multi-DID in a single UDS request."""
        pdu = bytes([0x22])
        for did_id in did_ids:
            pdu += bytes([(did_id >> 8) & 0xFF, did_id & 0xFF])
        resp = await self._request(pdu)
        if resp[0] != 0x62:
            raise TransportError(f"ReadDIDs response: expected 0x62, got 0x{resp[0]:02X}")

        results: Dict[int, DidResponse] = {}
        offset = 1  # skip SID 0x62
        for did_id in did_ids:
            echo_id = int.from_bytes(resp[offset:offset+2], "big")
            if echo_id != did_id:
                raise TransportError(f"ReadDIDs DID echo mismatch: expected 0x{did_id:04X}, got 0x{echo_id:04X}")
            offset += 2

            # Determine data length from config if available
            did_def = self._did_defs.get(did_id)
            if did_def is not None:
                data_len = did_def.data_length
                data = resp[offset:offset + data_len]
                offset += data_len
            else:
                # Can't know length without config — take remainder for last DID
                data = resp[offset:]
                offset = len(resp)

            results[did_id] = DidResponse(
                raw=resp,
                latency_ms=self._last_latency_ms,
                did_id=did_id,
                data=data,
            )

        return results

    async def write_did(self, did_id: int, data: bytes) -> UdsResponse:
        """0x2E WriteDataByIdentifier."""
        pdu = bytes([0x2E, (did_id >> 8) & 0xFF, did_id & 0xFF]) + data
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def read_memory_by_address(
        self,
        address: int,
        size: int,
        address_len: int = 4,
        size_len: int = 4,
    ) -> MemoryResponse:
        """0x23 ReadMemoryByAddress — read size bytes starting at address.

        Args:
            address:     Memory address to read from (big-endian in PDU).
            size:        Number of bytes to read.
            address_len: Width of the memoryAddress field in bytes (1–4). Default: 4.
            size_len:    Width of the memorySize field in bytes (1–4). Default: 4.

        Returns:
            MemoryResponse with address and data fields.

        Raises:
            NrcError:       ECU returned a negative response (e.g. 0x33 securityAccessDenied,
                            0x31 requestOutOfRange, 0x7F serviceNotSupportedInActiveSession).
            TransportError: Response SID was not 0x63.
        """
        alfid = (size_len << 4) | address_len
        pdu = bytes([0x23, alfid])
        pdu += address.to_bytes(address_len, "big")
        pdu += size.to_bytes(size_len, "big")
        resp = await self._request(pdu)
        if resp[0] != 0x63:
            raise TransportError(
                f"ReadMemoryByAddress response: expected 0x63, got 0x{resp[0]:02X}"
            )
        return MemoryResponse(
            raw=resp,
            latency_ms=self._last_latency_ms,
            address=address,
            data=resp[1:],
        )

    async def write_memory_by_address(
        self,
        address: int,
        data: bytes,
        address_len: int = 4,
        size_len: int = 4,
    ) -> UdsResponse:
        """0x3D WriteMemoryByAddress — write data to address.

        Args:
            address:     Memory address to write to (big-endian in PDU).
            data:        Bytes to write; len(data) is used as memorySize.
            address_len: Width of the memoryAddress field in bytes (1–4). Default: 4.
            size_len:    Width of the memorySize field in bytes (1–4). Default: 4.

        Returns:
            UdsResponse. The raw bytes contain the echo:
            [0x7D, alfid, memoryAddress..., memorySize...].

        Raises:
            NrcError:       ECU returned a negative response.
            TransportError: Response SID was not 0x7D.
        """
        alfid = (size_len << 4) | address_len
        pdu = bytes([0x3D, alfid])
        pdu += address.to_bytes(address_len, "big")
        pdu += len(data).to_bytes(size_len, "big")
        pdu += data
        resp = await self._request(pdu)
        if resp[0] != 0x7D:
            raise TransportError(
                f"WriteMemoryByAddress response: expected 0x7D, got 0x{resp[0]:02X}"
            )
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    # ── DTCs ─────────────────────────────────────────────────────────────────

    async def read_dtcs(
        self,
        status_mask: Union[int, DtcStatusMask] = DtcStatusMask.ALL,
    ) -> DtcResponse:
        """0x19 ReadDTCInformation — sub-function 0x02."""
        pdu = bytes([0x19, 0x02, int(status_mask)])
        resp = await self._request(pdu)
        if resp[0] != 0x59:
            raise TransportError(f"ReadDTCs response: expected 0x59, got 0x{resp[0]:02X}")

        dtcs: List[DtcRecord] = []
        # resp: [0x59, sub_fn, dtc_status_availability_mask, DTC[3], status, DTC[3], status, ...]
        offset = 3  # skip SID, sub_fn, status_avail_mask
        while offset + 3 < len(resp):
            code = int.from_bytes(resp[offset:offset+3], "big")
            status = resp[offset+3]
            offset += 4
            dtcs.append(DtcRecord(code=code, status_mask=status, description=""))

        return DtcResponse(raw=resp, latency_ms=self._last_latency_ms, dtcs=dtcs)

    async def clear_dtcs(self, group: int = 0xFFFFFF) -> UdsResponse:
        """0x14 ClearDiagnosticInformation."""
        pdu = bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF])
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def read_dtcs_fault_counter(self) -> FaultDetectionCounterResponse:
        """0x19/0x0B reportDTCFaultDetectionCounter — ISO 14229-1 §11.3.11.

        Returns pre-confirmed DTCs (testFailed == 0, counter < 0xFF) with their
        fault detection counter value. Response has NO DTCStatusAvailabilityMask
        byte — 4-byte records start immediately after [0x59, 0x0B].
        """
        pdu = bytes([0x19, 0x0B])
        resp = await self._request(pdu)
        if resp[0] != 0x59:
            raise TransportError(f"ReadDTCFaultCounter response: expected 0x59, got 0x{resp[0]:02X}")
        records: List[FaultDetectionCounterRecord] = []
        # resp: [0x59, 0x0B, {dtc3B, counter}...] — no availability mask
        offset = 2
        while offset + 3 < len(resp):
            code    = int.from_bytes(resp[offset:offset + 3], "big")
            counter = resp[offset + 3]
            offset += 4
            records.append(FaultDetectionCounterRecord(code=code, counter=counter))
        return FaultDetectionCounterResponse(raw=resp, latency_ms=self._last_latency_ms, records=records)

    async def read_dtcs_permanent(self) -> DtcResponse:
        """0x19/0x19 reportDTCWithPermanentStatus — ISO 14229-1 §11.3.25.

        Returns DTCs marked permanent (not clearable by SID 0x14). Same 4-byte
        record format as 0x02; availability mask is at byte 2.
        """
        pdu = bytes([0x19, 0x19])
        resp = await self._request(pdu)
        if resp[0] != 0x59:
            raise TransportError(f"ReadDTCPermanentStatus response: expected 0x59, got 0x{resp[0]:02X}")
        dtcs: List[DtcRecord] = []
        # resp: [0x59, 0x19, availMask, {dtc3B, statusByte}...] — same layout as 0x02
        offset = 3
        while offset + 3 < len(resp):
            code   = int.from_bytes(resp[offset:offset + 3], "big")
            status = resp[offset + 3]
            offset += 4
            dtcs.append(DtcRecord(code=code, status_mask=status, description=""))
        return DtcResponse(raw=resp, latency_ms=self._last_latency_ms, dtcs=dtcs)

    # ── Routines ─────────────────────────────────────────────────────────────

    async def routine_control(
        self,
        routine_id: int,
        control_type: RoutineControl = RoutineControl.START,
        option_record: bytes = b"",
    ) -> RoutineResponse:
        """0x31 RoutineControl."""
        pdu = bytes([
            0x31,
            int(control_type),
            (routine_id >> 8) & 0xFF,
            routine_id & 0xFF,
        ]) + option_record
        resp = await self._request(pdu)
        if resp[0] != 0x71:
            raise TransportError(f"RoutineControl response: expected 0x71, got 0x{resp[0]:02X}")
        return RoutineResponse(
            raw=resp,
            latency_ms=self._last_latency_ms,
            routine_id=routine_id,
            control_type=int(control_type),
            status_record=resp[4:] if len(resp) > 4 else b"",
        )

    # ── Communication control ─────────────────────────────────────────────────

    async def comm_control(
        self,
        control_type: CommControlType,
        comm_type: int = 0x01,
    ) -> UdsResponse:
        """0x28 CommunicationControl."""
        pdu = bytes([0x28, int(control_type), comm_type])
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def tester_present(self, suppress_response: bool = False) -> UdsResponse:
        """0x3E TesterPresent."""
        pdu = bytes([0x3E, 0x80 if suppress_response else 0x00])
        if suppress_response:
            # Fire-and-forget — ECU does not respond
            if getattr(self._bus, "is_message_transport", False):
                self._log("TX", self._tx_id, pdu)
                await self._bus.send(self._tx_id, pdu)
            else:
                frames = self._isotp.encode(pdu)
                for f in frames:
                    self._log("TX", self._tx_id, f)
                    await self._bus.send(self._tx_id, f)
            return UdsResponse(raw=b"", latency_ms=0.0)
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def dtc_settings(self, setting_type: int) -> UdsResponse:
        """0x85 ControlDTCSetting. setting_type: 0x01=on, 0x02=off."""
        pdu = bytes([0x85, setting_type])
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    # ── Firmware update ───────────────────────────────────────────────────────

    async def request_download(
        self,
        memory_address: int,
        memory_size: int,
        compression: int = 0x00,
        encrypting: int = 0x00,
    ) -> UdsResponse:
        """0x34 RequestDownload."""
        data_format = (compression << 4) | encrypting
        # addressAndLengthFormatIdentifier: high nibble = memorySize length, low = memoryAddress length
        addr_len = 4
        size_len = 4
        alfi = (size_len << 4) | addr_len
        pdu = bytes([0x34, data_format, alfi])
        pdu += memory_address.to_bytes(addr_len, "big")
        pdu += memory_size.to_bytes(size_len, "big")
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def request_upload(
        self,
        memory_address: int,
        memory_size: int,
        compression: int = 0x00,
        encrypting: int = 0x00,
    ) -> UdsResponse:
        """0x35 RequestUpload.

        Mirror of RequestDownload: same request layout (dataFormatIdentifier +
        addressAndLengthFormatIdentifier + memoryAddress + memorySize), but the
        subsequent 0x36 TransferData blocks flow ECU → tester. Completes the DFU
        verify loop (download firmware, then upload it back to check it).
        """
        data_format = (compression << 4) | encrypting
        addr_len = 4
        size_len = 4
        alfi = (size_len << 4) | addr_len
        pdu = bytes([0x35, data_format, alfi])
        pdu += memory_address.to_bytes(addr_len, "big")
        pdu += memory_size.to_bytes(size_len, "big")
        resp = await self._request(pdu)
        if resp and resp[0] != 0x75:
            raise TransportError(f"RequestUpload response: expected 0x75, got 0x{resp[0]:02X}")
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def transfer_data(
        self,
        block_sequence: int,
        data: bytes,
    ) -> UdsResponse:
        """0x36 TransferData — single block."""
        pdu = bytes([0x36, block_sequence & 0xFF]) + data
        resp = await self._request(pdu)
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def transfer_exit(self) -> UdsResponse:
        """0x37 RequestTransferExit."""
        resp = await self._request(bytes([0x37]))
        return UdsResponse(raw=resp, latency_ms=self._last_latency_ms)

    async def transfer_firmware(
        self,
        data: bytes,
        memory_address: int,
        block_size: int = 0xFFF,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> UdsResponse:
        """Complete firmware transfer: request_download + N*transfer_data + transfer_exit."""
        await self.request_download(memory_address, len(data))

        total = len(data)
        sent = 0
        block_num = 1

        while sent < total:
            chunk = data[sent:sent + block_size]
            await self.transfer_data(block_num, chunk)
            sent += len(chunk)
            block_num = (block_num + 1) & 0xFF  # wraps at 0xFF → 0x00
            if progress_cb is not None:
                progress_cb(sent, total)

        return await self.transfer_exit()

    # ── Raw access ────────────────────────────────────────────────────────────

    async def raw_request(
        self,
        pdu: bytes,
        timeout: Optional[float] = None,
    ) -> bytes:
        """Send raw UDS PDU, return raw response PDU."""
        return await self._request(pdu, timeout=timeout)


# ---------------------------------------------------------------------------
# SyncUdsTester — synchronous facade
# ---------------------------------------------------------------------------

class SyncUdsTester:
    """
    Synchronous facade over UdsTester. Returned by UdsTester.sync().
    All methods have identical signatures but are not async.
    """

    def __init__(self, async_tester: UdsTester) -> None:
        self._async_tester = async_tester
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def __enter__(self) -> "SyncUdsTester":
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._async_tester.__aenter__())
        return self

    def __exit__(self, *args) -> None:
        if self._loop is not None:
            self._loop.run_until_complete(self._async_tester.__aexit__(*args))
            self._loop.close()
            self._loop = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def session(self, session: Session) -> UdsResponse:
        return self._run(self._async_tester.session(session))

    def reset(self, reset_type: ResetType = ResetType.SOFT) -> UdsResponse:
        return self._run(self._async_tester.reset(reset_type))

    def security_access(self, level: int) -> UdsResponse:
        return self._run(self._async_tester.security_access(level))

    def request_seed(self, level: int) -> SecuritySeedResponse:
        return self._run(self._async_tester.request_seed(level))

    def send_key(self, level: int, key: bytes) -> UdsResponse:
        return self._run(self._async_tester.send_key(level, key))

    def read_did(self, did_id: int) -> DidResponse:
        return self._run(self._async_tester.read_did(did_id))

    def read_dids(self, did_ids: List[int]) -> Dict[int, DidResponse]:
        return self._run(self._async_tester.read_dids(did_ids))

    def write_did(self, did_id: int, data: bytes) -> UdsResponse:
        return self._run(self._async_tester.write_did(did_id, data))

    def read_memory_by_address(
        self,
        address: int,
        size: int,
        address_len: int = 4,
        size_len: int = 4,
    ) -> MemoryResponse:
        return self._run(self._async_tester.read_memory_by_address(address, size, address_len, size_len))

    def write_memory_by_address(
        self,
        address: int,
        data: bytes,
        address_len: int = 4,
        size_len: int = 4,
    ) -> UdsResponse:
        return self._run(self._async_tester.write_memory_by_address(address, data, address_len, size_len))

    def read_dtcs(self, status_mask: Union[int, DtcStatusMask] = DtcStatusMask.ALL) -> DtcResponse:
        return self._run(self._async_tester.read_dtcs(status_mask))

    def clear_dtcs(self, group: int = 0xFFFFFF) -> UdsResponse:
        return self._run(self._async_tester.clear_dtcs(group))

    def read_dtcs_fault_counter(self) -> FaultDetectionCounterResponse:
        return self._run(self._async_tester.read_dtcs_fault_counter())

    def read_dtcs_permanent(self) -> DtcResponse:
        return self._run(self._async_tester.read_dtcs_permanent())

    def routine_control(
        self,
        routine_id: int,
        control_type: RoutineControl = RoutineControl.START,
        option_record: bytes = b"",
    ) -> RoutineResponse:
        return self._run(self._async_tester.routine_control(routine_id, control_type, option_record))

    def tester_present(self, suppress_response: bool = False) -> UdsResponse:
        return self._run(self._async_tester.tester_present(suppress_response))

    def comm_control(self, control_type: CommControlType, comm_type: int = 0x01) -> UdsResponse:
        return self._run(self._async_tester.comm_control(control_type, comm_type))

    def dtc_settings(self, setting_type: int) -> UdsResponse:
        return self._run(self._async_tester.dtc_settings(setting_type))

    def request_download(
        self, memory_address: int, memory_size: int,
        compression: int = 0x00, encrypting: int = 0x00,
    ) -> UdsResponse:
        return self._run(self._async_tester.request_download(memory_address, memory_size, compression, encrypting))

    def request_upload(
        self, memory_address: int, memory_size: int,
        compression: int = 0x00, encrypting: int = 0x00,
    ) -> UdsResponse:
        return self._run(self._async_tester.request_upload(memory_address, memory_size, compression, encrypting))

    def transfer_data(self, block_sequence: int, data: bytes) -> UdsResponse:
        return self._run(self._async_tester.transfer_data(block_sequence, data))

    def transfer_exit(self) -> UdsResponse:
        return self._run(self._async_tester.transfer_exit())

    def transfer_firmware(
        self,
        data: bytes,
        memory_address: int,
        block_size: int = 0xFFF,
        progress_cb: Optional[Callable] = None,
    ) -> UdsResponse:
        return self._run(self._async_tester.transfer_firmware(data, memory_address, block_size, progress_cb))

    def raw_request(self, pdu: bytes, timeout: Optional[float] = None) -> bytes:
        return self._run(self._async_tester.raw_request(pdu, timeout))
