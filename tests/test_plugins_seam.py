# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""Tests for the core↔pro plugin seam (xaloqi.tester._plugins).

These run in every install config. The behaviour they pin down:

- A paid feature that is genuinely absent produces the "part of Xaloqi
  TestLab Pro" message.
- A paid feature whose provider IS installed but failed to load produces a
  DIFFERENT message naming the real failure. Telling a paying customer to
  buy what they already own — while hiding a version-lockstep mismatch — is
  the run-008 failure class (a silent environment fault with no signal).
"""

import pytest

from xaloqi.tester import _plugins


@pytest.fixture(autouse=True)
def _restore_registry():
    """Each test gets a clean seam and leaves the process state as it found it."""
    saved = ({k: dict(v) for k, v in _plugins._registry.items()},
             _plugins._discovered, _plugins._load_error)
    yield
    _plugins._registry.clear()
    _plugins._registry.update(saved[0])
    _plugins._discovered = saved[1]
    _plugins._load_error = saved[2]


class TestProMissingMessage:
    def test_absent_feature_says_part_of_pro(self):
        _plugins.reset()
        _plugins._discovered = True          # pretend discovery ran, found nothing
        msg = _plugins.pro_missing_message("runner_actions", "someip_call")
        assert "part of Xaloqi TestLab Pro" in msg
        assert _plugins.PRO_URL in msg

    def test_broken_install_does_not_tell_the_user_to_buy_it(self):
        """A load failure must surface the real error, not a purchase pitch."""
        _plugins.reset()
        _plugins._discovered = True
        _plugins._load_error = (
            "ImportError: Version mismatch: xaloqi-tester-pro v1.6.0 requires "
            "xaloqi-tester 1.6.x, but found v1.5.0"
        )
        msg = _plugins.pro_missing_message("runner_actions", "someip_call")
        assert "installed but failed to load" in msg
        assert "Version mismatch" in msg
        assert "Install the xaloqi-tester-pro wheel" not in msg

    def test_every_kind_has_a_label(self):
        _plugins.reset()
        _plugins._discovered = True
        for kind, names in _plugins._PRO_NAMES.items():
            msg = _plugins.pro_missing_message(kind, sorted(names)[0])
            assert "Feature '" not in msg, f"{kind} fell through to the generic label"

    def test_unknown_name_is_not_advertised_as_pro(self):
        _plugins.reset()
        _plugins._discovered = True
        msg = _plugins.pro_missing_message("runner_actions", "totally_made_up")
        assert "Pro" not in msg


class TestRegistry:
    def test_registered_action_is_discoverable(self):
        _plugins.reset()
        _plugins._discovered = True
        _plugins.register("runner_actions", "demo_action", lambda *a: None)
        assert "demo_action" in _plugins.get_runner_actions()

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="Unknown plugin kind"):
            _plugins.register("not_a_group", "x", object())

    def test_missing_transport_raises_transport_error_with_pro_message(self):
        from xaloqi.tester.exceptions import TransportError

        _plugins.reset()
        _plugins._discovered = True
        with pytest.raises(TransportError, match="Xaloqi TestLab Pro"):
            _plugins.get_transport("socketcan")
