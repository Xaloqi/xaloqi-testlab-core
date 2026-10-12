# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
cli.py -- `python3 -m xaloqi.tester.conformance` entry point.

    python3 -m xaloqi.tester.conformance --suite isotp --interface vcan0 --can-id 0x7E0
    python3 -m xaloqi.tester.conformance --suite uds-sid --interface vcan0 --rx-id 0x7E8 --tx-id 0x7E0
    python3 -m xaloqi.tester.conformance --suite doip --host 192.0.2.10 --port 13400

This is deliberately thin: it wires one of this module's own run_*_suite
functions to a transport you point it at. You bring the transport -- see
core/examples/conformance_bindings/ for a real, runnable CAN binding using
python-can.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .runner import DoipRawProbe, run_doip_suite, run_isotp_suite, run_uds_sid_suite
from .vectors import load_suite


def _print_report(report) -> None:
    for r in report.results:
        if r.skipped:
            status = "SKIP"
        elif r.passed:
            status = "PASS"
        else:
            status = "FAIL"
        print(f"  [{status}] {r.vector_id} ({r.vector_name})")
        if r.upstream_citation:
            print(f"         source: {r.upstream_citation}")
        if status != "PASS":
            print(f"         {r.detail}")
    print(report.summary())


async def _run_isotp(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(args.bindings_dir))
    from can_frame_python_can import PythonCanBus  # type: ignore

    bus = PythonCanBus(channel=args.interface)
    suite = load_suite("isotp")
    report = await run_isotp_suite(
        bus, suite, default_can_id=int(args.can_id, 16),
        include_can_fd=args.include_can_fd,
        include_fixture_dependent=args.include_fixture_dependent,
    )
    await bus.close()
    _print_report(report)
    return 0 if report.all_passed else 1


async def _run_uds_sid(args: argparse.Namespace) -> int:
    from xaloqi.tester import UdsTester

    sys.path.insert(0, str(args.bindings_dir))
    from can_frame_python_can import PythonCanBus  # type: ignore

    # A bare interface STRING here would have UdsTester look up "socketcan"
    # in xaloqi-tester-pro's transport registry -- the exact dependency
    # this corpus must not have (see this package's README). Passing an
    # already-constructed CanBus OBJECT instead takes the one path
    # UdsTester never license-gates: see xaloqi.tester.UdsTester.__aenter__,
    # the `else: self._bus = self._interface` branch.
    bus = PythonCanBus(channel=args.interface)
    suite = load_suite("uds_sid")
    async with UdsTester(
        interface=bus, rx_id=int(args.rx_id, 16), tx_id=int(args.tx_id, 16),
        keepalive=False,
    ) as tester:
        report = await run_uds_sid_suite(
            tester.raw_exchange, suite, include_fixture_dependent=args.include_fixture_dependent,
        )
    _print_report(report)
    return 0 if report.all_passed else 1


async def _run_doip(args: argparse.Namespace) -> int:
    probe = DoipRawProbe(args.host, args.port)
    await probe.open()
    try:
        suite = load_suite("doip")
        report = await run_doip_suite(probe, suite)
    finally:
        await probe.close()
    _print_report(report)
    return 0 if report.all_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m xaloqi.tester.conformance",
        description="UDS transport/service conformance vectors -- byte-exact, independently sourced.",
    )
    sub = parser.add_subparsers(dest="suite", required=True)

    p_isotp = sub.add_parser("isotp", help="ISO 15765-2 raw CAN frame vectors")
    p_isotp.add_argument("--interface", required=True, help="SocketCAN interface, e.g. vcan0")
    p_isotp.add_argument("--can-id", default="0x7E0", help="arbitration ID to send requests on")
    p_isotp.add_argument("--include-can-fd", action="store_true")
    p_isotp.add_argument("--include-fixture-dependent", action="store_true",
                          help="also run vectors that need a non-default block_size/stmin "
                               "the target almost certainly isn't configured with")
    p_isotp.add_argument("--bindings-dir", default="core/examples/conformance_bindings")

    p_uds = sub.add_parser("uds-sid", help="ISO 14229-1 complete-PDU vectors")
    p_uds.add_argument("--interface", required=True, help="SocketCAN interface, e.g. vcan0")
    p_uds.add_argument("--rx-id", default="0x7E8", help="ECU's response CAN ID")
    p_uds.add_argument("--tx-id", default="0x7E0", help="ECU's request CAN ID")
    p_uds.add_argument("--include-fixture-dependent", action="store_true",
                        help="also run vectors needing DID 0x0001 / RID 0x0012 / the XOR "
                             "security stub -- see corpus/uds_sid.json's fixture_note per vector")
    p_uds.add_argument("--bindings-dir", default="core/examples/conformance_bindings")

    p_doip = sub.add_parser("doip", help="ISO 13400-2 raw TCP byte vectors")
    p_doip.add_argument("--host", required=True)
    p_doip.add_argument("--port", type=int, default=13400)

    args = parser.parse_args()

    if args.suite == "isotp":
        return asyncio.run(_run_isotp(args))
    if args.suite == "uds-sid":
        return asyncio.run(_run_uds_sid(args))
    if args.suite == "doip":
        return asyncio.run(_run_doip(args))
    parser.error(f"unknown suite {args.suite!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
