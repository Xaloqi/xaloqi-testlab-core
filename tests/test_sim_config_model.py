# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""The simulator built from an ECU config, tied to the real EDS stack.

With an EcuModel (from an EDS diagnostics_config.yaml) the simulator applies the
per-DID and per-routine rules the stack applies after the access table. The
fixture holds the compiled stack's answer for each probe, built from the same
config (fixtures/basic_ecu_diagnostics_config.yaml, the EDS basic_ecu example).

Origin: TestLab#95.
"""
import json
from pathlib import Path

import pytest

from xaloqi import sim
from xaloqi.tester._security import derive_key

_FIX = Path(__file__).parent / "fixtures"
_CONFIG = _FIX / "basic_ecu_diagnostics_config.yaml"
_PROBES = json.loads((_FIX / "stack_config_probes.json").read_text())["probes"]


def _klass(rsp) -> str:
    if rsp is None:
        return "none"
    return f"NRC:{rsp[2]:02X}" if rsp[0] == 0x7F else "pos"


def _step(state, step):
    if step.startswith("UNLOCK"):
        level = int(step[6:] or 1)
        seed = sim.handle_request(bytes([0x27, 2 * level - 1]), state, False)
        key = derive_key(bytes(seed[2:]), level, quiet=True)
        assert sim.handle_request(bytes([0x27, 2 * level]) + key, state, False)[0] == 0x67
    else:
        sim.handle_request(bytes.fromhex(step.replace(" ", "")), state, False)


@pytest.mark.parametrize("probe", _PROBES, ids=lambda p: p["name"])
def test_config_model_matches_stack(probe):
    state = sim.EcuState(sim.EcuModel.from_config(_CONFIG))
    for step in probe["steps"]:
        _step(state, step)
    got = _klass(sim.handle_request(bytes.fromhex(probe["request"]), state, False))
    assert got == probe["stack"], f"stack answers {probe['stack']}, simulator {got}"


def test_without_a_model_the_built_in_dids_stay():
    """Default behaviour is unchanged when no config is given."""
    state = sim.EcuState()
    assert set(state.dids) == {0xF190, 0xF18C, 0xF187, 0x2001}
    assert state.model is None
    state.session = 0x03
    state.security_unlocked = True
    # writable and creatable, as before (TestLab#95 documents why)
    assert sim.handle_request(bytes.fromhex("2E123401"), state, False)[0] == 0x6E


def test_model_reads_ids_given_as_numbers_and_strings():
    m = sim.EcuModel.from_config({"dids": [{"id": 0xF190, "data_length": 3, "access": ["read", "write"]},
                                           {"id": "0x0414", "data_length": 1}],
                                  "routines": [{"id": "0xFF00", "min_session": "extended"}]})
    assert set(m.dids) == {0xF190, 0x0414} and m.dids[0xF190].writable and not m.dids[0x0414].writable
    assert m.routines[0xFF00].min_session == 0x03


def test_model_keeps_the_built_in_value_when_the_length_agrees():
    state = sim.EcuState(sim.EcuModel.from_config(_CONFIG))
    assert state.dids[0xF190] == b"1HGBH41JXMN109186"      # VIN, 17 bytes in both
    assert state.dids[0x0500] == bytes(1)                  # not built in: zeros
    assert 0x2001 not in state.dids                        # not in this ECU's config


_CAMPAIGN = """
jobs:
  rules:
    description: per-DID and per-routine rules
    steps:
      - {action: read_did, did: "0xF187", expect_nrc: "0x31"}      # min_session extended
      - {action: session, value: extended}
      - {action: security_access, level: 1}
      - {action: write_did, did: "0xF190", data: "0x00", expect_nrc: "0x31"}   # read-only
      - {action: write_did, did: "0xF187", data: "0x0102", expect_nrc: "0x13"}  # wrong length
      - {action: routine, id: "0x1234", sub_fn: start, expect_nrc: "0x31"}                     # not in config
"""


def _run_cli(monkeypatch, tmp_path, *extra):
    from xaloqi import runner
    campaign = tmp_path / "c.yaml"
    campaign.write_text(_CAMPAIGN)
    monkeypatch.setattr("sys.argv", ["testlab-run", "--config", str(_CONFIG), "--campaign", str(campaign),
                                     "--job", "rules", "--virtual", *extra])
    return runner.main()


def test_runner_flag_applies_the_config_rules(monkeypatch, tmp_path):
    assert _run_cli(monkeypatch, tmp_path, "--sim-from-config") == 0


def test_runner_without_the_flag_keeps_the_built_in_set(monkeypatch, tmp_path):
    # the built-in simulator reads F187 in the default session (after unlock only) and
    # writes any DID, so the same campaign fails: that is the lax behaviour #95 describes
    assert _run_cli(monkeypatch, tmp_path) != 0


def test_runner_flag_needs_virtual_and_a_dids_list(monkeypatch, tmp_path, capsys):
    from xaloqi import runner
    cfg = tmp_path / "no_dids.yaml"
    cfg.write_text("jobs:\n  j:\n    steps: []\n")
    monkeypatch.setattr("sys.argv", ["testlab-run", "--config", str(cfg), "--job", "j", "--virtual",
                                     "--sim-from-config"])
    assert runner.main() == 1
    assert "`dids:`" in capsys.readouterr().err
