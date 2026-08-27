# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""Free transports. Real-hardware transports (SocketCAN, PCAN/Kvaser, DoIP,
SOME/IP) live in xaloqi-tester-pro and are re-exported here lazily for
v1.4.x API compatibility."""
from .base import CanBus
from .virtual import VirtualBus

__all__ = ["CanBus", "VirtualBus", "SocketCanBus", "HardwareBus", "PcanBus", "KvaserBus"]

_PRO_EXPORTS = {
    "SocketCanBus": "transport.socketcan:SocketCanBus",
    "HardwareBus":  "transport.hardware:HardwareBus",
    "PcanBus":      "transport.hardware:PcanBus",
    "KvaserBus":    "transport.hardware:KvaserBus",
    "DoipBus":      "transport.doip:DoipBus",
}


def __getattr__(name: str):
    target = _PRO_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module 'xaloqi.tester.transport' has no attribute '{name}'")
    mod_path, _, attr = target.partition(":")
    try:
        import importlib
        mod = importlib.import_module(f"xaloqi_tester_pro.{mod_path}")
    except ImportError as exc:
        from .._plugins import PRO_URL
        raise ImportError(
            f"'{name}' is part of Xaloqi TestLab Pro — {PRO_URL}\n"
            f"Install the xaloqi-tester-pro wheel from your TestLab ZIP to enable it."
        ) from exc
    return getattr(mod, attr)
