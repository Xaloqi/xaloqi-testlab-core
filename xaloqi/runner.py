#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
=============================================================================
Xaloqi TestLab
FILE: xaloqi/runner.py  (CLI: testlab-run; legacy shim: tools/runner.py)

PURPOSE: Campaign runner — execute YAML-defined UDS test campaigns against
         a live or simulated ECU using the xaloqi-tester library.

         YAML schema is compatible with Xaloqi EDS jobrunner.py (IDEA-032).
         The same campaign YAML runs against both EDS jobrunner and TestLab
         runner — same vocabulary, same JSON output format.

         SOME/IP and SOVD actions, multi-ECU --workspace mode, and --rtos
         comparison are provided by xaloqi-tester-pro and discovered through
         the plugin seam (xaloqi.tester._plugins). Without pro installed
         they fail with a consistent "part of Xaloqi TestLab Pro" message.

USAGE:
    # List campaigns in a config
    testlab-run \
        --config core/examples/diagnostics_config.yaml \
        --list

    # Run a campaign against the Docker Compose ECU simulator (EDS config)
    testlab-run \
        --config core/examples/diagnostics_config.yaml \
        --campaign core/campaigns/basic_validation.yaml \
        --job eol_production_check \
        --interface vcan0 \
        --json reports/run_001.json

    # Dry-run: validate schema without connecting
    testlab-run \
        --config core/examples/diagnostics_config.yaml \
        --campaign core/campaigns/basic_validation.yaml \
        --dry-run

    # Run with standalone testlab_config.yaml (no EDS required)
    testlab-run \
        --config core/testlab_config.yaml \
        --campaign core/campaigns/standalone_validation.yaml \
        --job     basic_validation \
        --interface vcan0 \
        --json    reports/run.json

    # Run against VirtualBus (no hardware, for CI)
    testlab-run \
        --config core/examples/diagnostics_config.yaml \
        --campaign core/campaigns/basic_validation.yaml \
        --job eol_production_check \
        --virtual \
        --json reports/ci_run.json

    # RTOS comparison — run the same job on two CAN interfaces, diff the results
    testlab-run \
        --config   core/examples/diagnostics_config.yaml \
        --campaign core/campaigns/basic_validation.yaml \
        --job      eol_production_check \
        --rtos     zephyr,freertos \
        --rtos-interfaces vcan0,vcan1 \
        --rtos-out reports/rtos_diff.html

JSON OUTPUT CONTRACT (schema_version: 1):
    Identical to EDS jobrunner.py --json output. Consumable by testlab.py
    analyze / trend / report / compare commands.

DEPENDENCIES:
    xaloqi-tester (pip install xaloqi-tester)
    pyyaml >= 6.0

=============================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# xaloqi-tester imports
try:
    from xaloqi.tester import (
        UdsTester, Session, ResetType, RoutineControl,
        NrcError, TimeoutError as UdsTimeoutError, TransportError,
    )
    from xaloqi.tester import _plugins
    from xaloqi.tester._config import load_config
    from xaloqi.tester._security import derive_key
    from xaloqi.tester.transport.virtual import VirtualBus
