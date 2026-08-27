#!/usr/bin/env python3
"""
tests/test_runner.py

Unit and integration tests for xaloqi/runner.py — TestLab campaign runner.

Tests:
- Schema validation (dry-run)
- Config/campaign merging
- Job result data model
- Full end-to-end campaign over VirtualBus + ECU simulator
- JSON output format (schema_version: 1 compatibility)

No hardware, no license key, no network required.
Run with: XALOQI_LICENSE_SKIP=1 pytest tests/test_runner.py -v
"""

from __future__ import annotations

import asyncio
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

from xaloqi.runner import (
    validate_job_schema,
    dry_run_config,
    merge_configs,
    CampaignExecutor,
    StepResult,
    JobResult,
    JSON_SCHEMA_VERSION,
    RUNNER_VERSION,
)

os.environ["XALOQI_LICENSE_SKIP"] = "1"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CORE_DIR = Path(__file__).resolve().parent.parent

MINIMAL_CONFIG = {
    "metadata": {"ecu_name": "TestECU", "version": "1.0"},
    "can":      {"rx_can_id": "0x7DF", "tx_can_id": "0x7E8"},
    "timing":   {"p2_server_max_ms": 25},
    "dids": [
        {"id": "0xF190", "name": "VIN", "data_length": 17,
         "access": ["read"], "min_session": "default",
         "read_security_level": 0, "write_security_level": 0},
        {"id": "0xF18C", "name": "SerialNumber", "data_length": 11,
         "access": ["read"], "min_session": "default",
         "read_security_level": 0, "write_security_level": 0},
    ],
    "dtcs": [],
}

BASIC_JOB = {
    "description": "Basic validation job",
    "timeout_ms":  10000,
    "on_failure":  "abort",
    "steps": [
        {"action": "session",          "value":  "extended"},
        {"action": "security_access",  "level":  1},
        {"action": "read_did",         "did":    "0xF190"},
        {"action": "clear_dtc",        "group":  "0xFFFFFF"},
        {"action": "read_dtc"},
        {"action": "session",          "value":  "default"},
    ],
}

EOL_JOB = {
    "description": "End-of-line production check",
    "timeout_ms":  30000,
    "on_failure":  "continue",
    "steps": [
        {"action": "session",          "value":       "extended"},
        {"action": "security_access",  "level":       1},
        {"action": "foreach_did",      "min_session": "extended",
         "expect_ok": False, "save_results": True},
        {"action": "clear_dtc",        "group":       "0xFFFFFF"},
        {"action": "read_dtc",         "save_as":     "active_dtcs"},
        {"action": "session",          "value":       "default"},
    ],
}

# ---------------------------------------------------------------------------
# Schema validation tests
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_valid_job_no_errors(self):
        errors = validate_job_schema("basic", BASIC_JOB)
        assert errors == []

    def test_empty_steps_is_error(self):
        errors = validate_job_schema("bad", {"steps": []})
        assert any("non-empty" in e for e in errors)

    def test_unknown_action_is_error(self):
        job = {"steps": [{"action": "fly_to_moon"}]}
        errors = validate_job_schema("bad", job)
        assert any("unknown action" in e for e in errors)

    def test_session_unknown_value_is_error(self):
        job = {"steps": [{"action": "session", "value": "warp_speed"}]}
        errors = validate_job_schema("bad", job)
        assert any("session" in e for e in errors)

    def test_read_did_bad_format_is_error(self):
        job = {"steps": [{"action": "read_did", "did": "F190"}]}  # missing 0x prefix
        errors = validate_job_schema("bad", job)
        assert any("0xF190" in e or "4-digit hex" in e for e in errors)

    def test_security_access_missing_level_is_error(self):
        job = {"steps": [{"action": "security_access", "level": "one"}]}
        errors = validate_job_schema("bad", job)
        assert any("level" in e for e in errors)

    def test_dry_run_valid_config_returns_0(self):
        config = {**MINIMAL_CONFIG, "jobs": {"basic": BASIC_JOB}}
        rc = dry_run_config(config)
        assert rc == 0

    def test_dry_run_invalid_config_returns_1(self):
        config = {**MINIMAL_CONFIG, "jobs": {"bad": {"steps": []}}}
        rc = dry_run_config(config)
        assert rc == 1

    def test_dry_run_no_jobs_returns_0(self):
        rc = dry_run_config(MINIMAL_CONFIG)
        assert rc == 0

    def test_dry_run_specific_job(self):
        config = {
            **MINIMAL_CONFIG,
            "jobs": {
                "good": BASIC_JOB,
                "bad":  {"steps": []},
            },
        }
        assert dry_run_config(config, job_filter="good") == 0

