# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
tests/test_examples_bundled.py — O-11 (free-tier launch blocker) regression.

`xaloqi-sim --demo` used to end with "Next: run a full YAML campaign with
`testlab-run --virtual`" — a command that fails with "--config or --workspace
is required" for a pip-installed user, because the wheel shipped zero example
campaign/config YAML. These tests pin two things so it can't regress silently:

  1. `xaloqi.examples` (config + campaign) is actually resolvable as package
     data — i.e. present in an installed wheel, not just the repo checkout.
  2. The exact command `xaloqi-sim --demo` prints afterwards runs clean, end
     to end, over VirtualBus.

No hardware, no license key, no network required.
"""
from __future__ import annotations

import json
from importlib.resources import files as resource_files
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Repo-checkout-only guard (same pattern as test_version_lockstep.py): the
# core-only CI job copies just tests/ + examples/ into a throwaway dir to
# test the *installed* package, with no core/pyproject.toml or repo-root
# testlab_config.yaml alongside it. Skip the checkout-comparison tests there.
_repo_checkout = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").exists(),
    reason="repo checkout only",
)


def _examples_dir() -> Path:
    resolved = resource_files("xaloqi.examples")
    assert isinstance(resolved, Path), (
        "xaloqi.examples resolved to a non-filesystem resource "
        f"({type(resolved).__name__}); the bundled examples must be plain "
        "files so `xaloqi-sim --demo` can print a usable path"
    )
    return resolved


# ---------------------------------------------------------------------------
# 1. The package data is actually there
# ---------------------------------------------------------------------------

def test_examples_config_is_packaged_and_resolvable():
    config = _examples_dir() / "testlab_config.yaml"
    assert config.is_file(), f"missing bundled example config: {config}"
    data = yaml.safe_load(config.read_text())
    assert "rx_id" in data and "tx_id" in data


def test_examples_campaign_is_packaged_and_resolvable():
    campaign = _examples_dir() / "campaigns" / "standalone_validation.yaml"
    assert campaign.is_file(), f"missing bundled example campaign: {campaign}"
    data = yaml.safe_load(campaign.read_text())
    assert "basic_validation" in data.get("jobs", {})


@_repo_checkout
def test_examples_pyproject_declares_package_data():
    """Guard the packaging config itself — without this, `python -m build`
    silently drops the YAML from the wheel even though it's on disk in the
    repo checkout, which is exactly how O-11 happened the first time."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "xaloqi.examples" in pyproject
    assert '"*.yaml"' in pyproject or "'*.yaml'" in pyproject


# ---------------------------------------------------------------------------
# 2. The bundled copy matches the repo-root dev copy (drift guard — see
#    xaloqi/examples/__init__.py docstring)
# ---------------------------------------------------------------------------

@_repo_checkout
def test_bundled_config_matches_repo_root_dev_copy():
    bundled = (_examples_dir() / "testlab_config.yaml").read_text()
    dev_copy = (REPO_ROOT / "testlab_config.yaml").read_text()
    assert bundled == dev_copy


@_repo_checkout
def test_bundled_campaign_matches_repo_root_dev_copy():
    bundled = (_examples_dir() / "campaigns" / "standalone_validation.yaml").read_text()
    dev_copy = (REPO_ROOT / "campaigns" / "standalone_validation.yaml").read_text()
    assert bundled == dev_copy


# ---------------------------------------------------------------------------
# 3. `xaloqi-sim --demo`'s printed "Next:" command actually works
# ---------------------------------------------------------------------------

def test_demo_next_command_points_at_real_files():
    from xaloqi.sim import _demo_next_command

    lines = _demo_next_command()
    joined = " ".join(lines)
    assert "--config" in joined and "--campaign" in joined
    assert "testlab_config.yaml" in joined
    assert "standalone_validation.yaml" in joined
    # No leftover instruction that fails per O-11 (bare `--virtual`, nothing else)
    assert "--config or --workspace is required" not in joined


def test_demo_next_command_runs_clean_end_to_end(tmp_path):
    """Reproduce exactly what a pip-installed user gets by pasting the
    demo's printed command, via the real runner entry point.

    Not async: `runner_mod.main()` drives its own event loop internally
    (`asyncio.run(...)`), so this must run outside pytest-asyncio's loop."""
    import sys
    from xaloqi.sim import _demo_next_command
    from xaloqi import runner as runner_mod

    lines = _demo_next_command()
    config = str(_examples_dir() / "testlab_config.yaml")
    campaign = str(_examples_dir() / "campaigns" / "standalone_validation.yaml")
    assert config in " ".join(lines) and campaign in " ".join(lines), (
        "test fixture drifted from _demo_next_command()'s actual output"
    )

    out_json = tmp_path / "demo_next.json"
    argv = [
        "testlab-run",
        "--config", config,
        "--campaign", campaign,
        "--job", "basic_validation",
        "--virtual",
        "--json", str(out_json),
    ]
    old_argv = sys.argv
    sys.argv = argv
    try:
        rc = runner_mod.main()
    finally:
        sys.argv = old_argv

    assert rc == 0, "the demo's own 'Next:' command must exit 0"
    assert out_json.is_file()
    report = json.loads(out_json.read_text())
    runs = report.get("runs") or []
    assert runs, f"expected at least one job run in {out_json}"
    assert runs[0]["job_name"] == "basic_validation"
    assert runs[0]["success"] is True