except ImportError as _e:
    print(f"ERROR: xaloqi-tester not found: {_e}", file=sys.stderr)
    print("       Run: pip install xaloqi-tester", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

RUNNER_VERSION      = "1.5.2"
JSON_SCHEMA_VERSION = 1


def check_library_version() -> None:
    """Abort if the imported xaloqi library is not the one this CLI ships with.

    `import xaloqi` resolves from site-packages (or whatever sys.path finds
    first). A stale install — e.g. `pip install -e .` left behind by a
    previous TestLab extraction directory — silently runs this CLI against an
    older library and an older ECU simulator: campaigns pass, but the code
    under test is not the code you extracted (TestLab#28, lessons/run-008).
    Fail loudly instead.
    """
    import xaloqi
    lib_version = getattr(xaloqi, "__version__", None)
    if lib_version == RUNNER_VERSION:
        return
    print(
        f"ERROR: xaloqi library version mismatch.\n"
        f"       runner.py is v{RUNNER_VERSION}, but 'import xaloqi' resolved\n"
        f"       v{lib_version or 'unknown (pre-1.4.1)'} from:\n"
        f"         {xaloqi.__file__}\n"
        f"       You are probably running against a stale install from a\n"
        f"       previous TestLab directory. Fix:\n"
        f"         pip uninstall xaloqi-tester\n"
        f"         pip install -e <this TestLab directory>",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Colour output
# ---------------------------------------------------------------------------

RED    = "\033[31m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

USE_COLOUR = sys.stdout.isatty()


def col(c: str, s: str) -> str:
    return (c + s + RESET) if USE_COLOUR else s

# ---------------------------------------------------------------------------
# NRC reference (mirrors EDS jobrunner.py)
# ---------------------------------------------------------------------------

NRC_NAMES: Dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
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

SESSION_MAP = {
    "default":     Session.DEFAULT,
    "extended":    Session.EXTENDED,
    "programming": Session.PROGRAMMING,
}

# The 20 built-in (free) actions. SOME/IP and SOVD actions are registered by
# xaloqi-tester-pro through the xaloqi_tester.runner_actions entry-point group.
VALID_ACTIONS = frozenset({
    "session", "security_access", "read_did", "write_did",
    "read_memory_by_address", "write_memory_by_address",
    "read_dtc", "clear_dtc", "read_dtc_fault_counter", "read_dtc_permanent",
    "routine", "request_download", "request_upload",
    "transfer_data", "request_transfer_exit", "ecu_reset",
    "tester_present", "delay", "assert", "foreach_did",
})


def known_actions() -> frozenset:
    """Built-in actions plus any registered by installed plugins (pro)."""
    return VALID_ACTIONS | frozenset(_plugins.get_runner_actions())

# ---------------------------------------------------------------------------
# Data classes (identical schema to EDS jobrunner.py)
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    index:        int
    action:       str
    params:       Dict[str, Any]
    success:      bool
    skipped:      bool          = False  # executed=False, not a failure (e.g. SOVD no-op)
    duration_ms:  float         = 0.0
    request_pdu:  Optional[str] = None
    response_pdu: Optional[str] = None
    nrc:          Optional[int] = None
    nrc_name:     Optional[str] = None
    saved_var:    Optional[str] = None
    error:        Optional[str] = None
    ecu:          Optional[str] = None   # ECU name for workspace runs; None for single-ECU

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("saved_var", None)
        return d


@dataclass
class JobResult:
    schema_version: int
    job_name:       str
    config_path:    str
    ecu_name:       str
    ecu_version:    str
    started_at:     str
    finished_at:    str
    duration_ms:    float
    success:        bool
    steps:          List[StepResult]
    variables:      Dict[str, str]
    summary:        str
    workspace_path: Optional[str] = None  # set when run via --workspace

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        return d

# ---------------------------------------------------------------------------
# Schema validation (dry-run)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Allowed step keys (#4)
#
# The runner used to ignore any key it did not recognise, which for a test
# tool is a false-pass: misspelling `expect_nrc` as `expect_ncr` silently
# removed the assertion and the step still reported OK. Campaigns could be
# green while asserting nothing.
#
# Keys accepted on EVERY step, handled in the dispatch loop rather than in a
# per-action handler.
# ---------------------------------------------------------------------------

COMMON_STEP_KEYS = frozenset({
    "action",       # which action to run
    "ecu",          # workspace ECU selector
    "expect_nrc",   # step passes iff the ECU returns exactly this NRC
    "description",  # free-text, for humans reading the campaign
})

# Per-action keys, mirroring exactly what each _step_* handler reads.
# Keep in sync when an action gains a parameter -- the test
# tests/test_step_key_validation.py::test_table_matches_handlers asserts this.
ACTION_STEP_KEYS: Dict[str, frozenset] = {
    "session":                 frozenset({"value"}),
    "security_access":         frozenset({"level"}),
    "read_did":                frozenset({"did", "expect_ok", "save_as"}),
    "write_did":               frozenset({"did", "data"}),
    "read_memory_by_address":  frozenset({"address", "address_len", "size", "size_len", "save_as"}),
    "write_memory_by_address": frozenset({"address", "address_len", "size_len", "data"}),
    "read_dtc":                frozenset({"save_as", "dtc_count"}),
    "clear_dtc":               frozenset({"group"}),
    "read_dtc_fault_counter":  frozenset({"save_as"}),
    "read_dtc_permanent":      frozenset({"save_as", "dtc_count"}),
    "routine":                 frozenset({"id", "sub_fn", "save_as"}),
    "request_download":        frozenset({"memory_address", "memory_size"}),
    "request_upload":          frozenset({"memory_address", "memory_size"}),
    "transfer_data":           frozenset({"file", "memory_address"}),
    "request_transfer_exit":   frozenset(),
    "ecu_reset":               frozenset({"reset_type", "wait_ms"}),
    "tester_present":          frozenset({"suppress"}),
    "delay":                   frozenset({"ms"}),
    "assert":                  frozenset({"variable", "length", "contains", "not_nrc"}),
    "foreach_did":             frozenset({"min_session", "expect_ok", "save_results"}),
}


def allowed_step_keys(action: str) -> Optional[frozenset]:
    """Keys this action accepts, or None if the action is unknown here.

    Returns None for plugin (pro) actions so their steps are not rejected by
    a table that cannot know their parameters.
    """
    if action not in ACTION_STEP_KEYS:
        return None
    return COMMON_STEP_KEYS | ACTION_STEP_KEYS[action]


def _suggest_key(unknown: str, allowed: frozenset) -> str:
    """'did you mean' for a near-miss, which is the common case (#4)."""
    import difflib
    close = difflib.get_close_matches(unknown, sorted(allowed), n=1, cutoff=0.7)
    return f" Did you mean '{close[0]}'?" if close else ""


def validate_job_schema(
    job_name: str,
    job_def: dict,
    known_ecus: Optional[set] = None,
) -> List[str]:
    errors = []
    if not isinstance(job_def, dict):
        return [f"{job_name}: must be a mapping"]

    steps = job_def.get("steps", [])
    if not isinstance(steps, list) or len(steps) == 0:
        errors.append(f"{job_name}: 'steps' must be a non-empty list")
        return errors

    on_failure = job_def.get("on_failure", "abort")
    if on_failure not in ("abort", "continue"):
        errors.append(
            f"{job_name}: on_failure must be 'abort' or 'continue', got '{on_failure}'"
        )

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"{job_name}.steps[{i}]: must be a mapping")
            continue
        action = step.get("action")
        valid = known_actions()
        if not action:
            errors.append(f"{job_name}.steps[{i}]: missing 'action' field")
        elif action not in valid:
            if action in _plugins.PRO_ACTIONS:
                errors.append(
                    f"{job_name}.steps[{i}]: "
                    + _plugins.pro_missing_message("runner_actions", action)
                )
            else:
                errors.append(
                    f"{job_name}.steps[{i}]: unknown action '{action}'. "
                    f"Valid: {sorted(valid)}"
                )
        else:
            if action == "session":
                v = step.get("value")
                if v not in SESSION_MAP:
                    errors.append(
                        f"{job_name}.steps[{i}] (session): value must be one of "
                        f"{list(SESSION_MAP.keys())}, got '{v}'"
                    )
            elif action == "security_access":
                lvl = step.get("level")
                if not isinstance(lvl, int) or lvl < 1:
                    errors.append(
                        f"{job_name}.steps[{i}] (security_access): "
                        f"'level' must be a positive integer"
                    )
            elif action in ("read_did", "write_did"):
                did = step.get("did")
                if not did or not re.match(r"^0x[0-9A-Fa-f]{4}$", str(did)):
                    errors.append(
                        f"{job_name}.steps[{i}] ({action}): "
                        f"'did' must be a 4-digit hex string (e.g. 0xF190)"
                    )
        # Unknown / misspelled step keys (#4). A key the runner does not
        # consume used to be silently discarded, so a typo'd assertion
        # removed the assertion and the step still passed.
        if action:
            allowed = allowed_step_keys(action)
            if allowed is not None:
                for key in sorted(set(step) - allowed):
                    errors.append(
                        f"{job_name}.steps[{i}] ({action}): unknown key "
                        f"'{key}'.{_suggest_key(key, allowed)} "
                        f"Valid keys: {sorted(allowed)}"
                    )

        # Workspace ecu: field validation
        ecu_name = step.get("ecu")
        if ecu_name is not None and known_ecus is not None:
            if ecu_name not in known_ecus:
                errors.append(
                    f"{job_name}.steps[{i}]: 'ecu: {ecu_name}' not found in workspace. "
                    f"Known ECUs: {sorted(known_ecus)}"
                )
    return errors


def dry_run_config(
    config: dict,
    job_filter: Optional[str] = None,
    workspace_config=None,
) -> int:
    """
    Validate job schema without connecting to any ECU.

    Args:
        config:           Merged config dict (diagnostics + campaign).
        job_filter:       If set, only validate this job name.
        workspace_config: WorkspaceConfig instance; when provided, step 'ecu:'
                          fields are validated against the workspace ECU names.
    """
    jobs = config.get("jobs", {})
    if not jobs:
        print(col(YELLOW, "WARNING: no 'jobs:' block found in config"))
        return 0

    targets = {job_filter: jobs[job_filter]} if job_filter and job_filter in jobs else jobs
    all_errors: List[str] = []

    known_ecus = (
        {e.name for e in workspace_config.ecus} if workspace_config is not None else None
    )
    for name, defn in targets.items():
        all_errors.extend(validate_job_schema(name, defn, known_ecus=known_ecus))

    if all_errors:
        print(col(RED, "FAIL") + "  Schema validation errors:")
        for err in all_errors:
            print(f"       {err}")
        return 1

    for name in targets:
        step_count = len(targets[name].get("steps", []))
        print(f"  {col(GREEN, 'OK')}   {name} ({step_count} steps)")
    return 0

# ---------------------------------------------------------------------------
# Variable interpolation
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate(value: Any, variables: Dict[str, bytes]) -> Any:
    if not isinstance(value, str):
        return value
    m = _VAR_RE.fullmatch(value)
    if m:
        name = m.group(1)
        if name not in variables:
            raise ValueError(f"Undefined variable '${{{name}}}'")
        return variables[name]

    def _sub(match):
        name = match.group(1)
        if name not in variables:
            raise ValueError(f"Undefined variable '${{{name}}}'")
        return variables[name].hex()

    return _VAR_RE.sub(_sub, value)

# ---------------------------------------------------------------------------
# PDU helpers
# ---------------------------------------------------------------------------

def _hex(data: Optional[bytes]) -> Optional[str]:
    return data.hex().upper() if data else None


def _parse_hex(s: str) -> bytes:
    s = s.strip()
    if s.startswith(("0x", "0X")):
        s = s[2:]
    return bytes.fromhex(s.replace(" ", ""))


def _did_int(did_str: str) -> int:
    return int(did_str, 16)


def _parse_nrc(raw) -> int:
    """Parse an expect_nrc value from YAML — accepts int (0x7F → 127) or hex string '0x7F'."""
    if isinstance(raw, int):
        return raw
    return int(str(raw), 16)

# ---------------------------------------------------------------------------
# Campaign executor
# ---------------------------------------------------------------------------

class CampaignExecutor:
    """
    Executes a single named job from a campaign YAML file.
    All UDS operations go through UdsTester from xaloqi-tester.

    Supports multi-ECU workspaces: when `testers` is provided, campaign steps
    may specify 'ecu: <name>' to target a specific ECU. Steps without 'ecu:'
    target the default tester.
    """

    def __init__(
        self,
        config: dict,
        config_path: str,
        tester: UdsTester,
        verbose: bool = False,
        testers: Optional[Dict[str, UdsTester]] = None,
        someip_virtual: bool = False,
        someip_host_override: Optional[str] = None,
    ) -> None:
        self.config      = config
        self.config_path = config_path
        self.tester      = tester
        self.verbose     = verbose
        self.variables: Dict[str, bytes] = {}
        # Multi-ECU workspace support: name -> UdsTester
        self._testers: Dict[str, UdsTester] = testers or {}
        self._workspace_path: Optional[str] = None  # set by the workspace hook
        self._workspace_config = None               # set by the workspace hook
        self._someip_virtual = someip_virtual
        self._someip_host_override = someip_host_override
        # SOVD base URL from config section (step or workspace EcuDef may override).
        # Also drives the free skip semantics in _is_sovd_transport.
        self._sovd_base_url: str = config.get("sovd", {}).get("base_url", "")
        # Plugin scratch space. Pro action handlers keep per-job resources here
        # (e.g. SOME/IP bus pools) and append an async close callback to
        # plugin_closers for each resource that must be closed at job end.
        self.plugin_state: Dict[str, Any] = {}
        self.plugin_closers: List[Any] = []

    def resolve_tester(self, step: dict) -> UdsTester:
        """Public alias of _resolve_tester for plugin action handlers."""
        return self._resolve_tester(step)

    def _resolve_tester(self, step: dict) -> UdsTester:
        """Return the UdsTester for this step, honouring the 'ecu:' field."""
        ecu_name = step.get("ecu")
        if ecu_name and self._testers:
            if ecu_name not in self._testers:
                raise ValueError(
                    f"Step references unknown ECU '{ecu_name}'. "
                    f"Available in workspace: {list(self._testers.keys())}"
                )
            return self._testers[ecu_name]
        return self.tester

    async def execute_job(self, job_name: str) -> JobResult:
        """Execute a named job and return JobResult regardless of outcome."""
        # Find job in campaign config, then fall back to diagnostics_config jobs
        jobs = self.config.get("jobs", {})
        if job_name not in jobs:
            available = list(jobs.keys())
            raise ValueError(
                f"Job '{job_name}' not found. Available: {available}"
            )

        job_def    = jobs[job_name]
        steps      = job_def.get("steps", [])
        on_fail    = job_def.get("on_failure", "abort")
        meta       = self.config.get("metadata", {})

        started_at   = datetime.now(timezone.utc).isoformat()
        step_results = []
        job_ok       = True
        self.variables = {}

        print()
        print(col(BOLD, "─" * 65))
        print(f"  Config:  {self.config_path}")
        print(f"  ECU:     {meta.get('ecu_name', '?')} v{meta.get('version', '?')}")
        print(f"  Job:     {job_name}")
        desc = job_def.get("description", "")
        if desc:
            print(f"  Desc:    {desc.strip()}")
        print(col(BOLD, "─" * 65))
        print()

        t_job_start = time.monotonic()

        for i, step_def in enumerate(steps):
            label = self._step_label(step_def)
            ecu_tag = ""
            if self._testers and step_def.get("ecu"):
                ecu_tag = f"[{step_def['ecu']}] "
            print(f"  [{i+1:02d}/{len(steps):02d}] {ecu_tag}{label:<35}", end="", flush=True)

            t_start = time.monotonic()
            result  = await self._execute_step(step_def, i + 1)
            result.duration_ms = (time.monotonic() - t_start) * 1000

            step_results.append(result)
            self._print_outcome(result)

            if not result.success:
                job_ok = False
                if on_fail == "abort":
                    print()
                    print(f"  {col(RED, 'ABORTED')} at step {i+1} (on_failure: abort)")
                    break

        job_duration_ms = (time.monotonic() - t_job_start) * 1000
        finished_at     = datetime.now(timezone.utc).isoformat()

        # Close any plugin-held resources opened during this job
        # (SOME/IP buses, SOVD clients — registered by pro action handlers)
        for closer in self.plugin_closers:
            try:
                await closer()
            except Exception:
                pass
        self.plugin_closers.clear()
        self.plugin_state.clear()

        passed_count  = sum(1 for s in step_results if s.success and not s.skipped)
        skipped_count = sum(1 for s in step_results if s.skipped)
        summary       = f"{passed_count}/{len(step_results)} steps passed"
        if skipped_count:
            summary += f", {skipped_count} skipped (not executed)"

        print()
        print(col(BOLD, "─" * 65))
        outcome = col(GREEN, "PASS") if job_ok else col(RED, "FAIL")
        print(f"  Result:  {outcome}")
        print(f"  Steps:   {summary}")
        print(f"  Time:    {job_duration_ms:.0f} ms")
        print(col(BOLD, "─" * 65))
        print()

        return JobResult(
            schema_version = JSON_SCHEMA_VERSION,
            job_name       = job_name,
            config_path    = self.config_path,
            ecu_name       = meta.get("ecu_name", ""),
            ecu_version    = meta.get("version", ""),
            started_at     = started_at,
            finished_at    = finished_at,
            duration_ms    = round(job_duration_ms, 2),
            success        = job_ok,
            steps          = step_results,
            variables      = {k: v.hex() for k, v in self.variables.items()},
            summary        = summary,
            workspace_path = self._workspace_path,
        )

    async def _execute_step(self, step: dict, index: int) -> StepResult:
        action = step.get("action", "")
        params = {k: v for k, v in step.items() if k != "action"}

        dispatch = {
            "session":               self._step_session,
            "security_access":       self._step_security_access,
            "read_did":                  self._step_read_did,
            "write_did":                 self._step_write_did,
            "read_memory_by_address":    self._step_read_memory_by_address,
            "write_memory_by_address":   self._step_write_memory_by_address,
            "read_dtc":                  self._step_read_dtc,
            "clear_dtc":             self._step_clear_dtc,
            "read_dtc_fault_counter": self._step_read_dtc_fault_counter,
            "read_dtc_permanent":    self._step_read_dtc_permanent,
            "routine":               self._step_routine,
            "request_download":      self._step_request_download,
            "request_upload":        self._step_request_upload,
            "transfer_data":         self._step_transfer_data,
            "request_transfer_exit": self._step_transfer_exit,
            "ecu_reset":             self._step_ecu_reset,
            "tester_present":        self._step_tester_present,
            "delay":                 self._step_delay,
            "assert":                self._step_assert,
            "foreach_did":           self._step_foreach_did,
        }

        handler = dispatch.get(action)
        plugin_handler = None
        if handler is None:
            # SOME/IP, SOVD and future plugin actions:
            # async handler(executor, step, index) -> StepResult
            plugin_handler = _plugins.get_runner_actions().get(action)
        if handler is None and plugin_handler is None:
            if action in _plugins.PRO_ACTIONS:
                error = _plugins.pro_missing_message("runner_actions", action)
            else:
                error = f"Unknown action: '{action}'"
            return StepResult(
                index=index, action=action, params=params,
                success=False, error=error,
            )
        ecu_name_for_step: Optional[str] = step.get("ecu") if self._testers else None

        try:
            if plugin_handler is not None:
                result = await plugin_handler(self, step, index)
            else:
                result = await handler(step, index)
            if ecu_name_for_step is not None:
                result.ecu = ecu_name_for_step

            # expect_nrc: step passes iff the ECU returned exactly this NRC code.
            # Takes precedence over expect_ok when both are present.
            expect_nrc_raw = step.get("expect_nrc")
            if expect_nrc_raw is not None:
                expected_nrc = _parse_nrc(expect_nrc_raw)
                if result.nrc is not None:
                    matched = result.nrc == expected_nrc
                    result.success = matched
                    result.error = (
                        None if matched
                        else f"Expected NRC 0x{expected_nrc:02X}, got NRC 0x{result.nrc:02X} ({result.nrc_name or ''})"
                    )
                elif result.success:
                    result.success = False
                    result.error = f"Expected NRC 0x{expected_nrc:02X} but step produced a positive response"
                else:
                    result.success = False
                    if not result.error:
                        result.error = f"Expected NRC 0x{expected_nrc:02X} but step produced no response"

            return result
        except Exception as exc:
            return StepResult(
                index=index, action=action, params=params,
                success=False, error=f"{type(exc).__name__}: {exc}",
                ecu=ecu_name_for_step,
            )

    # ── Step handlers ────────────────────────────────────────────────────────

    def _is_sovd_transport(self) -> bool:
        """True when the config declares this ECU uses SOVD transport."""
        return (
            bool(self._sovd_base_url)
            or self.config.get("ecu", {}).get("transport") == "sovd"
        )

    async def _step_session(self, step: dict, index: int) -> StepResult:
        value   = step.get("value", "default")
        if self._is_sovd_transport():
            # Session negotiation is not applicable over SOVD REST.
            # skipped=True: the step was NOT executed — reporting it as a
            # plain pass would let a campaign "validate" session control
            # vacuously (validation report Bug 9).
            return StepResult(
                index=index, action="session",
                params={"value": value}, success=True, skipped=True,
                error="[SOVD] session step is not applicable over SOVD transport — skipped",
            )
        session = SESSION_MAP.get(value)
        if session is None:
            return StepResult(
                index=index, action="session",
                params={"value": value}, success=False,
                error=f"Unknown session value '{value}'",
            )
        try:
            resp = await self._resolve_tester(step).session(session)
            return StepResult(
                index=index, action="session",
                params={"value": value}, success=True,
                response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="session",
                params={"value": value}, success=False,
                nrc=e.nrc, nrc_name=e.name,
                error=str(e),
            )

    async def _step_security_access(self, step: dict, index: int) -> StepResult:
        level = step.get("level", 1)
        if self._is_sovd_transport():
            # SecurityAccess challenges are not applicable over SOVD REST.
            # skipped=True: the step was NOT executed — a campaign
            # "validating" security unlock over SOVD must not pass vacuously
            # (validation report Bug 9).
            return StepResult(
                index=index, action="security_access",
                params={"level": level}, success=True, skipped=True,
                error="[SOVD] security_access step is not applicable over SOVD transport — skipped",
            )
        try:
            resp = await self._resolve_tester(step).security_access(level=level)
            return StepResult(
                index=index, action="security_access",
                params={"level": level}, success=True,
                response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="security_access",
                params={"level": level}, success=False,
                nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_read_did(self, step: dict, index: int) -> StepResult:
        did_str   = step.get("did", "")
        expect_ok = step.get("expect_ok", True)
        save_as   = step.get("save_as")
        did_id    = _did_int(did_str)

        pdu_bytes = bytes([0x22, (did_id >> 8) & 0xFF, did_id & 0xFF])

        try:
            resp = await self._resolve_tester(step).read_did(did_id)
            if save_as and expect_ok:
                self.variables[save_as] = resp.data
            return StepResult(
                index=index, action="read_did",
                params={"did": did_str, "expect_ok": expect_ok},
                success=expect_ok,
                request_pdu=_hex(pdu_bytes),
                response_pdu=_hex(resp.raw),
                saved_var=save_as if expect_ok else None,
                error=(
                    None if expect_ok
                    else f"Expected DID 0x{did_id:04X} read to fail (expect_ok: false)"
                         f" but ECU returned a positive response"
                ),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="read_did",
                params={"did": did_str, "expect_ok": expect_ok},
                success=not expect_ok,
                request_pdu=_hex(pdu_bytes),
                nrc=e.nrc, nrc_name=e.name,
                error=None if not expect_ok else str(e),
            )

    async def _step_write_did(self, step: dict, index: int) -> StepResult:
        did_str = step.get("did", "")
        data    = step.get("data", "")
        did_id  = _did_int(did_str)

        resolved = interpolate(data, self.variables)
        data_bytes = resolved if isinstance(resolved, bytes) else _parse_hex(str(resolved))

        pdu_bytes = bytes([0x2E, (did_id >> 8) & 0xFF, did_id & 0xFF]) + data_bytes

        try:
            resp = await self._resolve_tester(step).write_did(did_id, data_bytes)
            return StepResult(
                index=index, action="write_did",
                params={"did": did_str, "data_length": len(data_bytes)},
                success=True,
                request_pdu=_hex(pdu_bytes),
                response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="write_did",
                params={"did": did_str, "data_length": len(data_bytes)},
                success=False,
                request_pdu=_hex(pdu_bytes),
                nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_read_memory_by_address(self, step: dict, index: int) -> StepResult:
        addr_raw    = step.get("address", "0x00000000")
        size_raw    = step.get("size", 0)
        address_len = int(step.get("address_len", 4))
        size_len    = int(step.get("size_len", 4))
        save_as     = step.get("save_as")
        address     = int(str(addr_raw), 0) if isinstance(addr_raw, str) else int(addr_raw)
        size        = int(size_raw)

        try:
            resp = await self._resolve_tester(step).read_memory_by_address(
                address, size, address_len, size_len,
            )
            if save_as:
                self.variables[save_as] = resp.data
            return StepResult(
                index=index, action="read_memory_by_address",
                params={"address": hex(address), "size": size},
                success=True, response_pdu=_hex(resp.raw),
                saved_var=save_as,
            )
        except NrcError as e:
            return StepResult(
                index=index, action="read_memory_by_address",
                params={"address": hex(address), "size": size},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_write_memory_by_address(self, step: dict, index: int) -> StepResult:
        addr_raw    = step.get("address", "0x00000000")
        data_raw    = step.get("data", "")
        address_len = int(step.get("address_len", 4))
        size_len    = int(step.get("size_len", 4))
        address     = int(str(addr_raw), 0) if isinstance(addr_raw, str) else int(addr_raw)

        resolved   = interpolate(data_raw, self.variables)
        data_bytes = resolved if isinstance(resolved, bytes) else _parse_hex(str(resolved))

        try:
            resp = await self._resolve_tester(step).write_memory_by_address(
                address, data_bytes, address_len, size_len,
            )
            return StepResult(
                index=index, action="write_memory_by_address",
                params={"address": hex(address), "size": len(data_bytes)},
                success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="write_memory_by_address",
                params={"address": hex(address), "size": len(data_bytes)},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_read_dtc(self, step: dict, index: int) -> StepResult:
        save_as = step.get("save_as")
        expect_count = step.get("dtc_count")
        try:
            resp = await self._resolve_tester(step).read_dtcs()
            if save_as:
                self.variables[save_as] = resp.raw
            # dtc_count is an assertion, not just a reported figure (#4): it
            # used to be written into params and never compared, so
            # `dtc_count: 999` passed against an ECU reporting none.
            count = len(resp.dtcs)
            mismatch = expect_count is not None and count != expect_count
            return StepResult(
                index=index, action="read_dtc",
                params={"dtc_count": count},
                success=not mismatch,
                response_pdu=_hex(resp.raw),
                saved_var=save_as,
                error=(
                    f"Expected {expect_count} DTC(s), ECU reported {count}"
                    if mismatch else None
                ),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="read_dtc", params={},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_clear_dtc(self, step: dict, index: int) -> StepResult:
        group_str = step.get("group", "0xFFFFFF")
        group     = int(group_str, 16) if isinstance(group_str, str) else group_str
        try:
            resp = await self._resolve_tester(step).clear_dtcs(group=group)
            return StepResult(
                index=index, action="clear_dtc",
                params={"group": group_str}, success=True,
                response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="clear_dtc",
                params={"group": group_str}, success=False,
                nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_read_dtc_fault_counter(self, step: dict, index: int) -> StepResult:
        save_as = step.get("save_as")
        try:
            resp = await self._resolve_tester(step).read_dtcs_fault_counter()
            if save_as:
                self.variables[save_as] = resp.raw
            return StepResult(
                index=index, action="read_dtc_fault_counter",
                params={"record_count": len(resp.records)},
                success=True,
                response_pdu=_hex(resp.raw),
                saved_var=save_as,
            )
        except NrcError as e:
            return StepResult(
                index=index, action="read_dtc_fault_counter", params={},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_read_dtc_permanent(self, step: dict, index: int) -> StepResult:
        save_as = step.get("save_as")
        expect_count = step.get("dtc_count")
        try:
            resp = await self._resolve_tester(step).read_dtcs_permanent()
            if save_as:
                self.variables[save_as] = resp.raw
            count = len(resp.dtcs)
            mismatch = expect_count is not None and count != expect_count
            return StepResult(
                index=index, action="read_dtc_permanent",
                params={"dtc_count": count},
                success=not mismatch,
                response_pdu=_hex(resp.raw),
                saved_var=save_as,
                error=(
                    f"Expected {expect_count} permanent DTC(s), ECU reported {count}"
                    if mismatch else None
                ),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="read_dtc_permanent", params={},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_routine(self, step: dict, index: int) -> StepResult:
        rid_str  = step.get("id", "0xFF00")
        sub_fn   = step.get("sub_fn", "start")
        save_as  = step.get("save_as")
        rid      = int(rid_str, 16)

        ctrl_map = {"start": RoutineControl.START, "stop": RoutineControl.STOP,
                    "requestResults": RoutineControl.REQUEST_RESULT}
        ctrl = ctrl_map.get(sub_fn, RoutineControl.START)

        try:
            resp = await self._resolve_tester(step).routine_control(rid, ctrl)
            if save_as and resp.status_record:
                self.variables[save_as] = resp.status_record
            return StepResult(
                index=index, action="routine",
                params={"id": rid_str, "sub_fn": sub_fn},
                success=True, response_pdu=_hex(resp.raw), saved_var=save_as,
            )
        except NrcError as e:
            return StepResult(
                index=index, action="routine",
                params={"id": rid_str, "sub_fn": sub_fn},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_request_download(self, step: dict, index: int) -> StepResult:
        addr = int(step.get("memory_address", "0x00000000"), 0)
        size_raw = step.get("memory_size", "0")
        resolved = interpolate(size_raw, self.variables)
        size = int.from_bytes(resolved, "big") if isinstance(resolved, bytes) else int(str(resolved), 0)

        try:
            resp = await self._resolve_tester(step).request_download(addr, size)
            return StepResult(
                index=index, action="request_download",
                params={"memory_address": hex(addr), "memory_size": size},
                success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="request_download",
                params={"memory_address": hex(addr), "memory_size": size},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_request_upload(self, step: dict, index: int) -> StepResult:
        addr = int(step.get("memory_address", "0x00000000"), 0)
        size_raw = step.get("memory_size", "0")
        resolved = interpolate(size_raw, self.variables)
        size = int.from_bytes(resolved, "big") if isinstance(resolved, bytes) else int(str(resolved), 0)

        try:
            resp = await self._resolve_tester(step).request_upload(addr, size)
            return StepResult(
                index=index, action="request_upload",
                params={"memory_address": hex(addr), "memory_size": size},
                success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="request_upload",
                params={"memory_address": hex(addr), "memory_size": size},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_transfer_data(self, step: dict, index: int) -> StepResult:
        file_path = step.get("file", "")
        if not file_path or not os.path.exists(file_path):
            return StepResult(
                index=index, action="transfer_data",
                params={"file": file_path}, success=False,
                error=f"Firmware file not found: '{file_path}'",
            )

        addr = int(step.get("memory_address", "0x00000000"), 0)
        firmware = Path(file_path).read_bytes()
        block_size = self.config.get("safeboot", {}).get("max_block_length", 0xFFF)

        try:
            resp = await self._resolve_tester(step).transfer_firmware(
                data=firmware,
                memory_address=addr,
                block_size=block_size,
            )
            return StepResult(
                index=index, action="transfer_data",
                params={"file": file_path, "memory_address": hex(addr), "total_bytes": len(firmware)},
                success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="transfer_data",
                params={"file": file_path, "memory_address": hex(addr), "total_bytes": len(firmware)},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_transfer_exit(self, step: dict, index: int) -> StepResult:
        try:
            resp = await self._resolve_tester(step).transfer_exit()
            return StepResult(
                index=index, action="request_transfer_exit",
                params={}, success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="request_transfer_exit",
                params={}, success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_ecu_reset(self, step: dict, index: int) -> StepResult:
        reset_str = step.get("reset_type", "soft")
        wait_ms   = step.get("wait_ms", 2000)
        rmap = {"hard": ResetType.HARD, "keyOffOn": ResetType.KEY_OFF, "soft": ResetType.SOFT}
        reset_type = rmap.get(reset_str, ResetType.SOFT)

        try:
            resp = await self._resolve_tester(step).reset(reset_type)
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000.0)
            return StepResult(
                index=index, action="ecu_reset",
                params={"reset_type": reset_str, "wait_ms": wait_ms},
                success=True, response_pdu=_hex(resp.raw),
            )
        except NrcError as e:
            return StepResult(
                index=index, action="ecu_reset",
                params={"reset_type": reset_str, "wait_ms": wait_ms},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_tester_present(self, step: dict, index: int) -> StepResult:
        suppress = step.get("suppress", False)
        try:
            resp = await self._resolve_tester(step).tester_present(suppress_response=suppress)
            return StepResult(
                index=index, action="tester_present",
                params={"suppress": suppress}, success=True,
                response_pdu=_hex(resp.raw) if not suppress else None,
            )
        except NrcError as e:
            return StepResult(
                index=index, action="tester_present",
                params={"suppress": suppress},
                success=False, nrc=e.nrc, nrc_name=e.name, error=str(e),
            )

    async def _step_delay(self, step: dict, index: int) -> StepResult:
        ms = step.get("ms", 500)
        await asyncio.sleep(ms / 1000.0)
        return StepResult(index=index, action="delay", params={"ms": ms}, success=True)

    async def _step_assert(self, step: dict, index: int) -> StepResult:
        variable = step.get("variable")
        if variable not in self.variables:
            return StepResult(
                index=index, action="assert", params=step, success=False,
                error=f"Variable '{variable}' not set",
            )

        value  = self.variables[variable]
        errors = []

        if "length" in step and len(value) != step["length"]:
            errors.append(f"length {len(value)} != expected {step['length']}")

        if "contains" in step:
            needle = _parse_hex(step["contains"])
            if needle not in value:
                errors.append(f"value does not contain {step['contains']}")

        if step.get("not_nrc") and len(value) >= 3 and value[0] == 0x7F:
            nrc = value[2]
            errors.append(f"value is NRC 0x{nrc:02X} ({NRC_NAMES.get(nrc, '?')})")

        return StepResult(
            index=index, action="assert", params={"variable": variable},
            success=len(errors) == 0,
            error="; ".join(errors) if errors else None,
        )

    async def _step_foreach_did(self, step: dict, index: int) -> StepResult:
        min_session  = step.get("min_session", "default")
        expect_ok    = step.get("expect_ok", True)
        save_results = step.get("save_results", False)

        session_order = ["default", "extended", "programming"]
        min_idx = session_order.index(min_session) if min_session in session_order else 0

        dids = self.config.get("dids", [])
        readable = [
            d for d in dids
            if "read" in d.get("access", [])
            and session_order.index(d.get("min_session", "default")) <= min_idx + 1
        ]

        if not readable:
            return StepResult(
                index=index, action="foreach_did",
                params={"min_session": min_session, "did_count": 0},
                success=True,
            )

        passed = failed = 0
        for did in readable:
            did_str = did.get("id", "")
            did_id  = _did_int(did_str)
            try:
                resp = await self._resolve_tester(step).read_did(did_id)
                passed += 1
                if save_results:
                    key = f"did_{did_str.replace('0x', '').upper()}"
                    self.variables[key] = resp.data
            except NrcError:
                failed += 1

        ok = failed == 0 if expect_ok else True
        return StepResult(
            index=index, action="foreach_did",
            params={
                "min_session": min_session,
                "did_count": len(readable),
                "passed": passed,
                "failed": failed,
            },
            success=ok,
            error=f"{failed}/{len(readable)} DIDs returned NRC" if failed and expect_ok else None,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _step_label(self, step: dict) -> str:
        action = step.get("action", "?")
        if action == "session":
            return f"session({step.get('value', '?')})"
        if action == "security_access":
            return f"security_access(level={step.get('level', '?')})"
        if action == "read_did":
            return f"read_did({step.get('did', '?')})"
        if action == "write_did":
            return f"write_did({step.get('did', '?')})"
        if action == "read_memory_by_address":
            return f"read_memory({step.get('address', '?')},{step.get('size', '?')}B)"
        if action == "write_memory_by_address":
            return f"write_memory({step.get('address', '?')})"
        if action == "foreach_did":
            return f"foreach_did({step.get('min_session', 'default')})"
        if action == "ecu_reset":
            return f"ecu_reset({step.get('reset_type', 'soft')})"
        if action == "routine":
            return f"routine({step.get('id', '?')},{step.get('sub_fn', '?')})"
        if action == "delay":
            return f"delay({step.get('ms', 500)}ms)"
        # Plugin actions may attach a `step_label(step) -> str` attribute to
        # their handler for a richer terminal label.
        plugin_handler = _plugins.get_runner_actions().get(action)
        label_fn = getattr(plugin_handler, "step_label", None)
        if callable(label_fn):
            return label_fn(step)
        return action

    def _print_outcome(self, result: StepResult) -> None:
        if result.skipped:
            reason = result.error or "not applicable on this transport"
            print(col(YELLOW, "→ SKIP") + f"  {reason}")
            return
        if result.success:
            extra = ""
            if result.action == "foreach_did":
                p = result.params.get("passed", 0)
                t = result.params.get("did_count", 0)
                extra = f"{p}/{t} OK"
            print(col(GREEN, f"→ {'OK ' + extra:<12}") + f" ({result.duration_ms:.0f} ms)")
        else:
            err = result.error or "failed"
            if result.nrc:
                err = f"NRC 0x{result.nrc:02X} ({result.nrc_name or '?'})"
            print(col(RED, "→ FAIL") + f"  {err}")

# ---------------------------------------------------------------------------
# Config / campaign loading
# ---------------------------------------------------------------------------

def load_yaml(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(p, encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            print(f"ERROR: YAML parse error in {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(data, dict):
        print(f"ERROR: {path} root must be a YAML mapping", file=sys.stderr)
        sys.exit(1)
    return data


def merge_configs(diagnostics_config: dict, campaign: dict) -> dict:
    """
    Merge a campaign YAML into a config dict.

    Campaign's 'jobs:' block takes precedence; everything else (dids, dtcs,
    can/interface settings) comes from the config file. Works with both EDS
    diagnostics_config.yaml and standalone testlab_config.yaml.
    """
    merged = dict(diagnostics_config)
    merged["jobs"] = campaign.get("jobs", {})
    # Campaign metadata may override
    if "metadata" in campaign:
        merged.setdefault("metadata", {}).update(campaign["metadata"])
    return merged

# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------


async def run_campaign(
    config: dict,
    config_path: str,
    jobs_to_run: List[str],
    interface: str,
    rx_id: int,
    tx_id: int,
    timeout: float,
    verbose: bool,
    virtual: bool,
    someip_virtual: bool = False,
    someip_host_override: Optional[str] = None,
    shared_ecu_state: bool = False,
) -> List[JobResult]:
    """Run one or more jobs and return all JobResults.

    shared_ecu_state (virtual mode only): carry the simulated ECU's
    session/security/DTC state across jobs, like a real ECU that is not
    power-cycled between jobs. Default is a fresh EcuState per job so
    --all results are deterministic and independent of job order.
    """

    if virtual:
        # In-process VirtualBus — no hardware needed. Primarily for CI.
        from xaloqi.sim import EcuState, run_on_bus

        tester_bus, ecu_bus = VirtualBus.pair("campaign")
        # The simulated ECU's state lives in a one-slot box so it can be
        # replaced between jobs. Without isolation, security unlock and DTC
        # state bleed from one job into the next and --all results depend on
        # job order (validation report Bug 10).
        state_box = {"state": EcuState()}
        stop  = asyncio.Event()
        task  = asyncio.create_task(run_on_bus(ecu_bus, state_box, stop, tx_id, verbose))

        if len(jobs_to_run) > 1 and not shared_ecu_state:
            print("  ECU state: fresh per job "
                  "(pass --shared-ecu-state to carry state across jobs)\n")

        results = []
        try:
            async with UdsTester(
                tester_bus, rx_id=tx_id, tx_id=rx_id,
                keepalive=False, verbose=verbose
            ) as tester:
                for i, job_name in enumerate(jobs_to_run):
                    if i > 0 and not shared_ecu_state:
                        state_box["state"] = EcuState()
                    executor = CampaignExecutor(
                        config, config_path, tester, verbose,
                        someip_virtual=someip_virtual,
                        someip_host_override=someip_host_override,
                    )
                    results.append(await executor.execute_job(job_name))
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return results

    else:
        # SocketCAN / real hardware
        # rx_id/tx_id in config are ECU-perspective; UdsTester uses tester-perspective:
        # tester.rx_id = ECU tx_id, tester.tx_id = ECU rx_id
        async with UdsTester(
            interface, rx_id=tx_id, tx_id=rx_id,
            timeout=timeout, keepalive=True, verbose=verbose,
        ) as tester:
            results = []
            for job_name in jobs_to_run:
                executor = CampaignExecutor(
                    config, config_path, tester, verbose,
                    someip_virtual=someip_virtual,
                    someip_host_override=someip_host_override,
                )
                results.append(await executor.execute_job(job_name))
            return results

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RTOS comparison (P3-C)
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Xaloqi TestLab Campaign Runner v{RUNNER_VERSION}"
    )
    parser.add_argument("--workspace", default=None,
                        help="Path to testlab_workspace.yaml (multi-ECU topology). "
                             "When set, --config is optional.")
    parser.add_argument("--config",   default=None,
                        help="Path to diagnostics_config.yaml (EDS) or testlab_config.yaml (standalone).")
    parser.add_argument("--campaign", default=None,
                        help="Path to campaign YAML (contains 'jobs:' block). "
                             "If omitted, reads jobs from --config directly.")
    parser.add_argument("--job",      default=None,
                        help="Name of the job to run")
    parser.add_argument("--all",      action="store_true",
                        help="Run all jobs in the campaign")
    parser.add_argument("--shared-ecu-state", action="store_true",
                        help="Virtual mode only: carry the simulated ECU's "
                             "session/security/DTC state across jobs, like a "
                             "real ECU that is not power-cycled between jobs. "
                             "Default: fresh ECU state per job (deterministic, "
                             "order-independent results).")
    parser.add_argument("--list",     action="store_true",
                        help="List available jobs and exit")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Validate job schema without connecting to ECU")
    parser.add_argument("--interface", default="vcan0",
                        help="SocketCAN interface (default: vcan0)")
    parser.add_argument("--virtual",  action="store_true",
                        help="Use in-process VirtualBus + ECU simulator (no hardware)")
    parser.add_argument("--timeout",  type=float, default=0.15,
                        help="UDS response timeout in seconds (default: 0.15)")
    parser.add_argument("--verbose",  action="store_true",
                        help="Log every CAN frame")
    parser.add_argument("--json",     default=None, metavar="OUTPUT",
                        help="Write JSON results to this file")
    parser.add_argument("--rtos",     default=None, metavar="A,B",
                        help="RTOS comparison mode: comma-separated names, e.g. zephyr,freertos. "
                             "Runs the same job(s) on two targets and diffs the results.")
    parser.add_argument("--rtos-interfaces", default=None, metavar="IF_A,IF_B",
                        help="CAN interfaces for each RTOS target, e.g. vcan0,vcan1 "
                             "(default: vcan0,vcan0 — same interface). Ignored with --virtual.")
    parser.add_argument("--rtos-out", default=None, metavar="FILE",
                        help="Write RTOS comparison HTML report to this file (optional).")
    parser.add_argument("--someip-virtual", action="store_true",
                        help="Use VirtualSomeIpBus instead of real network for SOME/IP actions (for CI).")
    parser.add_argument("--someip-host", default=None, metavar="HOST",
                        help="Override SOME/IP target host for all someip_* actions.")

    args = parser.parse_args()

    print(col(BOLD, f"\nXaloqi TestLab Campaign Runner v{RUNNER_VERSION}"))
    print(col(BOLD, "─" * 65))

    # Refuse to run the CLI against a different library version (TestLab#28)
    check_library_version()

    # No license gate here: the free tier (--virtual) is free by design, and
    # pro transports/features enforce licensing inside xaloqi-tester-pro.

    # Multi-ECU workspace mode is provided by xaloqi-tester-pro
    workspace_mode = _plugins.get_runner_hook("workspace") if args.workspace else None
    if args.workspace and workspace_mode is None:
        print(_plugins.pro_missing_message("runner_hooks", "workspace"), file=sys.stderr)
        return 1

    # --workspace mode: load workspace + optional campaign config
    if args.workspace and not args.config:
        # Pure workspace mode: load a stub config with no jobs
        # (jobs come from the campaign file)
        diagnostics_cfg = {}
        config_path_str = args.workspace
    elif args.config:
        diagnostics_cfg = load_yaml(args.config)
        config_path_str = args.config
    else:
        print("ERROR: --config or --workspace is required", file=sys.stderr)
        return 1

    # Load configs

    if args.campaign:
        campaign_cfg = load_yaml(args.campaign)
        config = merge_configs(diagnostics_cfg, campaign_cfg)
    else:
        config = diagnostics_cfg

    jobs = config.get("jobs", {})

    # -- list mode ------------------------------------------------------------
    if args.list:
        meta = config.get("metadata", {})
        if args.workspace:
            print(f"  Workspace: {args.workspace}")
        else:
            print(f"  Config: {args.config}")
        if args.campaign:
            print(f"  Campaign:  {args.campaign}")
        if not args.workspace:
            print(f"  ECU: {meta.get('ecu_name', '?')} v{meta.get('version', '?')}")
        else:
            try:
                ws_cfg = workspace_mode.load_workspace(args.workspace)
                ecu_names = ", ".join(e.name for e in ws_cfg.ecus)
                print(f"  ECUs:      {ecu_names}")
            except Exception:
                pass
        if not jobs:
            print("  No 'jobs:' block found.")
            return 0
        print(f"\n  Available jobs ({len(jobs)}):\n")
        for name, defn in jobs.items():
            desc  = defn.get("description", "")
            steps = len(defn.get("steps", []))
            print(f"    {col(CYAN, name)}")
            if desc:
                print(f"      {desc.strip()[:80]}")
            print(f"      {steps} steps, on_failure: {defn.get('on_failure', 'abort')}")
        print()
        return 0

    # -- dry-run mode ---------------------------------------------------------
    if args.dry_run:
        ws_cfg_for_dry = None
        if args.workspace:
            print(f"  Workspace: {args.workspace}")
            try:
                ws_cfg_for_dry = workspace_mode.load_workspace(args.workspace)
                ecu_names = ", ".join(e.name for e in ws_cfg_for_dry.ecus)
                print(f"  ECUs:      {ecu_names}")
            except Exception as exc:
                print(col(RED, f"  Workspace ERROR: {exc}"), file=sys.stderr)
                return 1
        else:
            print(f"  Config:  {args.config}")
        if args.campaign:
            print(f"  Campaign:  {args.campaign}")
        print(f"  Mode:    dry-run (no ECU connection)\n")
        job_filter = args.job if not args.all else None
        rc = dry_run_config(config, job_filter, workspace_config=ws_cfg_for_dry)
        if rc == 0:
            print(f"\n  {col(GREEN, 'All validations passed.')}")
        return rc

    # -- determine jobs to run ------------------------------------------------
    if args.all:
        jobs_to_run = list(jobs.keys())
    elif args.job:
        if args.job not in jobs:
            print(f"ERROR: Job '{args.job}' not found. Available: {list(jobs.keys())}",
                  file=sys.stderr)
            return 1
        jobs_to_run = [args.job]
    else:
        parser.print_help()
        print("\nERROR: Specify --job <name>, --all, --list, or --dry-run", file=sys.stderr)
        return 1

    # -- validate the campaign before touching the ECU ------------------------
    # Schema validation used to run ONLY under --dry-run, so a normal run
    # never checked the campaign at all -- which is how a misspelled
    # assertion key reached execution and silently passed (#4). Validate the
    # jobs we are about to run, and refuse to run an invalid campaign.
    schema_errors: List[str] = []
    for _name in jobs_to_run:
        schema_errors.extend(validate_job_schema(_name, jobs[_name]))
    if schema_errors:
        print(col(RED, "FAIL") + "  Campaign validation errors:", file=sys.stderr)
        for _err in schema_errors:
            print(f"       {_err}", file=sys.stderr)
        print(
            "\n  Refusing to run an invalid campaign. "
            "Fix the errors above, or re-check with --dry-run.",
            file=sys.stderr,
        )
        return 1

    # -- resolve rx/tx IDs from config ----------------------------------------
    # Support both EDS format (can.rx_can_id / can.tx_can_id) and standalone
    # testlab_config.yaml format (top-level rx_id / tx_id).
    can_section = diagnostics_cfg.get("can", {})

    def _parse_id(s, default):
        try:
            return int(str(s), 0)
        except (ValueError, TypeError):
            return default

    rx_id = _parse_id(
        can_section.get("rx_can_id") or diagnostics_cfg.get("rx_id"),
        0x7DF,
    )
    tx_id = _parse_id(
        can_section.get("tx_can_id") or diagnostics_cfg.get("tx_id"),
        0x7E8,
    )

    # -- workspace mode -------------------------------------------------------
    if args.workspace:
        if args.all:
            jobs_to_run_ws = list(jobs.keys()) if jobs else []
        elif args.job:
            jobs_to_run_ws = [args.job]
        else:
            print("ERROR: Specify --job <name> or --all with --workspace", file=sys.stderr)
            return 1

        all_results = asyncio.run(workspace_mode.run_workspace_campaign(
            workspace_path=args.workspace,
            config=config if "config" in dir() else {},
            config_path=args.workspace,
            jobs_to_run=jobs_to_run_ws,
            timeout=args.timeout,
            verbose=args.verbose,
            someip_virtual=args.someip_virtual,
            someip_host_override=args.someip_host,
        ))

        overall_rc = 0 if all(r.success for r in all_results) else 1

        if args.json:
            output = {
                "schema_version":    JSON_SCHEMA_VERSION,
                "runner_version":    RUNNER_VERSION,
                "workspace_path":    args.workspace,
                "generated_at":      datetime.now(timezone.utc).isoformat(),
                "runs":              [r.to_dict() for r in all_results],
            }
            out_path = Path(args.json)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(output, fh, indent=2)
            print(f"  JSON results: {args.json}")

        if len(all_results) > 1:
            passed = sum(1 for r in all_results if r.success)
            print(col(BOLD, f"\nSummary: {passed}/{len(all_results)} jobs passed\n"))

        return overall_rc

    # -- run ------------------------------------------------------------------

    if args.rtos:
        # ── RTOS comparison mode (xaloqi-tester-pro) ─────────────────────────
        rtos_mode = _plugins.get_runner_hook("rtos")
        if rtos_mode is None:
            print(_plugins.pro_missing_message("runner_hooks", "rtos"), file=sys.stderr)
            return 1
        return rtos_mode.cli_run(
            args, config=config, jobs_to_run=jobs_to_run, rx_id=rx_id, tx_id=tx_id,
        )

    # ── Normal (non-RTOS) run ─────────────────────────────────────────────────
    try:
        all_results = asyncio.run(run_campaign(
            config=config,
            config_path=args.config,
            jobs_to_run=jobs_to_run,
            interface=args.interface,
            rx_id=rx_id,
            tx_id=tx_id,
            timeout=args.timeout,
            verbose=args.verbose,
            virtual=args.virtual,
            someip_virtual=args.someip_virtual,
            someip_host_override=args.someip_host,
            shared_ecu_state=args.shared_ecu_state,
        ))
    except TransportError as exc:
        # e.g. a real-hardware run without xaloqi-tester-pro installed
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        from xaloqi.tester.exceptions import LicenseError
        if isinstance(exc, LicenseError):
            print(f"\n{exc}", file=sys.stderr)
            return 1
        raise

    overall_rc = 0 if all(r.success for r in all_results) else 1

    # -- JSON output ----------------------------------------------------------
    if args.json:
        output = {
            "schema_version":    JSON_SCHEMA_VERSION,
            "runner_version":    RUNNER_VERSION,
            "config_path":       args.config,
            "generated_at":      datetime.now(timezone.utc).isoformat(),
            "runs":              [r.to_dict() for r in all_results],
        }
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, indent=2)
        print(f"  JSON results: {args.json}")

    if len(all_results) > 1:
        passed = sum(1 for r in all_results if r.success)
        print(col(BOLD, f"\nSummary: {passed}/{len(all_results)} jobs passed\n"))

    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