# ---------------------------------------------------------------------------
# Config merging tests
# ---------------------------------------------------------------------------

class TestConfigMerging:
    def test_merge_jobs_from_campaign(self):
        campaign = {"jobs": {"eol": EOL_JOB}}
        merged = merge_configs(MINIMAL_CONFIG, campaign)
        assert "eol" in merged["jobs"]
        assert merged["metadata"]["ecu_name"] == "TestECU"

    def test_campaign_metadata_overrides(self):
        campaign = {
            "jobs": {"eol": EOL_JOB},
            "metadata": {"ecu_name": "OverriddenECU"},
        }
        merged = merge_configs(MINIMAL_CONFIG, campaign)
        assert merged["metadata"]["ecu_name"] == "OverriddenECU"

    def test_dids_preserved_from_diagnostics(self):
        campaign = {"jobs": {"eol": EOL_JOB}}
        merged = merge_configs(MINIMAL_CONFIG, campaign)
        assert len(merged["dids"]) == 2

# ---------------------------------------------------------------------------
# JobResult / StepResult data model tests
# ---------------------------------------------------------------------------

class TestDataModel:
    def test_step_result_to_dict_excludes_saved_var(self):
        s = StepResult(
            index=1, action="read_did", params={"did": "0xF190"},
            success=True, saved_var="my_var",
        )
        d = s.to_dict()
        assert "saved_var" not in d
        assert d["action"] == "read_did"
        assert d["success"] is True

    def test_job_result_to_dict_schema_version(self):
        jr = JobResult(
            schema_version=JSON_SCHEMA_VERSION,
            job_name="test_job",
            config_path="config.yaml",
            ecu_name="TestECU",
            ecu_version="1.0",
            started_at="2026-05-01T09:00:00+00:00",
            finished_at="2026-05-01T09:00:01+00:00",
            duration_ms=1000.0,
            success=True,
            steps=[],
            variables={},
            summary="0/0 steps passed",
        )
        d = jr.to_dict()
        assert d["schema_version"] == 1
        assert d["job_name"] == "test_job"
        assert d["success"] is True

# ---------------------------------------------------------------------------
# JSON output format tests (schema_version: 1 compatibility)
# ---------------------------------------------------------------------------

class TestJsonOutput:
    def test_schema_version_is_1(self):
        assert JSON_SCHEMA_VERSION == 1

    def _load_fixture(self, name: str) -> dict:
        path = _FIXTURES / name
        assert path.exists(), f"Fixture missing: {path}. Regenerate with tests/gen_fixtures.py"
        with open(path) as fh:
            return json.load(fh)

    def _assert_fixture_structure(self, data: dict) -> None:
        """Assert the fixture matches runner.py output schema exactly."""
        assert data["schema_version"] == 1
        assert "runner_version" in data, "Missing runner_version — fixture may be from EDS jobrunner"
        assert "jobrunner_version" not in data, "Stale jobrunner_version key — regenerate fixture"
        assert data["runner_version"] == RUNNER_VERSION
        assert data["config_path"] == "core/examples/diagnostics_config.yaml"
        assert len(data["runs"]) > 0
        for step in data["runs"][0]["steps"]:
            for field in ("index", "action", "params", "success", "duration_ms", "nrc", "error"):
                assert field in step, f"Step missing field: {field}"

    def test_fixture_all_pass_is_loadable(self):
        data = self._load_fixture("run_all_pass.json")
        self._assert_fixture_structure(data)
        run = data["runs"][0]
        assert run["success"] is True
        assert run["ecu_name"] == "TestLab-Sim"
        assert all(s["success"] for s in run["steps"])
        assert len(run["steps"]) == 7

    def test_fixture_mixed_loadable(self):
        data = self._load_fixture("run_mixed.json")
        self._assert_fixture_structure(data)
        run = data["runs"][0]
        assert run["success"] is False
        steps = run["steps"]
        outcomes = {s["success"] for s in steps}
        assert True in outcomes and False in outcomes
        # Verify NRC values use integer representation
        failing = [s for s in steps if not s["success"]]
        assert all(isinstance(s["nrc"], int) for s in failing if s["nrc"] is not None)

    def test_fixture_all_fail_is_loadable(self):
        data = self._load_fixture("run_all_fail.json")
        self._assert_fixture_structure(data)
        run = data["runs"][0]
        assert run["success"] is False
        assert all(not s["success"] for s in run["steps"])

    def test_json_write_roundtrip(self, tmp_path):
        """Runner writes valid JSON that can be loaded and verified."""
        jr = JobResult(
            schema_version=JSON_SCHEMA_VERSION,
            job_name="roundtrip",
            config_path="x.yaml",
            ecu_name="TestECU",
            ecu_version="1.0",
            started_at="2026-05-01T09:00:00+00:00",
            finished_at="2026-05-01T09:00:01+00:00",
            duration_ms=500.0,
            success=True,
            steps=[
                StepResult(
                    index=1, action="session",
                    params={"value": "extended"},
                    success=True, duration_ms=12.5,
                    response_pdu="5003",
                )
            ],
            variables={},
            summary="1/1 steps passed",
        )

        output = {
            "schema_version":  JSON_SCHEMA_VERSION,
            "runner_version":  RUNNER_VERSION,
            "config_path":     "x.yaml",
            "generated_at":    "2026-05-01T09:00:01+00:00",
            "runs":            [jr.to_dict()],
        }

        out_file = tmp_path / "result.json"
        out_file.write_text(json.dumps(output, indent=2), encoding="utf-8")

        loaded = json.loads(out_file.read_text())
        assert loaded["schema_version"] == 1
        assert loaded["runs"][0]["success"] is True
        assert loaded["runs"][0]["steps"][0]["action"] == "session"

