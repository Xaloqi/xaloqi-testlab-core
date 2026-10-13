#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
verify_upstream_attribution.py

Every vector in the conformance corpus that was ported from a third-party
project carries an `upstream` block naming that project, its licence, and
its copyright notice. Those notices are the reason the corpus can claim its
provenance is checkable: the field exists so a third party can go and
verify a vector against its actual source.

A notice that is *wrong* is therefore worse than no notice. It is also the
defect class this package can least afford, since the whole argument for
the corpus is that we only state what we have checked. MIT additionally
requires the copyright notice be reproduced accurately in redistributions.

This caught a real one: every `uds_sid` vector cited udsoncan as
"Copyright (c) 2019 Pier-Yves Lessard". The project's LICENSE.txt says
2017 -- the same year as python-can-isotp, which the `isotp` suite cited
correctly. Published in v1.5.7 before anyone noticed (TestLab#84).

TWO MODES, and the split is deliberate:

  default (offline, hermetic)
      Every `upstream` block in the corpus must match _EXPECTED below
      exactly, and every vector citing a given project must agree with
      every other vector citing it. Safe in a release gate: no network,
      no rate limit, no third-party availability in the release path.

  --online
      Re-verifies _EXPECTED itself against each upstream repository's
      actual licence file. This is what stops the pinned table from
      silently rotting -- an offline check against a stale pin only
      proves the corpus agrees with a guess. Opt-in, and NOT part of the
      release gate, precisely so a GitHub outage can never block a
      release. Run it when adding a project, or periodically.

Usage:
    python3 core/scripts/verify_upstream_attribution.py
    python3 core/scripts/verify_upstream_attribution.py --online

Exit 0 = every attribution matches. Non-zero = do not ship.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_CORPUS_DIR = Path(__file__).parent.parent / "xaloqi" / "tester" / "conformance" / "corpus"

# The pinned truth. Each entry was read from the upstream repository's own
# licence file, not from its README, its PyPI metadata (which carries only
# the SPDX id, no year), or from memory.
#
# Verified 2026-10-13 against each repo's licence file via the GitHub API.
# `--online` re-checks this table; if you add a project, run it.
_EXPECTED = {
    "python-can-isotp": {
        "repo": "pylessard/python-can-isotp",
        "licence_path": "LICENSE.txt",
        "license": "MIT",
        "copyright": "Copyright (c) 2017 Pier-Yves Lessard",
    },
    "udsoncan": {
        "repo": "pylessard/python-udsoncan",
        "licence_path": "LICENSE.txt",
        "license": "MIT",
        # Same year as python-can-isotp. The two differing was the bug.
        "copyright": "Copyright (c) 2017 Pier-Yves Lessard",
    },
    "python-doipclient": {
        "repo": "jacobschaer/python-doipclient",
        "licence_path": "LICENSE",
        "license": "MIT",
        "copyright": "Copyright (c) 2020 Jacob Schaer",
    },
}


def _iter_upstreams():
    """Yield (suite, vector_id, upstream_dict) for every cited vector."""
    for path in sorted(_CORPUS_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for vec in doc.get("vectors", []):
            up = vec.get("upstream")
            if up:
                yield path.stem, vec.get("id", "<no id>"), up


_COPYRIGHT_RE = re.compile(r"Copyright \(c\) \d{4}[^\"\n]*?(?=[.,;)]|\s-->|$)", re.I)


def _walk_strings(node, path="$"):
    """Every string anywhere in the document, with its JSON path."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def check_whole_document():
    """Catch a notice anywhere in the corpus, not only in vectors[].upstream.

    This exists because the structured check below did NOT catch everything.
    The udsoncan year was wrong in 14 vectors *and* in the suite-level
    `scope_note`, which has no `upstream` block and which a field-level
    check walks straight past -- the lessons/run-040 shape, where the check
    tests the property you thought of rather than the one that matters.
    Any future field carrying a notice is covered by this without anyone
    remembering to extend the list.
    """
    known = {e["copyright"] for e in _EXPECTED.values()}
    failures = []
    for path in sorted(_CORPUS_DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for jpath, text in _walk_strings(doc, f"{path.name}"):
            for notice in _COPYRIGHT_RE.findall(text):
                notice = notice.strip()
                if notice not in known:
                    failures.append(
                        f"{jpath}: copyright notice not in the pinned table\n"
                        f"      found:  {notice!r}\n"
                        f"      pinned: {sorted(known)}"
                    )
    return failures


def check_corpus():
    failures = []
    seen = 0
    for suite, vid, up in _iter_upstreams():
        seen += 1
        project = up.get("project")
        exp = _EXPECTED.get(project)
        if exp is None:
            failures.append(
                f"{suite}/{vid}: cites unknown project {project!r}. "
                f"Add it to _EXPECTED (and run --online to verify it) "
                f"rather than widening this check."
            )
            continue
        for field in ("license", "copyright"):
            got = up.get(field)
            if got != exp[field]:
                failures.append(
                    f"{suite}/{vid}: {project} {field}\n"
                    f"      corpus:   {got!r}\n"
                    f"      upstream: {exp[field]!r}"
                )
    if seen == 0:
        failures.append(
            "no upstream citations found at all -- the corpus moved, or this "
            "script is looking in the wrong place. Refusing to pass vacuously."
        )
    return seen, failures


def _fetch_licence(repo, path):
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "xaloqi-attribution-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def check_online():
    failures = []
    for project, exp in sorted(_EXPECTED.items()):
        try:
            text = _fetch_licence(exp["repo"], exp["licence_path"])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            failures.append(
                f"{project}: could not fetch {exp['repo']}/{exp['licence_path']} ({exc}). "
                f"This mode is opt-in for exactly this reason -- it is not a release gate."
            )
            continue
        if exp["copyright"] not in text:
            first = next(
                (ln.strip() for ln in text.splitlines() if "Copyright" in ln), "<none found>"
            )
            failures.append(
                f"{project}: pinned notice is not in the upstream licence\n"
                f"      pinned:   {exp['copyright']!r}\n"
                f"      upstream: {first!r}\n"
                f"      source:   {exp['repo']}/{exp['licence_path']}"
            )
        else:
            print(f"  ok  {project:<20} {exp['copyright']}  ({exp['repo']}/{exp['licence_path']})")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument(
        "--online",
        action="store_true",
        help="also re-verify the pinned table against each upstream repo's licence file",
    )
    args = ap.parse_args()

    print("Checking corpus attributions against the pinned table...", flush=True)
    seen, failures = check_corpus()
    if not failures:
        print(f"  ok  {seen} cited vectors, all attributions match")

    print("\nScanning every string in the corpus for stray notices...", flush=True)
    doc_failures = check_whole_document()
    failures += doc_failures
    if not doc_failures:
        print("  ok  no copyright notice anywhere outside the pinned set")

    if args.online:
        print("\nRe-verifying the pinned table against upstream licence files...", flush=True)
        failures += check_online()

    if failures:
        print("\nFAIL -- attribution mismatch:\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nAn inaccurate copyright notice in a redistributed MIT vector is a "
            "licensing defect, not a typo. Fix the corpus, or the pin if the "
            "upstream genuinely changed.",
            file=sys.stderr,
        )
        return 1
    print("\nPASS -- every upstream attribution matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
