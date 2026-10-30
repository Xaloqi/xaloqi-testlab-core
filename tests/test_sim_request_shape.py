# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""The simulator's request handling, tied to the real EDS stack.

test_sim_acl.py ties the session / security gates. This ties what comes after
the gate: length (NRC 0x13), sub-function (0x12), value (0x31), sequence
(0x24 / 0x73), session transitions (0x22) and the suppress bit. The fixture
holds the compiled stack's answer, as a class (positive / none / NRC:xx), for
each probe. The simulator must give the same class.

Where the simulator deliberately differs, the probe is listed in _KNOWN with
the reason and the issue. A listed probe must still differ (strict), so the
entry is removed when the difference is fixed.

Origin: TestLab#94 and #96.
"""
import json
from pathlib import Path

import pytest

from xaloqi import sim
from xaloqi.tester._security import derive_key

_PROBES = json.loads((Path(__file__).parent / "fixtures" / "stack_request_probes.json").read_text())["probes"]

_RESIDUAL = "TestLab#102"
_MEMORY_MAP = "the simulator cannot know the target ECU's memory map or DIDs (#95)"
_DID_MODEL = "per-DID / per-routine rules need the ECU config (#95)"
_0X19 = f"0x19 sub-functions the simulator does not implement, or checks differently ({_RESIDUAL})"
_NO_MEMORY = ("the harness build has no readable / upload region, so the stack answers 0x22 "
              f"to every well-formed 0x23 / 0x35; the simulator serves its 4 KiB region ({_RESIDUAL})")
_ALFID0 = ("ALFID 0x00 is 0x13 on the stack, 0x31 in the simulator; ISO 14229-1 says 0x31, "
           f"so not copied (run-049) ({_RESIDUAL})")

# probe name -> reason. Names are the fixture's.
_KNOWN: dict[str, str] = {}
_KNOWN.update({n: _0X19 for n in (
    "19 01", "19 0100", "19 0300", "19 010000", "19 030000", "19 04", "19 0400", "19 8100",
    *(f"19 sub {x}" for x in ("01", "03", "04", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "81", "83")))})
_KNOWN.update({n: _NO_MEMORY for n in (
    *(f"23 {t}" for t in ("00", "01", "0000", "0100", "0300", "000000", "010000", "030000", "04", "0400", "80", "8000", "8100")),
    *(f"35 {t}" for t in ("0000", "0100", "0300", "000000", "010000", "030000", "0400", "8000", "8100")))})
_KNOWN.update({n: _DID_MODEL for n in (
    "2E 000000", "2E 010000", "2E 030000", "31 000000", "31 010000", "31 030000", "27 both levels, write")})
_KNOWN.update({n: _ALFID0 for n in ("34 0000", "34 000000", "3D 000000", "3D 010000", "3D 030000")})
_KNOWN["34 outside memory"] = _MEMORY_MAP


def _known(name: str) -> str | None:
    return _KNOWN.get(name)


def _unlock(state, level=1):
    seed = sim.handle_request(bytes([0x27, 2 * level - 1]), state, False)
    assert seed[0] == 0x67, seed.hex()
    key = derive_key(bytes(seed[2:]), level, quiet=True)
    assert sim.handle_request(bytes([0x27, 2 * level]) + key, state, False)[0] == 0x67


def _klass(rsp) -> str:
    if rsp is None:
        return "none"
    return f"NRC:{rsp[2]:02X}" if rsp[0] == 0x7F else "pos"


def _run(probe) -> str:
    state = sim.EcuState()
    for step in probe["steps"]:
        if step.startswith("UNLOCK"):
            _unlock(state, int(step[6:] or 1))
        else:
            sim.handle_request(bytes.fromhex(step.replace(" ", "")), state, False)
    return _klass(sim.handle_request(bytes.fromhex(probe["request"]), state, False))


@pytest.mark.parametrize("probe", _PROBES, ids=lambda p: p["name"])
def test_request_shape_matches_stack(probe):
    got = _run(probe)
    reason = _known(probe["name"])
    if reason is not None:
        assert got != probe["stack"], (
            f"{probe['name']!r} now matches the stack ({got}); remove it from _KNOWN ({reason})")
        pytest.xfail(reason)
    assert got == probe["stack"], f"stack answers {probe['stack']}, simulator {got}"
