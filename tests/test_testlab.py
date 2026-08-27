#!/usr/bin/env python3
"""
tests/test_testlab.py

Unit tests for tools/testlab.py — analyze / trend / report / compare.

Tests use in-memory fixture dicts — no file I/O except for report output.
No license key, no ECU, no network required.

Run with: XALOQI_LICENSE_SKIP=1 pytest tests/test_testlab.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Imports (conftest puts core/ on sys.path for repo-checkout runs)
# ---------------------------------------------------------------------------

os.environ["XALOQI_LICENSE_SKIP"] = "1"

from xaloqi import testlab
from xaloqi.testlab import (
    _load_results_file, _load_many, _step_label, _step_status_line,
    cmd_analyze, TestLabError, SUPPORTED_SCHEMA_VERSION, NRC_NAMES,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _make_step(index, action, success, nrc=None, **params):
    return {
        "index": index, "action": action, "params": params,
        "success": success, "duration_ms": 12.5,
        "request_pdu": None, "response_pdu": None,
        "nrc": nrc, "nrc_name": NRC_NAMES.get(nrc) if nrc else None,
        "error": f"NRC 0x{nrc:02X}" if nrc and not success else None,
    }


def _make_run(job_name="basic", success=True, steps=None, ecu="TestECU", started="2026-05-06T09:00:00+00:00"):
    if steps is None:
        steps = [
            _make_step(1, "session",         True,  value="extended"),
            _make_step(2, "security_access",  True,  level=1),
            _make_step(3, "read_did",         True,  did="0xF190", expect_ok=True),
        ]
    n_pass = sum(1 for s in steps if s["success"])
    return {
        "schema_version": 1, "job_name": job_name, "config_path": "cfg.yaml",
        "ecu_name": ecu, "ecu_version": "1.0",
        "started_at": started, "finished_at": started,
        "duration_ms": 100.0, "success": success, "variables": {}, "steps": steps,
        "summary": f"{n_pass}/{len(steps)} steps passed",
    }


def _make_file(runs, tmp_path, name="run.json"):
    data = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "runner_version": "1.0.0",
        "config_path": "cfg.yaml",
        "generated_at": "2026-05-06T10:00:00+00:00",
        "runs": runs,
    }
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)

# ---------------------------------------------------------------------------
# _step_label
# ---------------------------------------------------------------------------

class TestStepLabel:
    def test_session(self):
        assert _step_label({"action": "session", "params": {"value": "extended"}}) == "session(extended)"

    def test_security_access(self):
        assert _step_label({"action": "security_access", "params": {"level": 1}}) == "security_access(level=1)"

    def test_read_did(self):
        assert _step_label({"action": "read_did", "params": {"did": "0xF190"}}) == "read_did(0xF190)"

    def test_ecu_reset(self):
        assert _step_label({"action": "ecu_reset", "params": {"reset_type": "soft"}}) == "ecu_reset(soft)"

    def test_unknown_action_returns_action(self):
        assert _step_label({"action": "mystery", "params": {}}) == "mystery"

# ---------------------------------------------------------------------------
# _load_results_file
# ---------------------------------------------------------------------------

class TestLoadResultsFile:
    def test_load_valid_file(self, tmp_path):
        run = _make_run()
        path = _make_file([run], tmp_path)
        data = _load_results_file(path)
        assert data["schema_version"] == 1
        assert len(data["runs"]) == 1

    def test_missing_file_raises(self):
        with pytest.raises(TestLabError, match="not found"):
            _load_results_file("/nonexistent/path/run.json")

    def test_wrong_schema_version_raises(self, tmp_path):
        data = {"schema_version": 99, "runs": [_make_run()]}
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(data))
        with pytest.raises(TestLabError, match="schema_version"):
            _load_results_file(str(p))

    def test_empty_runs_raises(self, tmp_path):
        data = {"schema_version": 1, "runs": []}
        p = tmp_path / "empty.json"
        p.write_text(json.dumps(data))
        with pytest.raises(TestLabError, match="No 'runs'"):
            _load_results_file(str(p))

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json {{{{")
        with pytest.raises(TestLabError, match="JSON parse error"):
            _load_results_file(str(p))

# ---------------------------------------------------------------------------
# _collect_trend_data
# ---------------------------------------------------------------------------

class TestCmdAnalyze:
    def test_pass_run_returns_0(self, tmp_path, capsys):
        path = _make_file([_make_run(success=True)], tmp_path)
        ns = MagicMock(); ns.results = path
        rc = cmd_analyze(ns)
        assert rc == 0

    def test_fail_run_returns_1(self, tmp_path):
        run = _make_run(success=False, steps=[_make_step(1, "read_did", False, 0x31, did="0xDEAD", expect_ok=True)])
        path = _make_file([run], tmp_path)
        ns = MagicMock(); ns.results = path
        rc = cmd_analyze(ns)
        assert rc == 1

    def test_missing_file_returns_1(self):
        ns = MagicMock(); ns.results = "/nonexistent.json"
        rc = cmd_analyze(ns)
        assert rc == 1

# ---------------------------------------------------------------------------
# cmd_trend
# ---------------------------------------------------------------------------

