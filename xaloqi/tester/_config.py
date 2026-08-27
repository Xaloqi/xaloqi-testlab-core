# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
_config.py — Configuration parsing for xaloqi-tester.

Reads two YAML formats:

  EDS format (diagnostics_config.yaml)
    Has a top-level 'metadata:' key. Used by Xaloqi EDS customers.

  TestLab standalone format (testlab_config.yaml)
    Has a top-level 'interface:' key. For customers without EDS.

load_config() auto-detects the format. Both produce a TesterConfig.

Public functions:
    load_config(path)           Auto-detect and load either format.
    load_eds_config(path)       Load EDS diagnostics_config.yaml explicitly.
    load_testlab_config(path)   Load testlab_config.yaml explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

from .exceptions import ConfigError

_VALID_SESSIONS:   frozenset = frozenset({"default", "extended", "programming"})
_VALID_SEVERITIES: frozenset = frozenset({"immediate", "check_at_next_halt", "information"})
_VALID_ACCESS:     frozenset = frozenset({"read", "write"})


@dataclass
class DidDef:
    """Definition of a single Data Identifier."""
    id:                   int
    name:                 str
    data_length:          int
    access:               List[str]
    min_session:          str
    read_security_level:  int
    write_security_level: int


@dataclass
class DtcDef:
    """Definition of a single Diagnostic Trouble Code."""
    code:        int
    description: str
    severity:    str


@dataclass
class TesterConfig:
    """
    Parsed configuration. Produced by load_config(), load_eds_config(),
    or load_testlab_config(). Format-agnostic — consumers only see this.
    """
    interface:     str          = "vcan0"
    rx_id:         int          = 0x7E8
    tx_id:         int          = 0x7DF
    p2_timeout_ms: int          = 150
    ecu_name:      str          = ""
    dids:          List[DidDef] = field(default_factory=list)
    dtcs:          List[DtcDef] = field(default_factory=list)


def _parse_hex(value, default: int, field_name: str = "") -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip()
    try:
        return int(s, 0)
    except ValueError:
        hint = f" for '{field_name}'" if field_name else ""
        raise ConfigError(
            f"Cannot parse CAN ID{hint}: {s!r}. "
            f"Use hex (\"0x7DF\") or decimal (2015)."
        )


def _parse_did(raw: dict, index: int) -> DidDef:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"dids[{index}] must be a YAML mapping, got {type(raw).__name__}"
        )

    did_id_raw = raw.get("id")
    if did_id_raw is None:
        raise ConfigError(f"dids[{index}]: missing required field 'id'")
    did_id = _parse_hex(did_id_raw, 0, f"dids[{index}].id")

    data_length = raw.get("data_length", 0)
    try:
        data_length = int(data_length)
    except (ValueError, TypeError):
        raise ConfigError(
            f"dids[{index}] (id=0x{did_id:04X}): "
            f"'data_length' must be an integer, got {data_length!r}"
        )
    if data_length < 0:
        raise ConfigError(
            f"dids[{index}] (id=0x{did_id:04X}): "
            f"'data_length' must be >= 0, got {data_length}"
        )

    min_session = str(raw.get("min_session", "default"))
    if min_session not in _VALID_SESSIONS:
        raise ConfigError(
            f"dids[{index}] (id=0x{did_id:04X}): "
            f"'min_session' must be one of {sorted(_VALID_SESSIONS)}, got {min_session!r}"
        )

    access_raw = raw.get("access", ["read"])
    if isinstance(access_raw, str):
        access_raw = [access_raw]
    access = list(access_raw)
    for a in access:
        if a not in _VALID_ACCESS:
            raise ConfigError(
                f"dids[{index}] (id=0x{did_id:04X}): "
                f"'access' entries must be 'read' or 'write', got {a!r}"
            )

    for level_field in ("read_security_level", "write_security_level"):
        val = raw.get(level_field, 0)
        try:
            val_int = int(val)
        except (ValueError, TypeError):
            raise ConfigError(
                f"dids[{index}] (id=0x{did_id:04X}): "
                f"'{level_field}' must be an integer, got {val!r}"
            )
        if val_int < 0:
            raise ConfigError(
                f"dids[{index}] (id=0x{did_id:04X}): "
                f"'{level_field}' must be >= 0, got {val_int}"
            )

    return DidDef(
        id=did_id,
        name=str(raw.get("name", "")),
        data_length=data_length,
        access=access,
        min_session=min_session,
        read_security_level=int(raw.get("read_security_level", 0)),
        write_security_level=int(raw.get("write_security_level", 0)),
    )


def _parse_dtc(raw: dict, index: int) -> DtcDef:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"dtcs[{index}] must be a YAML mapping, got {type(raw).__name__}"
        )

    code_raw = raw.get("code")
    if code_raw is None:
        raise ConfigError(f"dtcs[{index}]: missing required field 'code'")
    code = _parse_hex(code_raw, 0, f"dtcs[{index}].code")

    severity = str(raw.get("severity", "information"))
    if severity not in _VALID_SEVERITIES:
        raise ConfigError(
            f"dtcs[{index}] (code=0x{code:06X}): "
            f"'severity' must be one of {sorted(_VALID_SEVERITIES)}, got {severity!r}"
        )

    return DtcDef(
        code=code,
        description=str(raw.get("description", "")),
        severity=severity,
    )


