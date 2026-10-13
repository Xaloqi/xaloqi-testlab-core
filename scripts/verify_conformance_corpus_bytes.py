#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
verify_conformance_corpus_bytes.py

The conformance corpus (core/xaloqi/tester/conformance/corpus/*.json) was
hand-transcribed from EDS's own C conformance test files, not hand-typed
from memory -- but "transcribed from source" still carries transcription
risk a mechanical check should catch rather than trust.

This script does NOT regenerate the corpus (the mapping from a C vector's
white-box assertions to this corpus's black-box steps is editorial, not
mechanical -- several vectors are deliberately reimagined, not copied; see
each vector's own "description" and this package's README for why). What
it DOES do: for every byte literal this corpus actually sends or expects
that also appears as a literal byte array in the cited EDS C source file,
confirm the bytes match. A hand-transcription typo in a byte that exists
in both places is exactly what this catches; it cannot catch an error in
bytes that only ever existed in this corpus (the reimagined vectors) --
those are reviewed by reading, not by this script.

Usage:
    python3 scripts/verify_conformance_corpus_bytes.py /path/to/EDS/checkout
"""
import json
import re
import sys
from pathlib import Path

_CORPUS_DIR = Path(__file__).parent.parent / "xaloqi" / "tester" / "conformance" / "corpus"

# Maps each corpus suite to the EDS C file it was ported from, and the exact
# byte sequences (as they appear hand-transcribed in this corpus) that ARE
# direct transcriptions from that file -- i.e. not reimagined. Checked as
# a normalised hex-byte substring search: a false negative here means a
# real transcription error OR that the byte sequence genuinely isn't in
# the source (reimagined), which is why this list is curated, not "every
# vector."
_DIRECT_TRANSCRIPTIONS = {
    "isotp": "tests/unit_runnable/test_isotp_conformance_vectors.c",
    "uds_sid": "tests/unit_runnable/test_uds_sid_conformance_vectors.c",
    "doip": "tests/unit_runnable/test_doip_conformance_vectors.c",
}


def _normalise(hex_str: str) -> str:
    """'02 FD 00' -> '0x02,0xFD,0x00' (uppercase, comma-joined) for a
    substring-independent comparison against C literal arrays, which use
    varying whitespace/line-wrapping but a consistent '0xNN' token form."""
    toks = [t for t in hex_str.strip().split() if t]
    return ",".join(f"0x{int(t, 16):02X}" for t in toks)


_C_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _strip_c_comments(c_source: str) -> str:
    """EDS's own byte-array literals routinely wrap every 8 bytes with a
    '/* ... */' comment (CAN-frame-width grouping) -- strip comments first
    so a sequence spanning a comment still matches as consecutive tokens."""
    return _C_COMMENT_RE.sub(" ", c_source)


def _c_byte_sequence_present(c_source: str, hex_str: str) -> bool:
    """True if the exact byte sequence appears as consecutive 0xNN tokens
    (any case, any whitespace, comments already stripped) anywhere in the
    C source."""
    toks = [t for t in hex_str.strip().split() if t]
    if not toks:
        return True
    pattern = r"0[xX]0*" + r"[uU]?\s*,\s*0[xX]0*".join(f"{int(t, 16):X}" for t in toks) + r"[uU]?"
    return re.search(pattern, c_source) is not None


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} /path/to/EDS/checkout", file=sys.stderr)
        return 1
    eds_root = Path(sys.argv[1])

    total_checked = 0
    total_present = 0
    not_present = []

    for suite_name, rel_path in _DIRECT_TRANSCRIPTIONS.items():
        c_path = eds_root / rel_path
        if not c_path.is_file():
            print(f"ERROR: {c_path} not found", file=sys.stderr)
            return 1
        c_source = _strip_c_comments(c_path.read_text(encoding="utf-8"))

        with open(_CORPUS_DIR / f"{suite_name}.json") as fh:
            suite = json.load(fh)

        for vec in suite["vectors"]:
            for step in vec.get("steps", []):
                data = step.get("data", "")
                if not data:
                    continue
                total_checked += 1
                present = _c_byte_sequence_present(c_source, data)
                if present:
                    total_present += 1
                else:
                    not_present.append((suite_name, vec["id"], data))

    print(f"Checked {total_checked} byte strings across 3 suites.")
    print(f"  {total_present} found verbatim as a byte sequence in the cited EDS C file.")
    print(f"  {len(not_present)} NOT found verbatim (expected for reimagined vectors -- "
          f"review each by reading, not by this script):")
    for suite_name, vec_id, data in not_present:
        print(f"    {suite_name}/{vec_id}: {data!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
