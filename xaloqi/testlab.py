#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
=============================================================================
Xaloqi TestLab
FILE: xaloqi/testlab.py  (CLI: testlab; legacy shim: tools/testlab.py)

PURPOSE: Analyse campaign runner JSON results.

         The free core ships `analyze` (terminal step summary) and the
         shared result-loading primitives. The deliverable subcommands —
         trend, report, compare, explain, someip-validate, serve — are
         provided by xaloqi-tester-pro and discovered through the
         ``xaloqi_tester.cli_commands`` entry-point group.

USAGE:
    testlab analyze --results reports/run_001.json

JSON INPUT: schema_version 1 — produced by testlab-run --json.

VERSION: 1.5.2
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from xaloqi.tester import _plugins

SUPPORTED_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Colour helpers
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
# NRC reference table
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

# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------

class TestLabError(Exception):
    pass


def _load_results_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise TestLabError(f"Results file not found: {path}")
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise TestLabError(f"JSON parse error in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise TestLabError(f"Results file root must be a JSON object: {path}")

    schema = data.get("schema_version")
    if schema != SUPPORTED_SCHEMA_VERSION:
        raise TestLabError(
            f"Unsupported schema_version {schema!r} in {path}. "
            f"testlab.py supports schema_version {SUPPORTED_SCHEMA_VERSION}. "
            "Regenerate results with a matching runner.py version."
        )

    runs = data.get("runs")
    if not isinstance(runs, list) or len(runs) == 0:
        raise TestLabError(f"No 'runs' found in {path}")

    return data


def _load_many(paths: List[str]) -> List[dict]:
    return [_load_results_file(p) for p in paths]

# ---------------------------------------------------------------------------
# Step label helpers
# ---------------------------------------------------------------------------


def _step_label(step: dict) -> str:
    action = step.get("action", "?")
    params = step.get("params", {})
    if action == "session":
        return f"session({params.get('value', '?')})"
    if action == "security_access":
        return f"security_access(level={params.get('level', '?')})"
    if action == "read_did":
        return f"read_did({params.get('did', '?')})"
    if action == "write_did":
        return f"write_did({params.get('did', '?')})"
    if action == "foreach_did":
        passed = params.get("passed", "?")
        total  = params.get("did_count", "?")
        return f"foreach_did({params.get('min_session', 'default')}) {passed}/{total}"
    if action == "ecu_reset":
        return f"ecu_reset({params.get('reset_type', 'soft')})"
    if action == "routine":
        return f"routine({params.get('id', '?')},{params.get('sub_fn', '?')})"
    if action == "delay":
        return f"delay({params.get('ms', 500)}ms)"
    return action


def _step_status_line(step: dict) -> str:
    index   = step.get("index", "?")
    success = step.get("success", False)
    dur     = step.get("duration_ms", 0.0)
    nrc     = step.get("nrc")
    error   = step.get("error")
    label   = _step_label(step)

    if success:
        outcome = col(GREEN, "PASS")
    else:
        outcome = col(RED, "FAIL")
        if nrc is not None:
            outcome += f"  NRC 0x{nrc:02X} ({NRC_NAMES.get(nrc, 'unknown')})"
        elif error:
            outcome += f"  {error}"

    return f"  [{index:>3}]  {label:<42}  {outcome}  ({dur:.0f} ms)"

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> int:
    try:
        data = _load_results_file(args.results)
    except TestLabError as exc:
        print(col(RED, f"ERROR: {exc}"), file=sys.stderr)
        return 1

    overall_exit = 0
    for run in data["runs"]:
        job_name    = run.get("job_name", "unknown")
        ecu_name    = run.get("ecu_name", "")
        ecu_version = run.get("ecu_version", "")
        started_at  = run.get("started_at", "")
        duration_ms = run.get("duration_ms", 0.0)
        success     = run.get("success", False)
        summary     = run.get("summary", "")
        steps       = run.get("steps", [])

        print()
        print(col(BOLD, "═" * 70))
        print(f"  Job:     {col(CYAN, job_name)}")
        print(f"  ECU:     {ecu_name} v{ecu_version}")
        print(f"  Started: {started_at}")
        print(f"  Config:  {run.get('config_path', '—')}")
        print(col(BOLD, "═" * 70))
        print()

        for step in steps:
            print(_step_status_line(step))

        print()
        print(col(BOLD, "─" * 70))
        result_str = col(GREEN, "PASS") if success else col(RED, "FAIL")
        print(f"  Result:  {result_str}")
        print(f"  Steps:   {summary}")
        print(f"  Time:    {duration_ms:.0f} ms")
        print(col(BOLD, "─" * 70))

        if not success:
            overall_exit = 1

    return overall_exit


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testlab",
        description="Xaloqi TestLab — analyse campaign results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  testlab analyze --results reports/run_001.json\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("analyze", help="Step summary for a single run")
    p.add_argument("--results", required=True, metavar="FILE")
    p.set_defaults(func=cmd_analyze)

    # Pro subcommands (report/trend/compare/serve/explain/someip-validate)
    # register themselves through the entry-point seam.
    for name, register in sorted(_plugins.get_cli_commands().items()):
        register(sub)

    return parser


def main() -> int:
    # A Pro subcommand typed without pro installed gets the one consistent
    # message instead of argparse's "invalid choice".
    argv = sys.argv[1:]
    if argv:
        candidate = argv[0]
        if (candidate in _plugins.PRO_CLI_COMMANDS
                and candidate not in _plugins.get_cli_commands()):
            print(_plugins.pro_missing_message("cli_commands", candidate),
                  file=sys.stderr)
            return 1
    parser = build_parser()
    args   = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
