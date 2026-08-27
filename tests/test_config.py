# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
tests/test_config.py — Comprehensive tests for _config.py.

Covers:
  - load_testlab_config: all fields, defaults, edge cases, validation errors
  - load_eds_config: all fields, defaults, nested sections, validation errors
  - load_config: auto-detection, ambiguous files, error propagation
  - _parse_hex: hex strings, decimal strings, integers, bad input
  - _parse_did / _parse_dtc: field validation, error messages
  - End-to-end: UdsTester.from_config with both formats (VirtualBus, no hardware)
"""

from __future__ import annotations

import os
import textwrap
import tempfile
import asyncio
import pytest

from xaloqi.tester._config import (
    load_config, load_eds_config, load_testlab_config,
    DidDef, DtcDef, TesterConfig,
    _parse_hex, _VALID_SESSIONS, _VALID_SEVERITIES,
)
from xaloqi.tester.exceptions import ConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_tmp(content: str) -> str:
    """Write content to a temp YAML file and return its path."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    f.write(textwrap.dedent(content))
    f.close()
    return f.name


def tmp(content: str):
    """Context-manager-style: yields path, unlinks on exit."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        p = write_tmp(content)
        try:
            yield p
        finally:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    return _ctx()


# ---------------------------------------------------------------------------
# Fixture YAML strings
# ---------------------------------------------------------------------------

TESTLAB_FULL = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
p2_timeout_ms: 150
ecu_name: TestECU
dids:
  - id: "0xF190"
    name: VIN
    data_length: 17
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
  - id: "0xF187"
    name: PartNumber
    data_length: 16
    access: [read, write]
    min_session: extended
    read_security_level: 1
    write_security_level: 1
dtcs:
  - code: "0xC00100"
    description: CAN loss
    severity: check_at_next_halt
  - code: "0xC00200"
    description: Voltage fault
    severity: immediate
"""

TESTLAB_MINIMAL = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
"""

EDS_FULL = """
metadata:
  ecu_name: BmsEcu
  version: "1.0"
can:
  interface: vcan0
  rx_can_id: "0x7DF"
  tx_can_id: "0x7E8"
timing:
  p2_server_max_ms: 25
dids:
  - id: "0x2001"
    name: CellVoltage
    data_length: 2
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
dtcs:
  - code: "0xC00100"
    description: CAN loss
    severity: check_at_next_halt
"""

EDS_NO_SECTIONS = """
metadata:
  ecu_name: MinimalEcu
