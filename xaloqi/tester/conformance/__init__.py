# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
conformance — UDS transport/service conformance vectors and a runner.

Publishes the vector corpus and a judge, not plumbing. Each vector in
corpus/*.json is byte-exact and independently sourced (see each vector's
``upstream`` field), ported from the python-can-isotp/udsoncan/python-
doipclient test suites' own fixtures.

You bring your own transport: implement ``xaloqi.tester.transport.base.CanBus``
(raw CAN frame send/recv) and/or point ``xaloqi.tester.UdsTester`` at it --
both already ship in this package's free, Apache-2.0 core. This module adds
only what was missing: the vectors themselves, a byte-exact judge, and (for
the DoIP suite, which needs raw TCP framing no free transport here exposes)
one minimal probe. See README.md in this package for what each suite
actually asserts and what it refuses to claim.
"""
from .vectors import Suite, Vector, Step, load_suite
from .runner import Report, VectorResult, run_isotp_suite, run_uds_sid_suite, run_doip_suite

__all__ = [
    "Suite",
    "Vector",
    "Step",
    "load_suite",
    "Report",
    "VectorResult",
    "run_isotp_suite",
    "run_uds_sid_suite",
    "run_doip_suite",
]
