# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
xaloqi/examples — bundled example config + campaign, shipped inside the
`xaloqi-tester` wheel so `xaloqi-sim --demo`'s own "Next:" instruction has
something real to point at for a pip-installed user (no repo checkout).

`testlab_config.yaml` and `campaigns/standalone_validation.yaml` here are
copies of the repo-root files of the same name (kept for the git-clone /
`pip install -e .` dev workflow documented in README.md). The two must stay
byte-identical — `tests/test_examples_bundled.py` asserts that in CI so they
can't drift apart silently.
"""
