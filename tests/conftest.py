# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
import sys
from pathlib import Path
import pytest

# Ensure the core package is imported over any stale system/user install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
