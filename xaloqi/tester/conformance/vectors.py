# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
vectors.py — the vector corpus data model and JSON loader.

Each corpus/*.json file is a ``Suite``: a list of ``Vector``s, each an
ordered list of ``Step``s. A Step is either something sent or something
expected back, expressed as raw bytes (hex string, space-separated) --
never as a semantic action. That is the whole point: a vector is a byte
fact, independently sourced, not an opinion about what the bytes mean.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_CORPUS_DIR = Path(__file__).parent / "corpus"


def _hex_to_bytes(s: str) -> bytes:
    """'05 11 22' -> b'\\x05\\x11\\x22'. Empty string -> b''."""
    s = s.strip()
    if not s:
        return b""
    return bytes(int(tok, 16) for tok in s.split())


def _bytes_to_hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


@dataclass(frozen=True)
class Step:
    """
    One step of a vector's exchange.

    op:
      "send"          -- send `data` (interpretation is suite-specific:
                          a raw CAN frame payload for isotp, a complete UDS
                          PDU for uds_sid, raw TCP bytes for doip).
      "expect"        -- the next thing received must equal `data` exactly
                          (or, for isotp, match `data` as a byte-exact
                          *prefix* of the received frame -- padding past
                          the asserted length is a separate EDS feature
                          this suite does not care about, matching the
                          original C vectors' own `assert_frame_prefix`).
      "expect_none"   -- nothing must arrive within `timeout_ms` (a
                          negative assertion: VEC-RX-MF-003's "no further
                          FC may be sent for a bad SN").
      "can_id"        -- (isotp only) the arbitration ID to use for the
                          *next* send/expect step, overriding the suite's
                          default tx_can_id/rx_can_id. Present because a
                          vector sends on one ID (the ECU's RX) and expects
                          on another (the ECU's TX).

    data: hex-byte string, e.g. "05 11 22 33 44 55 00 00". Empty for
    expect_none.
    """

    op: str
    data: str = ""
    can_id: Optional[int] = None
    timeout_ms: int = 200
    note: str = ""
    # uds_sid and doip "expect" steps default to exact, full-length
    # equality -- the whole point of a byte-exact vector. A handful of
    # vectors are deliberately shape-only (e.g. VEC-SID-10-001 asserts
    # SID+session echo but not the target's own P2/P2* timing bytes) and
    # set exact: false to compare only `data`'s length as a prefix. The
    # isotp suite always prefix-matches regardless of this flag, matching
    # the original C vectors' own assert_frame_prefix (CAN frames are
    # padded to 8 bytes; the padding is an EDS feature orthogonal to the
    # vector, not something to assert on).
    exact: bool = True
    # uds_sid suite only -- a small, closed set of ops for vectors that need
    # a value computed from the target's own (unpredictable) response, e.g.
    # SecurityAccess's seed. Not a general scripting facility: each op/match/
    # algorithm name is implemented explicitly in runner.py, nothing is
    # eval()'d, and the set exists to serve exactly VEC-SID-27-001 and
    # VEC-SID-2E-001 -- extend deliberately, not by convention.
    match: str = ""
    sid: str = ""
    subfn: str = ""
    algorithm: str = ""
    from_var: str = ""
    store_as: str = ""
    prefix: str = ""
    value_var: str = ""

    @property
    def data_bytes(self) -> bytes:
        return _hex_to_bytes(self.data)


@dataclass(frozen=True)
class Upstream:
    """Provenance of a vector's bytes -- who it was ported from, and under
    what licence. Carried into every report so a failure or a pass cites
    the independent source, not this package's own say-so."""

    project: str
    file: str
    license: str
    copyright: str = ""


@dataclass(frozen=True)
class Vector:
    id: str
    name: str
    description: str
    upstream: Optional[Upstream]
    iso_clause: str = ""
    requires_fixture: bool = False
    fixture_note: str = ""
    requires_can_fd: bool = False
    session: str = "default"
    steps: List[Step] = field(default_factory=list)


@dataclass(frozen=True)
class Suite:
    name: str
    iso_reference: str
    scope_note: str
    not_ported: List[Dict[str, str]]
    vectors: List[Vector]

    def vector(self, vector_id: str) -> Vector:
        for v in self.vectors:
            if v.id == vector_id:
                return v
        raise KeyError(f"no vector {vector_id!r} in suite {self.name!r}")


def _step_from_dict(d: Dict[str, Any]) -> Step:
    return Step(
        op=d["op"],
        data=d.get("data", ""),
        can_id=(int(d["can_id"], 16) if "can_id" in d else None),
        timeout_ms=d.get("timeout_ms", 200),
        note=d.get("note", ""),
        exact=d.get("exact", True),
        match=d.get("match", ""),
        sid=d.get("sid", ""),
        subfn=d.get("subfn", ""),
        algorithm=d.get("algorithm", ""),
        from_var=d.get("from", ""),
        store_as=d.get("store_as", ""),
        prefix=d.get("prefix", ""),
        value_var=d.get("value", ""),
    )


def _vector_from_dict(d: Dict[str, Any]) -> Vector:
    upstream_d = d.get("upstream")
    upstream = Upstream(**upstream_d) if upstream_d else None
    return Vector(
        id=d["id"],
        name=d["name"],
        description=d.get("description", ""),
        upstream=upstream,
        iso_clause=d.get("iso_clause", ""),
        requires_fixture=d.get("requires_fixture", False),
        fixture_note=d.get("fixture_note", ""),
        requires_can_fd=d.get("requires_can_fd", False),
        session=d.get("session", "default"),
        steps=[_step_from_dict(s) for s in d.get("steps", [])],
    )


def load_suite(suite_name: str) -> Suite:
    """Load a suite by name ('isotp', 'uds_sid', or 'doip') from this
    package's corpus/ directory, or raise FileNotFoundError."""
    path = _CORPUS_DIR / f"{suite_name}.json"
    return load_suite_file(path)


def load_suite_file(path: Path) -> Suite:
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    return Suite(
        name=d["name"],
        iso_reference=d.get("iso_reference", ""),
        scope_note=d.get("scope_note", ""),
        not_ported=d.get("not_ported", []),
        vectors=[_vector_from_dict(v) for v in d["vectors"]],
    )
