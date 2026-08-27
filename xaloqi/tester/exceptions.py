# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
exceptions.py — Exception hierarchy for xaloqi-tester.
"""


class UdsError(Exception):
    """Base class for all xaloqi-tester errors."""


class NrcError(UdsError):
    """ECU returned a Negative Response Code."""

    def __init__(self, sid: int, nrc: int, name: str) -> None:
        self.sid = sid
        self.nrc = nrc
        self.name = name
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"NRC 0x{self.nrc:02X} ({self.name}) for SID 0x{self.sid:02X}"
        )


class TimeoutError(UdsError):
    """No response received within the configured timeout."""

    def __init__(self, sid: int, timeout: float) -> None:
        self.sid = sid
        self.timeout = timeout
        super().__init__(
            f"Timeout waiting for response to SID 0x{sid:02X} "
            f"(timeout={timeout:.3f}s)"
        )


class TransportError(UdsError):
    """ISO-TP framing or CAN bus error."""


class SovdError(UdsError):
    """OpenSOVD REST API returned an error status code."""

    def __init__(self, status_code: int, path: str, detail: object = None) -> None:
        self.status_code = status_code
        self.path = path
        self.detail = detail
        super().__init__(
            f"SOVD {status_code} on {path}"
            + (f": {detail}" if detail else "")
        )


class ConfigError(UdsError):
    """Invalid or missing configuration."""


class LicenseError(UdsError):
    """License key missing, invalid, or expired."""
