# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""xaloqi.tester.transport.__all__ lists only names importable in the installed
tier (xaloqi-testlab-core#13, TestLab#99): the free names plus the Pro names
whose import succeeds. So `import *` works on a free install, and with Pro it
includes DoipBus and the other Pro transports (see pro/tests for that half)."""
import sys

import pytest

from xaloqi.tester import transport

FREE = {"CanBus", "VirtualBus"}


@pytest.fixture
def pro_absent(monkeypatch):
    """Make every import of xaloqi_tester_pro fail, whether or not it is installed."""
    for name in [m for m in sys.modules if m.split(".")[0] == "xaloqi_tester_pro"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "xaloqi_tester_pro", None)


def _star_import() -> dict:
    ns: dict = {}
    exec("from xaloqi.tester.transport import *", ns)
    return {k for k in ns if not k.startswith("__")}


def test_star_import_succeeds_without_pro(pro_absent):
    assert _star_import() == FREE


def test_all_has_no_pro_names_without_pro(pro_absent):
    assert set(transport.__all__) == FREE
    assert not set(transport.__all__) & set(transport._PRO_EXPORTS)


def test_pro_name_still_raises_helpful_error_without_pro(pro_absent):
    with pytest.raises(ImportError, match="Xaloqi TestLab Pro"):
        transport.DoipBus


def test_all_is_free_names_plus_importable_pro_names():
    pro_names = set(transport.__all__) - FREE
    assert pro_names <= set(transport._PRO_EXPORTS)
    for name in pro_names:
        getattr(transport, name)  # every listed name resolves
    for name in set(transport._PRO_EXPORTS) - pro_names:
        with pytest.raises(ImportError):
            getattr(transport, name)  # every omitted name really is unavailable
