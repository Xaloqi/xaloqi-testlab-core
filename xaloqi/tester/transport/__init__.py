# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""Free transports. Real-hardware transports (SocketCAN, PCAN/Kvaser, DoIP,
SOME/IP) live in xaloqi-tester-pro and are re-exported here lazily for
v1.4.x API compatibility."""
from .base import CanBus
from .virtual import VirtualBus

_PRO_EXPORTS = {
    "SocketCanBus": "transport.socketcan:SocketCanBus",
    "HardwareBus":  "transport.hardware:HardwareBus",
    "PcanBus":      "transport.hardware:PcanBus",
    "KvaserBus":    "transport.hardware:KvaserBus",
    "DoipBus":      "transport.doip:DoipBus",
}

_FREE_EXPORTS = ("CanBus", "VirtualBus")


def _resolve(name: str):
    target = _PRO_EXPORTS[name]
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


def _available_exports() -> list:
    """Free names plus the Pro names whose import succeeds. Computed on access
    (module __getattr__), never at import: core must not import Pro at load, and
    `import *` then works on a free install (TestLab#99) and includes DoipBus
    etc. with Pro (xaloqi-testlab-core#13)."""
    names = list(_FREE_EXPORTS)
    for name in _PRO_EXPORTS:
        try:
            _resolve(name)
        except ImportError:
            continue
        names.append(name)
    return names


def __getattr__(name: str):
    if name == "__all__":
        return _available_exports()
    if name not in _PRO_EXPORTS:
        raise AttributeError(f"module 'xaloqi.tester.transport' has no attribute '{name}'")
    return _resolve(name)
