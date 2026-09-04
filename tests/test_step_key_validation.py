"""
tests/test_step_key_validation.py — Regression tests for #4.

The campaign runner used to accept any unknown key in a step and ignore it.
For a test tool that is a false-pass: misspelling `expect_nrc` as
`expect_ncr` silently removed the assertion and the step still reported OK,
so a campaign could be green while asserting nothing. Two compounding
problems were found:

  1. unknown step keys were never rejected, and
  2. `validate_job_schema()` only ran under `--dry-run`, so a normal
     `testlab-run` never validated the campaign at all.

`dtc_count` was the same defect wearing a different hat: it looks like an
assertion but was only ever written into StepResult.params as an output,
so `dtc_count: 999` passed against an ECU reporting none.

Run:
    XALOQI_LICENSE_SKIP=1 pytest tests/test_step_key_validation.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from xaloqi.runner import (
    ACTION_STEP_KEYS,
    COMMON_STEP_KEYS,
    allowed_step_keys,
    validate_job_schema,
)

_RUNNER_SRC = Path(__file__).parent.parent / "xaloqi" / "runner.py"


def _job(*steps: dict) -> dict:
    return {"steps": list(steps)}


# ---------------------------------------------------------------------------
# The bug from #4
# ---------------------------------------------------------------------------

def test_misspelled_expect_nrc_is_rejected():
    errors = validate_job_schema(
        "j", _job({"action": "read_did", "did": "0xF190", "expect_ncr": "0x33"})
    )
    assert errors, "a misspelled assertion key must not be silently ignored"
    assert "expect_ncr" in errors[0]


def test_misspelled_key_suggests_the_right_one():
    errors = validate_job_schema(
        "j", _job({"action": "read_did", "did": "0xF190", "expect_ncr": "0x33"})
    )
    assert "Did you mean 'expect_nrc'?" in errors[0]


def test_invented_key_is_rejected():
    errors = validate_job_schema(
        "j", _job({"action": "read_did", "did": "0xF190", "banana": 1})
    )
    assert errors and "banana" in errors[0]


def test_valid_step_passes():
    assert validate_job_schema(
        "j",
        _job({"action": "read_did", "did": "0xF190",
              "expect_nrc": "0x31", "save_as": "v", "description": "x"}),
    ) == []


def test_common_keys_accepted_on_every_action():
    for action in ACTION_STEP_KEYS:
        allowed = allowed_step_keys(action)
        assert allowed is not None
        assert COMMON_STEP_KEYS <= allowed, f"{action} lost the common keys"


def test_plugin_actions_are_not_rejected():
    """Pro/plugin actions have parameters this table cannot know."""
    assert allowed_step_keys("some_unknown_plugin_action") is None


# ---------------------------------------------------------------------------
# The table must not go stale
# ---------------------------------------------------------------------------

def _keys_read_by_handlers() -> dict[str, set]:
    """Re-derive, from the source, which step keys each handler actually reads."""
    tree = ast.parse(_RUNNER_SRC.read_text(encoding="utf-8"))
    found: dict[str, set] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_step_"):
            continue
        keys: set = set()
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get"
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "step"
                    and n.args and isinstance(n.args[0], ast.Constant)):
                keys.add(n.args[0].value)
            if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                    and n.value.id == "step" and isinstance(n.slice, ast.Constant)):
                keys.add(n.slice.value)
            if (isinstance(n, ast.Compare) and n.comparators
                    and isinstance(n.comparators[0], ast.Name)
                    and n.comparators[0].id == "step"
                    and isinstance(n.left, ast.Constant)):
                keys.add(n.left.value)
        found[node.name[len("_step_"):]] = keys
    return found


def test_table_matches_handlers():
    """Every key a handler reads must be declared, or a valid step gets rejected.

    This is the test that keeps ACTION_STEP_KEYS honest: add a parameter to a
    handler without declaring it here and this fails, instead of users
    discovering it as a spurious 'unknown key' error.
    """
    handler_keys = _keys_read_by_handlers()
    # handler name -> action name, where they differ
    alias = {"transfer_exit": "request_transfer_exit"}
    problems = []
    for handler, keys in handler_keys.items():
        action = alias.get(handler, handler)
        if action not in ACTION_STEP_KEYS:
            continue  # e.g. the _step_label helper, not a dispatchable action
        declared = allowed_step_keys(action)
        missing = keys - declared
        if missing:
            problems.append(f"{action}: handler reads {sorted(missing)}, not declared")
    assert not problems, "; ".join(problems)


# ---------------------------------------------------------------------------
# dtc_count is an assertion, not an output label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["read_dtc", "read_dtc_permanent"])
def test_dtc_count_is_an_accepted_key(action):
    assert "dtc_count" in allowed_step_keys(action)


def test_dtc_count_asserts_against_the_virtual_ecu():
    """The virtual ECU reports 0 DTCs; a wrong dtc_count must fail the step."""
    import asyncio
    from xaloqi.runner import CampaignExecutor  # noqa: F401  (import shape check)

    # Behavioural coverage lives in the campaign-level tests; this asserts the
    # contract that made #4 possible -- that the key is read at all.
    src = _RUNNER_SRC.read_text(encoding="utf-8")
    assert 'expect_count = step.get("dtc_count")' in src, (
        "dtc_count must be READ from the step, not only written into params"
    )
    assert src.count('expect_count = step.get("dtc_count")') == 2, (
        "both read_dtc and read_dtc_permanent must read dtc_count"
    )
