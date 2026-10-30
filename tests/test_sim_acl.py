# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""The simulator's session/security gates, tied to the real EDS stack.

xaloqi.sim is a model of an ECU. Checks written against it are only as good as
the model, so the model is compared with the thing it models: the fixture
stack_acl_matrix.json records, for every service in the EDS default access
table and every session / security state, whether the stack's gate refused the
request (NRC 0x7F or 0x33) or let it through. The maintainers regenerate it from
the compiled stack (tools/sim_acl_fixture.py in the source repository) when the
EDS access table changes.

Origin: the free simulator answered WriteDataByIdentifier without
SecurityAccess, so a campaign step asserting the gate passed on --virtual and
failed on the ECU (TestLab#93).
"""
import json
from pathlib import Path

import pytest

from xaloqi import sim
from xaloqi.tester._security import derive_key

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "stack_acl_matrix.json").read_text())
_SESSION = {"default": 0x01, "programming": 0x02, "extended": 0x03}

def _send(state, pdu):
    return sim.handle_request(bytes(pdu), state, False)


def _unlock(state, level=1):
    seed = _send(state, [0x27, 2 * level - 1])
    assert seed[0] == 0x67, seed.hex()
    key = derive_key(bytes(seed[2:]), level, quiet=True)
    assert _send(state, bytes([0x27, 2 * level]) + key)[0] == 0x67


def _gate(rsp):
    if rsp and rsp[0] == 0x7F and len(rsp) == 3 and rsp[2] in (0x7F, 0x33):
        return f"{rsp[2]:02X}"
    return "pass"


def _cell_id(c):
    return f"{c['sid']:02X}-{c['kind']}-{c['session']}-{'unlocked' if c['unlocked'] else 'locked'}"


@pytest.mark.parametrize("cell", _FIXTURE["cells"], ids=_cell_id)
def test_gate_matches_stack(cell):
    state = sim.EcuState()
    if _SESSION[cell["session"]] != 0x01:
        assert _send(state, [0x10, _SESSION[cell["session"]]])[0] == 0x50
    if cell["unlocked"]:
        _unlock(state)
    rsp = _send(state, bytes.fromhex(cell["request"]))
    assert _gate(rsp) == cell["gate"], (
        f"stack gate={cell['gate']} but sim answered {rsp.hex() if rsp else None}")


@pytest.mark.parametrize("t", _FIXTURE["transitions"], ids=lambda t: t["name"])
def test_session_change_relocks_like_stack(t):
    state = sim.EcuState()
    for step in t["steps"]:
        if step == "UNLOCK":
            _unlock(state)
        else:
            assert _send(state, bytes.fromhex(step.replace(" ", "")))[0] == 0x50
    rsp = _send(state, bytes.fromhex(t["request"]))
    assert _gate(rsp) == t["gate"], rsp.hex()


def test_fixture_covers_every_gated_service():
    """A service gated in the stack's table must be in the fixture, and every
    service the fixture says is gated must be gated by the simulator."""
    gated_in_stack = {c["sid"] for c in _FIXTURE["cells"] if c["gate"] != "pass"}
    assert gated_in_stack == {0x14, 0x23, 0x27, 0x28, 0x2A, 0x2E, 0x2F,
                              0x34, 0x35, 0x36, 0x37, 0x3D, 0x85}
    assert set(sim.SERVICE_ACL) == gated_in_stack
