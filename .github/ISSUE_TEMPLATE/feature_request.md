---
name: Feature request
about: Suggest an addition to xaloqi-tester (the free core)
title: ""
labels: enhancement
---

**What are you trying to do?**

Describe the goal, not just the mechanism — it helps to know the real use
case even if the eventual design looks different.

**What's missing today?**

**Where would this belong?**

- [ ] The UDS client (`xaloqi.tester.UdsTester`)
- [ ] The campaign runner / a new campaign action
- [ ] The simulator (`xaloqi.sim`)
- [ ] The plugin seam (a new entry-point group, or a change to an existing one)
- [ ] Something else

**Note on scope**

This repository is the free, Apache-2.0 core. Real transports (hardware
CAN, DoIP, SOME/IP), SOVD, multi-ECU workspaces, reports/dashboard, and AI
analysis are part of the commercial Xaloqi TestLab Pro and out of scope
here — but if what you want is a new *transport* or *campaign action*, the
plugin seam (`xaloqi_tester.transports` / `.runner_actions` entry-point
groups) may let you add it as your own package without needing it merged
here at all.