# ---------------------------------------------------------------------------
# End-to-end integration: VirtualBus + ECU simulator
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """
    Full campaign over VirtualBus + docker/ecu_sim/sim.py.
    No hardware, no network, no license key.
    """

    @pytest.fixture
    def ecu_config(self):
        sim_config = {
            **MINIMAL_CONFIG,
            "jobs": {
                "basic_validation": {
                    "description": "Basic validation",
                    "timeout_ms": 10000,
                    "on_failure": "abort",
                    "steps": [
                        {"action": "session",         "value": "extended"},
                        {"action": "security_access", "level": 1},
                        {"action": "read_did",        "did": "0xF190"},
                        {"action": "read_dtc"},
                        {"action": "clear_dtc"},
                        {"action": "session",         "value": "default"},
                    ],
                },
                "continue_on_fail": {
                    "description": "Job that encounters NRC but continues",
                    "timeout_ms": 10000,
                    "on_failure": "continue",
                    "steps": [
                        {"action": "read_did", "did": "0xDEAD"},  # will fail NRC 0x31
                        {"action": "read_did", "did": "0xF190"},  # should still run
                    ],
                },
            },
        }
        return sim_config

    @pytest.mark.asyncio
    async def test_basic_validation_job_passes(self, ecu_config):
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["basic_validation"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert len(results) == 1
        result = results[0]
        assert result.job_name == "basic_validation"
        assert result.success is True
        assert len(result.steps) == 6
        assert all(s.success for s in result.steps)

    @pytest.mark.asyncio
    async def test_on_failure_continue_runs_all_steps(self, ecu_config):
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["continue_on_fail"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        result = results[0]
        # Job fails overall (first step fails) but second step runs
        assert len(result.steps) == 2
        assert result.steps[0].success is False   # 0xDEAD → NRC 0x31
        assert result.steps[1].success is True    # 0xF190 → OK

    @pytest.mark.asyncio
    async def test_json_output_contains_correct_fields(self, ecu_config, tmp_path):
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["basic_validation"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )

        output = {
            "schema_version": JSON_SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION,
            "config_path":    "test_config.yaml",
            "generated_at":   "2026-05-01T09:00:00+00:00",
            "runs":           [r.to_dict() for r in results],
        }

        out_file = tmp_path / "result.json"
        out_file.write_text(json.dumps(output), encoding="utf-8")

        loaded = json.loads(out_file.read_text())
        assert loaded["schema_version"] == 1
        run = loaded["runs"][0]
        assert run["job_name"] == "basic_validation"
        assert run["success"] is True
        assert "started_at" in run
        assert "duration_ms" in run
        for step in run["steps"]:
            assert "index" in step
            assert "action" in step
            assert "success" in step
            assert "duration_ms" in step

    @pytest.mark.asyncio
    async def test_security_access_unlocks_did(self, ecu_config):
        """After security_access, DID 0xF187 should be readable."""
        ecu_config["jobs"]["secured_read"] = {
            "description": "Read secured DID",
            "timeout_ms": 10000,
            "on_failure": "abort",
            "steps": [
                {"action": "session",         "value": "extended"},
                {"action": "security_access", "level": 1},
                {"action": "read_did",        "did": "0xF187"},
            ],
        }
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["secured_read"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_foreach_did_reads_all(self, ecu_config):
        ecu_config["jobs"]["foreach_test"] = {
            "description": "foreach_did test",
            "timeout_ms": 10000,
            "on_failure": "continue",
            "steps": [
                {"action": "session",     "value": "default"},
                {"action": "foreach_did", "min_session": "default",
                 "expect_ok": True, "save_results": True},
            ],
        }
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["foreach_test"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        result = results[0]
        foreach_step = next(s for s in result.steps if s.action == "foreach_did")
        assert foreach_step.params["passed"] == foreach_step.params["did_count"]

    @pytest.mark.asyncio
    async def test_multiple_jobs_run_sequentially(self, ecu_config):
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["basic_validation", "basic_validation"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_ecu_reset_step(self, ecu_config):
        ecu_config["jobs"]["reset_test"] = {
            "description": "ECU reset test",
            "timeout_ms": 10000,
            "on_failure": "abort",
            "steps": [
                {"action": "ecu_reset", "reset_type": "soft", "wait_ms": 0},
            ],
        }
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["reset_test"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert results[0].steps[0].success is True

    @pytest.mark.asyncio
    async def test_tester_present_step(self, ecu_config):
        ecu_config["jobs"]["tp_test"] = {
            "description": "TesterPresent test",
            "timeout_ms": 5000,
            "on_failure": "abort",
            "steps": [
                {"action": "tester_present", "suppress": False},
                {"action": "tester_present", "suppress": True},
            ],
        }
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=ecu_config,
            config_path="test_config.yaml",
            jobs_to_run=["tp_test"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert all(s.success for s in results[0].steps)


class TestWorkspaceEcuField:
    """
    Verify that StepResult.ecu is populated correctly for workspace runs
    and remains None for single-ECU runs (backward compatibility).
    """

    def _make_ecu_sim(self, responses, resp_id=0x7E8):
        """Return an async ECU simulator coroutine factory."""
        from xaloqi.tester._isotp import IsoTpEngine
        _engine = IsoTpEngine()

        async def sim(bus, stop):
            while not stop.is_set():
                r = await bus.recv(timeout=0.05)
                if r is None:
                    continue
                _, f = r
                if (f[0] >> 4) & 0xF != 0:
                    continue
                pdu = bytes(f[1:1 + (f[0] & 0xF)])
                for req, resp in responses.items():
                    if pdu == bytes(req):
                        await bus.send(resp_id, _engine.encode(bytes(resp))[0])
                        break
        return sim

    @pytest.mark.asyncio
    async def test_workspace_steps_have_ecu_field(self):
        """Each step in a workspace run records the target ECU name."""
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        gw_t, gw_e = VirtualBus.pair("ws_step_gw")
        bms_t, bms_e = VirtualBus.pair("ws_step_bms")
        stop = asyncio.Event()
        sim = self._make_ecu_sim({bytes([0x10, 0x03]): bytes([0x50, 0x03])})
        t1 = asyncio.create_task(sim(gw_e, stop))
        t2 = asyncio.create_task(sim(bms_e, stop))

        config = {
            "jobs": {
                "ws_job": {
                    "on_failure": "continue",
                    "steps": [
                        {"action": "session", "ecu": "gateway", "value": "extended"},
                        {"action": "session", "ecu": "bms",     "value": "extended"},
                    ],
                }
            }
        }
        gw = UdsTester(gw_t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)
        bms = UdsTester(bms_t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        try:
            async with gw, bms:
                executor = CampaignExecutor(
                    config, "ws.yaml", gw, verbose=False,
                    testers={"gateway": gw, "bms": bms},
                )
                result = await executor.execute_job("ws_job")
                assert result.success
                step_dicts = [s.to_dict() for s in result.steps]
                assert step_dicts[0]["ecu"] == "gateway"
                assert step_dicts[1]["ecu"] == "bms"
        finally:
            stop.set()
            t1.cancel(); t2.cancel()
            for t in [t1, t2]:
                try: await t
                except asyncio.CancelledError: pass

    @pytest.mark.asyncio
    async def test_single_ecu_steps_have_none_ecu_field(self):
        """Steps in a single-ECU run have ecu=None — backward compat."""
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        t, e = VirtualBus.pair("single_step")
        stop = asyncio.Event()
        sim = self._make_ecu_sim({bytes([0x10, 0x03]): bytes([0x50, 0x03])})
        task = asyncio.create_task(sim(e, stop))

        config = {
            "jobs": {
                "j": {
                    "on_failure": "abort",
                    "steps": [{"action": "session", "value": "extended"}],
                }
            }
        }
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        try:
            async with tester:
                executor = CampaignExecutor(config, "cfg.yaml", tester)
                result = await executor.execute_job("j")
                assert result.success
                assert result.steps[0].to_dict()["ecu"] is None
                assert result.workspace_path is None
        finally:
            stop.set(); task.cancel()
            try: await task
            except asyncio.CancelledError: pass

    @pytest.mark.asyncio
    async def test_workspace_path_in_job_result(self):
        """workspace_path is set on JobResult when run via workspace executor."""
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        t, e = VirtualBus.pair("ws_path")
        stop = asyncio.Event()
        sim = self._make_ecu_sim({bytes([0x10, 0x03]): bytes([0x50, 0x03])})
        task = asyncio.create_task(sim(e, stop))

        config = {
            "jobs": {
                "j": {
                    "on_failure": "abort",
                    "steps": [{"action": "session", "ecu": "gw", "value": "extended"}],
                }
            }
        }
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        try:
            async with tester:
                executor = CampaignExecutor(
                    config, "ws.yaml", tester,
                    testers={"gw": tester},
                )
                executor._workspace_path = "testlab_workspace.yaml"
                result = await executor.execute_job("j")
                assert result.workspace_path == "testlab_workspace.yaml"
                d = result.to_dict()
                assert d["workspace_path"] == "testlab_workspace.yaml"
        finally:
            stop.set(); task.cancel()
            try: await task
            except asyncio.CancelledError: pass

    def test_step_result_ecu_field_in_to_dict(self):
        """StepResult.to_dict() includes the ecu field."""
        step = StepResult(
            index=1, action="session",
            params={"value": "extended"},
            success=True,
            ecu="gateway",
        )
        d = step.to_dict()
        assert d["ecu"] == "gateway"

    def test_step_result_ecu_none_by_default(self):
        """StepResult.ecu defaults to None for backward compatibility."""
        step = StepResult(
            index=1, action="session",
            params={"value": "extended"},
            success=True,
        )
        assert step.ecu is None
        assert step.to_dict()["ecu"] is None


# ---------------------------------------------------------------------------
# Phase 3 — Workspace dry-run and --list support
# ---------------------------------------------------------------------------

class TestReadDtcSubFunctions:
    """
    Integration tests for read_dtc_fault_counter and read_dtc_permanent
    campaign actions — VirtualBus + ECU simulator (docker/ecu_sim/sim.py).
    """

    @pytest.fixture
    def dtc_config(self):
        return {
            **MINIMAL_CONFIG,
            "jobs": {
                "fault_counter_job": {
                    "description": "Read fault detection counters",
                    "timeout_ms": 5000,
                    "on_failure": "abort",
                    "steps": [
                        {"action": "read_dtc_fault_counter"},
                    ],
                },
                "permanent_job": {
                    "description": "Read permanent DTCs",
                    "timeout_ms": 5000,
                    "on_failure": "abort",
                    "steps": [
                        {"action": "read_dtc_permanent"},
                    ],
                },
                "combined_job": {
                    "description": "All three DTC sub-functions in sequence",
                    "timeout_ms": 10000,
                    "on_failure": "abort",
                    "steps": [
                        {"action": "read_dtc"},
                        {"action": "read_dtc_fault_counter"},
                        {"action": "read_dtc_permanent"},
                    ],
                },
            },
        }

    @pytest.mark.asyncio
    async def test_read_dtc_fault_counter_step_succeeds(self, dtc_config):
        """Campaign action read_dtc_fault_counter succeeds with empty counter list."""
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=dtc_config,
            config_path="test_config.yaml",
            jobs_to_run=["fault_counter_job"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert len(results) == 1
        result = results[0]
        assert result.success is True
        assert len(result.steps) == 1
        step = result.steps[0]
        assert step.action == "read_dtc_fault_counter"
        assert step.success is True
        assert "record_count" in step.params
        assert step.params["record_count"] == 0  # simulator has no DTCs pre-loaded

    @pytest.mark.asyncio
    async def test_read_dtc_permanent_step_succeeds(self, dtc_config):
        """Campaign action read_dtc_permanent succeeds with empty permanent list."""
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=dtc_config,
            config_path="test_config.yaml",
            jobs_to_run=["permanent_job"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert len(results) == 1
        result = results[0]
        assert result.success is True
        step = result.steps[0]
        assert step.action == "read_dtc_permanent"
        assert step.success is True
        assert "dtc_count" in step.params
        assert step.params["dtc_count"] == 0

    @pytest.mark.asyncio
    async def test_combined_dtc_job_all_steps_pass(self, dtc_config):
        """All three DTC sub-functions in a single job all succeed."""
        from xaloqi.runner import run_campaign
        results = await run_campaign(
            config=dtc_config,
            config_path="test_config.yaml",
            jobs_to_run=["combined_job"],
            interface="vcan0",
            rx_id=0x7DF,
            tx_id=0x7E8,
            timeout=0.5,
            verbose=False,
            virtual=True,
        )
        assert len(results) == 1
        result = results[0]
        assert result.success is True
        assert len(result.steps) == 3
        assert all(s.success for s in result.steps)
        assert result.steps[0].action == "read_dtc"
        assert result.steps[1].action == "read_dtc_fault_counter"
        assert result.steps[2].action == "read_dtc_permanent"

    def test_valid_actions_contains_new_entries(self):
        """VALID_ACTIONS frozenset includes both new sub-function action names."""
        from xaloqi.runner import VALID_ACTIONS
        assert "read_dtc_fault_counter" in VALID_ACTIONS
        assert "read_dtc_permanent" in VALID_ACTIONS

    def test_dry_run_accepts_new_actions(self):
        """dry_run_config reports no errors for campaigns using new action names."""
        config = {
            **MINIMAL_CONFIG,
            "jobs": {
                "test": {
                    "description": "Dry run check",
                    "timeout_ms": 5000,
                    "on_failure": "abort",
                    "steps": [
                        {"action": "read_dtc_fault_counter"},
                        {"action": "read_dtc_permanent"},
                    ],
                }
            },
        }
        rc = dry_run_config(config)
        assert rc == 0, f"dry_run_config returned non-zero exit code: {rc}"


class TestTransferData:
    """
    Unit tests for the transfer_data campaign action (_step_transfer_data).

    Uses AsyncMock to isolate the runner logic from the DFU transport layer —
    the UDS 0x34/0x36/0x37 exchange is exercised by test_uds_tester.py.
    Key regression: memory_address was hardcoded to 0 instead of being read
    from the step dict (BUG fixed: runner.py _step_transfer_data).
    """

    def _make_config(self, steps):
        return {
            **MINIMAL_CONFIG,
            "jobs": {
                "dfu_job": {
                    "description": "DFU transfer job",
                    "timeout_ms": 5000,
                    "on_failure": "abort",
                    "steps": steps,
                }
            },
        }

    @pytest.mark.asyncio
    async def test_memory_address_passed_to_transfer_firmware(self, tmp_path):
        """memory_address from step dict is forwarded to transfer_firmware."""
        from unittest.mock import AsyncMock, MagicMock
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        fw_file = tmp_path / "firmware.bin"
        fw_file.write_bytes(b"\xDE\xAD\xBE\xEF" * 4)

        t, _ = VirtualBus.pair("td_addr")
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        mock_resp = MagicMock()
        mock_resp.raw = bytes([0x77])
        tester.transfer_firmware = AsyncMock(return_value=mock_resp)

        config = self._make_config([
            {"action": "transfer_data", "file": str(fw_file), "memory_address": "0x08020000"},
        ])

        async with tester:
            executor = CampaignExecutor(config, "cfg.yaml", tester, verbose=False)
            result = await executor.execute_job("dfu_job")

        assert result.success is True
        tester.transfer_firmware.assert_awaited_once()
        _, kwargs = tester.transfer_firmware.call_args
        assert kwargs["memory_address"] == 0x08020000

    @pytest.mark.asyncio
    async def test_memory_address_defaults_to_zero(self, tmp_path):
        """When memory_address is omitted from the step, defaults to 0x00000000."""
        from unittest.mock import AsyncMock, MagicMock
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        fw_file = tmp_path / "firmware.bin"
        fw_file.write_bytes(b"\xAA\xBB" * 8)

        t, _ = VirtualBus.pair("td_default")
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        mock_resp = MagicMock()
        mock_resp.raw = bytes([0x77])
        tester.transfer_firmware = AsyncMock(return_value=mock_resp)

        config = self._make_config([
            {"action": "transfer_data", "file": str(fw_file)},
        ])

        async with tester:
            executor = CampaignExecutor(config, "cfg.yaml", tester, verbose=False)
            result = await executor.execute_job("dfu_job")

        assert result.success is True
        _, kwargs = tester.transfer_firmware.call_args
        assert kwargs["memory_address"] == 0x00000000

    @pytest.mark.asyncio
    async def test_memory_address_in_step_params(self, tmp_path):
        """memory_address is recorded in StepResult.params for reporting."""
        from unittest.mock import AsyncMock, MagicMock
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        fw_file = tmp_path / "firmware.bin"
        fw_file.write_bytes(b"\x00" * 16)

        t, _ = VirtualBus.pair("td_params")
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)

        mock_resp = MagicMock()
        mock_resp.raw = bytes([0x77])
        tester.transfer_firmware = AsyncMock(return_value=mock_resp)

        config = self._make_config([
            {"action": "transfer_data", "file": str(fw_file), "memory_address": "0x1C2000"},
        ])

        async with tester:
            executor = CampaignExecutor(config, "cfg.yaml", tester, verbose=False)
            result = await executor.execute_job("dfu_job")

        step = result.steps[0]
        assert step.success is True
        assert "memory_address" in step.params
        assert step.params["memory_address"] == hex(0x1C2000)
        assert step.params["total_bytes"] == 16

    @pytest.mark.asyncio
    async def test_missing_file_returns_failure(self, tmp_path):
        """A transfer_data step with a non-existent file fails without calling transfer_firmware."""
        from unittest.mock import AsyncMock, MagicMock
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        t, _ = VirtualBus.pair("td_nofile")
        tester = UdsTester(t, rx_id=0x7E8, tx_id=0x7DF, keepalive=False)
        tester.transfer_firmware = AsyncMock()

        config = self._make_config([
            {"action": "transfer_data", "file": str(tmp_path / "missing.bin")},
        ])

        async with tester:
            executor = CampaignExecutor(config, "cfg.yaml", tester, verbose=False)
            result = await executor.execute_job("dfu_job")

        step = result.steps[0]
        assert step.success is False
        assert "not found" in step.error
        tester.transfer_firmware.assert_not_awaited()


# ---------------------------------------------------------------------------
# Library / CLI version cross-check (TestLab#28)
# ---------------------------------------------------------------------------
# A stale editable install (pip install -e from a previous TestLab directory)
# makes `import xaloqi` resolve an older library than the CLI: campaigns pass
# against code that is not the code you extracted. The runner must refuse.


class TestLibraryVersionCheck:
    def test_matching_version_passes(self):
        import xaloqi
        from xaloqi.runner import RUNNER_VERSION, check_library_version
        assert xaloqi.__version__ == RUNNER_VERSION
        check_library_version()  # must not raise

    def test_mismatched_version_aborts_with_pointer_to_stale_install(self, capsys):
        import xaloqi
        from xaloqi.runner import RUNNER_VERSION, check_library_version
        with patch.object(xaloqi, "__version__", "1.4.0"):
            with pytest.raises(SystemExit) as excinfo:
                check_library_version()
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "version mismatch" in err
        assert "1.4.0" in err
        assert RUNNER_VERSION in err
        assert xaloqi.__file__ in err          # shows where the stale lib lives
        assert "pip uninstall xaloqi-tester" in err

    def test_missing_version_attribute_treated_as_mismatch(self, capsys):
        # Pre-1.4.1 installs have no xaloqi.__version__ at all.
        import xaloqi
        from xaloqi.runner import check_library_version
        orig = xaloqi.__version__
        del xaloqi.__version__
        try:
            with pytest.raises(SystemExit) as excinfo:
                check_library_version()
        finally:
            xaloqi.__version__ = orig
        assert excinfo.value.code == 1
        assert "unknown (pre-1.4.1)" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Skipped step status for SOVD no-op steps (validation report Bug 9)
# ---------------------------------------------------------------------------
# session and security_access are not applicable over SOVD REST. They used to
# return plain success=True, so a campaign "validating" security unlock over
# SOVD passed vacuously. They now carry skipped=True, are excluded from the
# passed count, and are reported separately in the summary.


class TestSovdSkippedSteps:
    @staticmethod
    def _sovd_executor():
        from xaloqi.runner import CampaignExecutor
        config = {"sovd": {"base_url": "http://localhost:9999/sovd"}}
        return CampaignExecutor(config, "test.yaml", tester=MagicMock())

    def test_security_access_over_sovd_is_skipped_not_passed(self):
        ex = self._sovd_executor()
        result = asyncio.run(ex._step_security_access({"level": 1}, index=1))
        assert result.skipped is True
        assert result.success is True          # a skip is not a failure
        assert "skipped" in (result.error or "")

    def test_session_over_sovd_is_skipped_not_passed(self):
        ex = self._sovd_executor()
        result = asyncio.run(ex._step_session({"value": "extended"}, index=1))
        assert result.skipped is True
        assert result.success is True

    def test_skipped_is_serialised_into_json(self):
        ex = self._sovd_executor()
        result = asyncio.run(ex._step_security_access({"level": 1}, index=1))
        d = result.to_dict()
        assert d["skipped"] is True

    def test_non_sovd_step_is_not_skipped(self):
        from xaloqi.runner import CampaignExecutor, StepResult
        ex = CampaignExecutor({}, "test.yaml", tester=MagicMock())
        assert ex._is_sovd_transport() is False
        # default for every normal StepResult
        r = StepResult(index=1, action="read_did", params={}, success=True)
        assert r.skipped is False

    def test_summary_counts_skips_separately(self):
        # Reproduce the summary computation contract on mixed results.
        from xaloqi.runner import StepResult
        steps = [
            StepResult(index=1, action="read_did", params={}, success=True),
            StepResult(index=2, action="security_access", params={},
                       success=True, skipped=True),
            StepResult(index=3, action="read_did", params={}, success=False),
        ]
        passed  = sum(1 for s in steps if s.success and not s.skipped)
        skipped = sum(1 for s in steps if s.skipped)
        assert (passed, skipped) == (1, 1)


# ---------------------------------------------------------------------------
# Virtual --all job isolation (validation report Bug 10)
# ---------------------------------------------------------------------------
# One EcuState() used to be shared across all jobs of a --all virtual run, so
# security unlock / session / DTC state bled between jobs and results depended
# on job order. Fresh state per job is now the default; --shared-ecu-state
# restores the carry-over behaviour (a real ECU not power-cycled between jobs).


class TestVirtualJobStateIsolation:
    _CAMPAIGN = {
        "jobs": {
            "unlock": {
                "description": "Enter extended session and unlock level 1.",
                "steps": [
                    {"action": "session", "value": "extended"},
                    {"action": "security_access", "level": 1},
                ],
            },
            "probe_locked": {
                "description": "F187 must still be security-locked.",
                "steps": [
                    {"action": "session", "value": "extended"},
                    {"action": "read_did", "did": "0xF187", "expect_nrc": 0x33},
                ],
            },
            "probe_unlocked": {
                "description": "F187 readable only if unlock carried over.",
                "steps": [
                    {"action": "read_did", "did": "0xF187"},
                ],
            },
        }
    }

    @staticmethod
    def _run(jobs, shared):
        from xaloqi.runner import run_campaign
        import yaml as _yaml
        cfg_path = str(_CORE_DIR / "examples" / "diagnostics_config.yaml")
        with open(cfg_path) as f:
            config = _yaml.safe_load(f)
        config["jobs"] = TestVirtualJobStateIsolation._CAMPAIGN["jobs"]
        return asyncio.run(run_campaign(
            config=config, config_path=cfg_path, jobs_to_run=jobs,
            interface="virtual", rx_id=0x7DF, tx_id=0x7E8,
            timeout=2.0, verbose=False, virtual=True,
            shared_ecu_state=shared,
        ))

    def test_fresh_state_default_does_not_leak_unlock(self):
        results = self._run(["unlock", "probe_locked"], shared=False)
        assert results[0].success, "unlock job itself must pass"
        assert results[1].success, (
            "F187 must be NRC 0x33 in a fresh-state job — the previous "
            "job's security unlock leaked across jobs"
        )

    def test_shared_ecu_state_carries_unlock_across_jobs(self):
        results = self._run(["unlock", "probe_unlocked"], shared=True)
        assert results[0].success
        assert results[1].success, (
            "with --shared-ecu-state the level-1 unlock (and extended "
            "session) from job 1 must still be active in job 2"
        )

    def test_job_order_independence_with_fresh_state(self):
        # probe_locked must behave identically whether or not an unlock
        # job ran before it.
        alone    = self._run(["probe_locked"], shared=False)
        after    = self._run(["unlock", "probe_locked"], shared=False)
        assert alone[0].success == after[-1].success == True


# ---------------------------------------------------------------------------
# 0x35 RequestUpload campaign action (TestLab#25) — end-to-end via VirtualBus
# ---------------------------------------------------------------------------

class TestRequestUploadAction:
    @staticmethod
    def _run(steps):
        from xaloqi.runner import run_campaign
        import yaml as _yaml
        cfg_path = str(_CORE_DIR / "examples" / "diagnostics_config.yaml")
        with open(cfg_path) as f:
            config = _yaml.safe_load(f)
        config["jobs"] = {"upload": {"description": "0x35 upload", "steps": steps}}
        return asyncio.run(run_campaign(
            config=config, config_path=cfg_path, jobs_to_run=["upload"],
            interface="virtual", rx_id=0x7DF, tx_id=0x7E8,
            timeout=2.0, verbose=False, virtual=True,
        ))

    def test_request_upload_in_programming_session_passes(self):
        # Sim answers 0x35 with 0x75 only in programming session (0x02).
        results = self._run([
            {"action": "session", "value": "programming"},
            {"action": "request_upload", "memory_address": "0x08000000",
             "memory_size": "0x1000"},
        ])
        steps = results[0].steps
        assert steps[-1].action == "request_upload"
        assert steps[-1].success, "0x35 should succeed in programming session"
        assert steps[-1].response_pdu.startswith("75")

    def test_request_upload_wrong_session_fails_with_nrc(self):
        # Default session → sim returns NRC 0x7F, step fails (not a crash).
        results = self._run([
            {"action": "request_upload", "memory_address": "0x08000000",
             "memory_size": "0x1000"},
        ])
        step = results[0].steps[-1]
        assert step.action == "request_upload"
        assert step.success is False
        assert step.nrc == 0x7F

    def test_request_upload_is_a_valid_action(self):
        from xaloqi.runner import VALID_ACTIONS
        assert "request_upload" in VALID_ACTIONS