dids: []
dtcs: []
"""


# ---------------------------------------------------------------------------
# _parse_hex
# ---------------------------------------------------------------------------

class TestParseHex:
    def test_hex_string_with_prefix(self):
        assert _parse_hex("0x7DF", 0, "x") == 0x7DF

    def test_hex_string_uppercase(self):
        assert _parse_hex("0X7DF", 0, "x") == 0x7DF

    def test_decimal_string(self):
        assert _parse_hex("2015", 0, "x") == 2015

    def test_integer_passthrough(self):
        assert _parse_hex(0x7DF, 0, "x") == 0x7DF

    def test_none_returns_default(self):
        assert _parse_hex(None, 0x7DF, "x") == 0x7DF

    def test_invalid_string_raises(self):
        with pytest.raises(ConfigError, match="Cannot parse CAN ID"):
            _parse_hex("NOTAHEX", 0, "rx_id")

    def test_error_message_includes_field_name(self):
        with pytest.raises(ConfigError, match="rx_id"):
            _parse_hex("bad", 0, "rx_id")

    def test_zero(self):
        assert _parse_hex("0x000", 0, "x") == 0

    def test_max_11bit(self):
        assert _parse_hex("0x7FF", 0, "x") == 0x7FF


# ---------------------------------------------------------------------------
# load_testlab_config — happy path
# ---------------------------------------------------------------------------

class TestLoadTestlabConfigHappyPath:
    def test_full_config_all_fields(self):
        with tmp(TESTLAB_FULL) as p:
            cfg = load_testlab_config(p)
        assert cfg.interface == "vcan0"
        assert cfg.rx_id == 0x7DF
        assert cfg.tx_id == 0x7E8
        assert cfg.p2_timeout_ms == 150
        assert cfg.ecu_name == "TestECU"

    def test_dids_parsed_correctly(self):
        with tmp(TESTLAB_FULL) as p:
            cfg = load_testlab_config(p)
        assert len(cfg.dids) == 2
        vin = cfg.dids[0]
        assert vin.id == 0xF190
        assert vin.name == "VIN"
        assert vin.data_length == 17
        assert vin.access == ["read"]
        assert vin.min_session == "default"
        assert vin.read_security_level == 0
        assert vin.write_security_level == 0

    def test_did_with_write_access(self):
        with tmp(TESTLAB_FULL) as p:
            cfg = load_testlab_config(p)
        part = cfg.dids[1]
        assert part.id == 0xF187
        assert "write" in part.access
        assert part.min_session == "extended"
        assert part.read_security_level == 1
        assert part.write_security_level == 1

    def test_dtcs_parsed_correctly(self):
        with tmp(TESTLAB_FULL) as p:
            cfg = load_testlab_config(p)
        assert len(cfg.dtcs) == 2
        assert cfg.dtcs[0].code == 0xC00100
        assert cfg.dtcs[0].severity == "check_at_next_halt"
        assert cfg.dtcs[1].severity == "immediate"

    def test_minimal_config_uses_defaults(self):
        with tmp(TESTLAB_MINIMAL) as p:
            cfg = load_testlab_config(p)
        assert cfg.interface == "vcan0"
        assert cfg.rx_id == 0x7DF
        assert cfg.tx_id == 0x7E8
        assert cfg.p2_timeout_ms == 150
        assert cfg.ecu_name == ""
        assert cfg.dids == []
        assert cfg.dtcs == []

    def test_integer_can_ids(self):
        with tmp("interface: vcan0\nrx_id: 2015\ntx_id: 2024\n") as p:
            cfg = load_testlab_config(p)
        assert cfg.rx_id == 2015
        assert cfg.tx_id == 2024

    def test_no_dids_no_dtcs(self):
        with tmp("interface: can0\nrx_id: \"0x600\"\ntx_id: \"0x601\"\n") as p:
            cfg = load_testlab_config(p)
        assert cfg.interface == "can0"
        assert cfg.rx_id == 0x600
        assert cfg.dids == []
        assert cfg.dtcs == []

    def test_custom_p2_timeout(self):
        with tmp("interface: vcan0\nrx_id: \"0x7DF\"\ntx_id: \"0x7E8\"\np2_timeout_ms: 500\n") as p:
            cfg = load_testlab_config(p)
        assert cfg.p2_timeout_ms == 500

    def test_all_valid_min_sessions(self):
        for session in sorted(_VALID_SESSIONS):
            yaml = f"""
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: X
    data_length: 4
    access: [read]
    min_session: {session}
    read_security_level: 0
    write_security_level: 0
"""
            with tmp(yaml) as p:
                cfg = load_testlab_config(p)
            assert cfg.dids[0].min_session == session

    def test_all_valid_severities(self):
        for sev in sorted(_VALID_SEVERITIES):
            yaml = f"""
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dtcs:
  - code: "0xC00100"
    description: test
    severity: {sev}
