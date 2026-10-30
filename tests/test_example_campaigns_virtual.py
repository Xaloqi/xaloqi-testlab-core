# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""Every campaign shipped in the repo passes on --virtual.

The simulator now refuses what an EDS ECU refuses (TestLab#93, #94, #96), so a
shipped campaign that only passed because the simulator was lax fails here
instead of in a customer's first run. Two of them did: dtcs (ClearDiagnostic
in the default session) and firmware_download (a second RequestDownload).
"""
import asyncio
import os
from pathlib import Path

import pytest
import yaml

_CORE = Path(__file__).resolve().parent.parent
_CONFIG = _CORE / "xaloqi" / "examples" / "testlab_config.yaml"
_CAMPAIGNS = sorted([*(_CORE / "campaigns").glob("*.yaml"),
                     *(_CORE / "examples").glob("*/campaign.yaml"),
                     *(_CORE / "xaloqi" / "examples" / "campaigns").glob("*.yaml")])
_JOBS = [(c, j) for c in _CAMPAIGNS for j in yaml.safe_load(c.read_text())["jobs"]]


@pytest.mark.skipif(not (_CORE / "examples").is_dir(), reason="needs a repo checkout (_repo_checkout)")
@pytest.mark.parametrize("campaign,job", _JOBS, ids=lambda v: v.name if isinstance(v, Path) else v)
def test_shipped_campaign_passes_virtual(campaign, job, monkeypatch):
    from xaloqi.runner import run_campaign

    monkeypatch.chdir(_CORE)  # example paths (examples/.../firmware.bin) are relative to core/
    config = yaml.safe_load(_CONFIG.read_text())
    config.update({k: v for k, v in yaml.safe_load(campaign.read_text()).items() if k != "jobs"})
    config["jobs"] = {job: yaml.safe_load(campaign.read_text())["jobs"][job]}
    results = asyncio.run(run_campaign(
        config=config, config_path=str(_CONFIG), jobs_to_run=[job],
        interface="virtual", rx_id=0x7DF, tx_id=0x7E8, timeout=2.0, verbose=False, virtual=True))
    failed = [(s.index, s.action, s.error) for r in results for s in r.steps if not s.success]
    assert results and not failed, failed
