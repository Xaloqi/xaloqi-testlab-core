# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
xaloqi.tester._plugins — plugin seam between xaloqi-tester (core) and
xaloqi-tester-pro.

Core discovers optional feature providers through setuptools entry points
(PEP 621 ``[project.entry-points]``). Four groups:

    xaloqi_tester.transports      name → transport factory.
                                  A factory is ``factory(interface: str) -> bus``;
                                  the returned bus is opened by the caller
                                  (UdsTester.__aenter__ / xaloqi-sim).
    xaloqi_tester.runner_actions  campaign action name → async step handler
                                  ``handler(executor, step, index) -> StepResult``.
    xaloqi_tester.runner_hooks    runner mode name → implementation.
                                  Reserved names: "workspace" (multi-ECU
                                  campaign runner) and "rtos" (RTOS comparison
                                  module).
    xaloqi_tester.cli_commands    testlab subcommand name → ``register(subparsers)``
                                  callable that adds the subcommand parser.

Anything not found in a group simply is not available — there is no runtime
license gate in core. Unknown names that match a known Pro feature produce
one consistent message (see pro_missing_message).

Fallback discovery: when no entry-point metadata is installed (running from a
repo checkout via sys.path, or a legacy ``pip install -e .`` whose metadata
predates the split), core probes for an importable ``xaloqi_tester_pro``
package and asks it to register itself directly.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

PRO_URL = "https://xaloqi.com"

# Names advertised by Xaloqi TestLab Pro. Used ONLY to phrase the error
# message when the feature is not installed — no paid code behind them here.
PRO_TRANSPORTS = frozenset({"socketcan", "pcan", "kvaser", "doip", "someip", "sovd"})
PRO_ACTIONS = frozenset({
    "someip_discover", "someip_call", "someip_subscribe", "someip_assert_event",
    "sovd_read_data", "sovd_read_faults", "sovd_clear_faults", "sovd_call_function",
})
PRO_CLI_COMMANDS = frozenset({
    "report", "trend", "compare", "serve", "explain", "someip-validate",
})
PRO_RUNNER_HOOKS = frozenset({"workspace", "rtos"})

_GROUPS = {
    "transports":     "xaloqi_tester.transports",
    "runner_actions": "xaloqi_tester.runner_actions",
    "runner_hooks":   "xaloqi_tester.runner_hooks",
    "cli_commands":   "xaloqi_tester.cli_commands",
}

_PRO_NAMES = {
    "transports":     PRO_TRANSPORTS,
    "runner_actions": PRO_ACTIONS,
    "runner_hooks":   PRO_RUNNER_HOOKS,
    "cli_commands":   PRO_CLI_COMMANDS,
}

_KIND_LABEL = {
    "transports":     "Transport",
    "runner_actions": "Campaign action",
    "runner_hooks":   "Runner mode",
    "cli_commands":   "Command",
}


def pro_missing_message(kind: str, name: str) -> str:
    """The one consistent message for a paid feature that is not available.

    A feature can be unavailable for two very different reasons, and saying
    the wrong one costs a paying customer an afternoon: pro is genuinely not
    installed (→ buy/install it), or pro IS installed but failed to load
    (→ fix the install; most often a core/pro version mismatch). Never tell
    someone to buy what they already own.
    """
    _discover()
    label = _KIND_LABEL.get(kind, "Feature")
    if name in _PRO_NAMES.get(kind, ()):
        if _load_error is not None:
            return (
                f"{label} '{name}' is provided by Xaloqi TestLab Pro, which is "
                f"installed but failed to load:\n"
                f"  {_load_error}\n"
                f"Fix the installation, then retry — support: {PRO_URL}"
            )
        return (
            f"{label} '{name}' is part of Xaloqi TestLab Pro — {PRO_URL}\n"
            f"Install the xaloqi-tester-pro wheel from your TestLab ZIP to enable it."
        )
    return f"{label} '{name}' is not available in this installation."


# Registry cache: kind → {name: loaded object}. Populated lazily.
_registry: Dict[str, Dict[str, Any]] = {}
_discovered = False
# Set when pro is present but could not be loaded (e.g. version lockstep
# mismatch). Distinguishes "not installed" from "broken install" — see
# pro_missing_message. Silent absence here is the run-008 failure class.
_load_error: Optional[str] = None


def _discover() -> None:
    """Populate the registry from entry points, once.

    Enumerating entry points can fail on broken dist metadata — that is
    tolerated, and the direct-import fallback still runs. A plugin that IS
    advertised but fails to load is NOT tolerated silently: the error is
    recorded in _load_error so the user is told what actually went wrong.
    """
    global _discovered, _load_error
    if _discovered:
        return
    _discovered = True
    for kind in _GROUPS:
        _registry.setdefault(kind, {})

    from importlib.metadata import entry_points
    try:
        found = [(kind, ep) for kind, group in _GROUPS.items()
                 for ep in entry_points(group=group)]
    except Exception:
        # Broken dist metadata — fall through to the direct-import path.
        found = []

    for kind, ep in found:
        if ep.name in _registry[kind]:
            continue
        try:
            _registry[kind][ep.name] = ep.load()
        except Exception as exc:
            if _load_error is None:
                _load_error = f"{type(exc).__name__}: {exc}"

    # Fallback: repo checkouts / stale editable metadata. If pro is importable
    # but its entry points were not discovered, let it register directly.
    if not any(_registry[k] for k in _registry):
        try:
            import xaloqi_tester_pro
        except ImportError as exc:
            # A ModuleNotFoundError naming pro itself means pro is genuinely
            # not installed — the expected free-tier case, stay quiet.
            # Anything else (a failed import inside pro, or its version
            # lockstep assertion) means a broken install: record it.
            not_installed = (
                isinstance(exc, ModuleNotFoundError)
                and getattr(exc, "name", None) == "xaloqi_tester_pro"
            )
            if not not_installed and _load_error is None:
                _load_error = f"{type(exc).__name__}: {exc}"
            return
        register_all = getattr(xaloqi_tester_pro, "register_plugins", None)
        if callable(register_all):
            try:
                register_all(_registry)
            except Exception as exc:
                if _load_error is None:
                    _load_error = f"{type(exc).__name__}: {exc}"


def register(kind: str, name: str, obj: Any) -> None:
    """Direct registration API (used by the fallback path and by tests)."""
    if kind not in _GROUPS:
        raise ValueError(f"Unknown plugin kind '{kind}'. Valid: {sorted(_GROUPS)}")
    _registry.setdefault(kind, {})[name] = obj


def reset() -> None:
    """Forget everything discovered (test isolation helper)."""
    global _discovered, _load_error
    _discovered = False
    _load_error = None
    _registry.clear()


def _get(kind: str, name: str) -> Optional[Any]:
    _discover()
    return _registry.get(kind, {}).get(name)


def names(kind: str) -> frozenset:
    """All discovered names for a plugin kind."""
    _discover()
    return frozenset(_registry.get(kind, {}))


def get_transport(name: str) -> Callable[..., Any]:
    from .exceptions import TransportError
    factory = _get("transports", name)
    if factory is None:
        raise TransportError(pro_missing_message("transports", name))
    return factory


def get_runner_actions() -> Dict[str, Any]:
    """All discovered action handlers: name → async handler(executor, step, index)."""
    _discover()
    return dict(_registry.get("runner_actions", {}))


def get_runner_hook(name: str) -> Optional[Any]:
    return _get("runner_hooks", name)


def get_cli_commands() -> Dict[str, Any]:
    """All discovered testlab subcommands: name → register(subparsers)."""
    _discover()
    return dict(_registry.get("cli_commands", {}))