"""
            with tmp(yaml) as p:
                cfg = load_testlab_config(p)
            assert cfg.dtcs[0].severity == sev

    def test_did_access_as_string_not_list(self):
        """access: read (not a list) should be accepted."""
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: VIN
    data_length: 17
    access: read
    min_session: default
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            cfg = load_testlab_config(p)
        assert cfg.dids[0].access == ["read"]

    def test_empty_dids_list(self):
        with tmp("interface: vcan0\nrx_id: \"0x7DF\"\ntx_id: \"0x7E8\"\ndids: []\n") as p:
            cfg = load_testlab_config(p)
        assert cfg.dids == []


# ---------------------------------------------------------------------------
# load_testlab_config — validation errors
# ---------------------------------------------------------------------------

class TestLoadTestlabConfigValidation:
    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_testlab_config("/nonexistent/path/config.yaml")

    def test_invalid_yaml_raises(self):
        with tmp("{{{invalid yaml") as p:
            with pytest.raises(ConfigError, match="parse"):
                load_testlab_config(p)

    def test_eds_file_raises(self):
        """Passing an EDS file to load_testlab_config raises ConfigError."""
        with tmp(EDS_FULL) as p:
            with pytest.raises(ConfigError, match="metadata"):
                load_testlab_config(p)

    def test_invalid_rx_id_raises(self):
        with tmp("interface: vcan0\nrx_id: NOTVALID\ntx_id: \"0x7E8\"\n") as p:
            with pytest.raises(ConfigError, match="Cannot parse CAN ID"):
                load_testlab_config(p)

    def test_out_of_range_rx_id_raises(self):
        with tmp("interface: vcan0\nrx_id: \"0x800\"\ntx_id: \"0x7E8\"\n") as p:
            with pytest.raises(ConfigError, match="out of range"):
                load_testlab_config(p)

    def test_negative_p2_timeout_raises(self):
        with tmp("interface: vcan0\nrx_id: \"0x7DF\"\ntx_id: \"0x7E8\"\np2_timeout_ms: -1\n") as p:
            with pytest.raises(ConfigError, match="p2_timeout_ms"):
                load_testlab_config(p)

    def test_zero_p2_timeout_raises(self):
        with tmp("interface: vcan0\nrx_id: \"0x7DF\"\ntx_id: \"0x7E8\"\np2_timeout_ms: 0\n") as p:
            with pytest.raises(ConfigError, match="p2_timeout_ms"):
                load_testlab_config(p)

    def test_did_missing_id_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - name: VIN
    data_length: 17
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="missing required field 'id'"):
                load_testlab_config(p)

    def test_did_negative_data_length_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: VIN
    data_length: -1
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="data_length"):
                load_testlab_config(p)

    def test_did_invalid_min_session_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: VIN
    data_length: 17
    access: [read]
    min_session: warp_speed
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="min_session"):
                load_testlab_config(p)

    def test_did_invalid_access_value_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: VIN
    data_length: 17
    access: [execute]
    min_session: default
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="access"):
                load_testlab_config(p)

    def test_dtc_missing_code_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dtcs:
  - description: some fault
    severity: immediate
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="missing required field 'code'"):
                load_testlab_config(p)

    def test_dtc_invalid_severity_raises(self):
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dtcs:
  - code: "0xC00100"
    description: test
    severity: catastrophic
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="severity"):
                load_testlab_config(p)

    def test_error_message_includes_did_index(self):
        """Error message should identify which DID caused the problem."""
        yaml = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
dids:
  - id: "0xF190"
    name: Good
    data_length: 17
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
  - id: "0xF191"
    name: Bad
    data_length: 4
    access: [read]
    min_session: invalid_session
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match=r"dids\[1\]"):
                load_testlab_config(p)


# ---------------------------------------------------------------------------
# load_eds_config — happy path
# ---------------------------------------------------------------------------

class TestLoadEdsConfigHappyPath:
    def test_full_eds_config(self):
        with tmp(EDS_FULL) as p:
            cfg = load_eds_config(p)
        assert cfg.rx_id == 0x7E8  # tester receives on ECU's tx channel
        assert cfg.tx_id == 0x7DF  # tester sends on ECU's rx channel
        assert cfg.ecu_name == "BmsEcu"
        assert cfg.p2_timeout_ms == 25
        assert len(cfg.dids) == 1
        assert cfg.dids[0].id == 0x2001

    def test_eds_no_can_section_uses_defaults(self):
        with tmp(EDS_NO_SECTIONS) as p:
            cfg = load_eds_config(p)
        assert cfg.rx_id == 0x7E8  # tester receives on ECU's tx channel
        assert cfg.tx_id == 0x7DF  # tester sends on ECU's rx channel
        assert cfg.p2_timeout_ms == 150
        assert cfg.dids == []
        assert cfg.dtcs == []

    def test_eds_ecu_name_from_metadata(self):
        with tmp(EDS_NO_SECTIONS) as p:
            cfg = load_eds_config(p)
        assert cfg.ecu_name == "MinimalEcu"


# ---------------------------------------------------------------------------
# load_eds_config — validation errors
# ---------------------------------------------------------------------------

class TestLoadEdsConfigValidation:
    def test_missing_metadata_raises(self):
        with tmp(TESTLAB_MINIMAL) as p:
            with pytest.raises(ConfigError, match="metadata"):
                load_eds_config(p)

    def test_invalid_rx_can_id_raises(self):
        yaml = """
metadata:
  ecu_name: Test
can:
  rx_can_id: GARBAGE
  tx_can_id: "0x7E8"
