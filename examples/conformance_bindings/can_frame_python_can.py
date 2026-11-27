#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
can_frame_python_can.py -- the module name --bindings-dir looks for.

The conformance CLI's CAN suites use the python-can binding shipped in the
package (xaloqi/tester/conformance/python_can_bus.py) unless you pass
--bindings-dir. To run them over your own transport, write a module with
this name in a directory of your own, defining a PythonCanBus class that
implements xaloqi.tester.transport.base.CanBus (send() and recv(), plus
close()) and takes channel= in its constructor, then run:

    python3 -m xaloqi.tester.conformance uds-sid --interface vcan0 \\
        --rx-id 0x7E8 --tx-id 0x7E0 --bindings-dir path/to/that/directory

This file only re-exports the packaged binding, so it doubles as a working
--bindings-dir for a check. Its source is the reference to copy.
"""
from xaloqi.tester.conformance.python_can_bus import PythonCanBus  # noqa: F401