def _load_yaml(path: Union[str, Path]) -> dict:
    try:
        import yaml
    except ImportError:
        raise ConfigError("pyyaml is required. Install it: pip install pyyaml")

    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"  Copy testlab_config.yaml from the TestLab root and fill in "
            f"your ECU's CAN IDs to get started."
        )
    except Exception as exc:
        raise ConfigError(f"Failed to parse YAML at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config file must be a YAML mapping (key: value pairs) at "
            f"the top level — check {path}"
        )
    return raw


def _validate_can_id(value: int, name: str) -> None:
    if value < 0 or value > 0x7FF:
        raise ConfigError(
            f"CAN ID '{name}' out of range: 0x{value:X}. "
            f"Valid range: 0x000–0x7FF (11-bit standard CAN)."
        )


def load_eds_config(path: Union[str, Path]) -> TesterConfig:
    """
    Load a Xaloqi EDS diagnostics_config.yaml and return a TesterConfig.

    Raises:
        ConfigError: If the file is missing, unparseable, or has invalid fields.
    """
    raw = _load_yaml(path)

    if "metadata" not in raw:
        raise ConfigError(
            f"Not an EDS diagnostics_config.yaml — missing 'metadata:' section in {path}.\n"
            f"  For a standalone config, use load_testlab_config() or load_config()."
        )

    can     = raw.get("can") or {}
    timing  = raw.get("timing") or {}
    meta    = raw.get("metadata") or {}

    # EDS YAML: rx_can_id = ECU's receive channel, tx_can_id = ECU's transmit channel.
    # TesterConfig: rx_id = tester receives (= ECU tx), tx_id = tester sends (= ECU rx).
    ecu_rx_id = _parse_hex(can.get("rx_can_id", "0x7DF"), 0x7DF, "rx_can_id")
    ecu_tx_id = _parse_hex(can.get("tx_can_id", "0x7E8"), 0x7E8, "tx_can_id")
    _validate_can_id(ecu_rx_id, "rx_can_id")
    _validate_can_id(ecu_tx_id, "tx_can_id")

    return TesterConfig(
        interface=str(can.get("interface", "vcan0")),
        rx_id=ecu_tx_id,
        tx_id=ecu_rx_id,
        p2_timeout_ms=int(timing.get("p2_server_max_ms", 150)),
        ecu_name=str(meta.get("ecu_name", "")),
        dids=[_parse_did(d, i) for i, d in enumerate(raw.get("dids") or [])],
        dtcs=[_parse_dtc(d, i) for i, d in enumerate(raw.get("dtcs") or [])],
    )


def load_testlab_config(path: Union[str, Path]) -> TesterConfig:
    """
    Load a standalone testlab_config.yaml and return a TesterConfig.

    The standalone format is flat — all keys sit at the top level.
    Copy testlab_config.yaml from the TestLab root as your starting point.

    Required:
        interface    SocketCAN interface name ("vcan0", "can0", …)
        rx_id        CAN ID the ECU sends responses on
        tx_id        CAN ID the ECU listens on

    Optional (defaults shown):
        ecu_name        ""   Name shown in campaign output and HTML reports
        p2_timeout_ms   150  Response timeout in milliseconds
        dids            []   DID definitions — enables multi-DID reads and reports
        dtcs            []   DTC definitions — adds names to fault records in reports

    Raises:
        ConfigError: If the file is missing, unparseable, or has invalid fields.
    """
    raw = _load_yaml(path)

    if "metadata" in raw:
        raise ConfigError(
            f"This looks like an EDS diagnostics_config.yaml (has 'metadata:' key).\n"
            f"  Use load_eds_config() or load_config() instead."
        )

    rx_id = _parse_hex(raw.get("rx_id", "0x7E8"), 0x7E8, "rx_id")
    tx_id = _parse_hex(raw.get("tx_id", "0x7DF"), 0x7DF, "tx_id")
    _validate_can_id(rx_id, "rx_id")
    _validate_can_id(tx_id, "tx_id")

    p2 = raw.get("p2_timeout_ms", 150)
    try:
        p2 = int(p2)
    except (ValueError, TypeError):
        raise ConfigError(f"'p2_timeout_ms' must be an integer, got {p2!r}")
    if p2 <= 0:
        raise ConfigError(f"'p2_timeout_ms' must be > 0, got {p2}")

    return TesterConfig(
        interface=str(raw.get("interface", "vcan0")),
        rx_id=rx_id,
        tx_id=tx_id,
        p2_timeout_ms=p2,
        ecu_name=str(raw.get("ecu_name", "")),
        dids=[_parse_did(d, i) for i, d in enumerate(raw.get("dids") or [])],
        dtcs=[_parse_dtc(d, i) for i, d in enumerate(raw.get("dtcs") or [])],
    )


def load_config(path: Union[str, Path]) -> TesterConfig:
    """
    Auto-detect config format and load.

    Detection:
        'metadata:' key present  →  EDS diagnostics_config.yaml
        'metadata:' key absent   →  TestLab standalone testlab_config.yaml

    This is the recommended entry point. UdsTester.from_config() and
    runner.py both call this.

    Args:
        path: Path to diagnostics_config.yaml or testlab_config.yaml.

    Returns:
        TesterConfig populated from the file.

    Raises:
        ConfigError: If the file is missing, malformed, or has invalid values.
    """
    raw = _load_yaml(path)
    if "metadata" in raw:
        return load_eds_config(path)
    return load_testlab_config(path)