dids: []
dtcs: []
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="Cannot parse CAN ID"):
                load_eds_config(p)

    def test_out_of_range_tx_can_id_raises(self):
        yaml = """
metadata:
  ecu_name: Test
can:
  rx_can_id: "0x7DF"
  tx_can_id: "0xFFF"
dids: []
dtcs: []
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="out of range"):
                load_eds_config(p)


# ---------------------------------------------------------------------------
# load_config — auto-detection
# ---------------------------------------------------------------------------

class TestLoadConfigAutoDetect:
    def test_detects_eds_by_metadata_key(self):
        with tmp(EDS_FULL) as p:
            cfg = load_config(p)
        assert cfg.ecu_name == "BmsEcu"
        assert cfg.p2_timeout_ms == 25

    def test_detects_testlab_by_absence_of_metadata(self):
        with tmp(TESTLAB_FULL) as p:
            cfg = load_config(p)
        assert cfg.ecu_name == "TestECU"
        assert cfg.p2_timeout_ms == 150

    def test_minimal_testlab_no_metadata(self):
        with tmp(TESTLAB_MINIMAL) as p:
            cfg = load_config(p)
        assert cfg.rx_id == 0x7DF

    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path.yaml")

    def test_validation_errors_propagate(self):
        yaml = """
interface: vcan0
rx_id: "0xFFF"
tx_id: "0x7E8"
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="out of range"):
                load_config(p)

    def test_eds_validation_errors_propagate(self):
        yaml = """
metadata:
  ecu_name: Test
can:
  rx_can_id: "0x7DF"
  tx_can_id: "0x7E8"
dids:
  - id: "0xF190"
    name: VIN
    data_length: -5
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
dtcs: []
"""
        with tmp(yaml) as p:
            with pytest.raises(ConfigError, match="data_length"):
                load_config(p)


# ---------------------------------------------------------------------------
# TesterConfig dataclass
# ---------------------------------------------------------------------------

class TestTesterConfig:
    def test_defaults(self):
        cfg = TesterConfig()
        assert cfg.interface == "vcan0"
        assert cfg.rx_id == 0x7E8  # tester-perspective: receive ECU responses
        assert cfg.tx_id == 0x7DF  # tester-perspective: send requests to ECU
        assert cfg.p2_timeout_ms == 150
        assert cfg.ecu_name == ""
        assert cfg.dids == []
        assert cfg.dtcs == []

    def test_did_def_fields(self):
        d = DidDef(
            id=0xF190, name="VIN", data_length=17,
            access=["read"], min_session="default",
            read_security_level=0, write_security_level=0,
        )
        assert d.id == 0xF190
        assert d.name == "VIN"

    def test_dtc_def_fields(self):
        d = DtcDef(code=0xC00100, description="CAN loss", severity="immediate")
        assert d.code == 0xC00100
        assert d.severity == "immediate"


# ---------------------------------------------------------------------------
# End-to-end: UdsTester.from_config with testlab_config.yaml
# ---------------------------------------------------------------------------

class TestFromConfigEndToEnd:
    """Verify UdsTester.from_config() works with both formats (VirtualBus)."""

    @pytest.mark.asyncio
    async def test_from_testlab_config(self):
        """UdsTester.from_config() loads testlab_config.yaml and opens successfully."""
        import os
        os.environ["XALOQI_LICENSE_SKIP"] = "1"
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        yaml_content = """
interface: vcan0
rx_id: "0x7DF"
tx_id: "0x7E8"
ecu_name: TestECU
dids:
  - id: "0xF190"
    name: VIN
    data_length: 17
    access: [read]
    min_session: default
    read_security_level: 0
    write_security_level: 0
"""
        with tmp(yaml_content) as p:
            tester = UdsTester.from_config(p, interface=VirtualBus.pair("cfg_test")[0])
            # Check config was parsed correctly
            assert tester._rx_id == 0x7DF
            assert tester._tx_id == 0x7E8
            assert 0xF190 in tester._did_defs

    @pytest.mark.asyncio
    async def test_from_eds_config(self):
        """UdsTester.from_config() loads EDS diagnostics_config.yaml."""
        import os
        os.environ["XALOQI_LICENSE_SKIP"] = "1"
        from xaloqi.tester import UdsTester
        from xaloqi.tester.transport.virtual import VirtualBus

        with tmp(EDS_FULL) as p:
            tester = UdsTester.from_config(p, interface=VirtualBus.pair("eds_test")[0])
            assert tester._rx_id == 0x7E8  # tester receives on ECU's tx channel
            assert tester._tx_id == 0x7DF  # tester sends on ECU's rx channel
            assert 0x2001 in tester._did_defs
