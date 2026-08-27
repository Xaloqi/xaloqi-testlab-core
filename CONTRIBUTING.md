# Contributing to xaloqi-tester

Thanks for considering a contribution. This is the free, Apache-2.0 core of
Xaloqi TestLab — the UDS client, the in-process simulator, and the campaign
runner. (Real transports, SOVD, workspaces, and reporting live in the
commercial Xaloqi TestLab Pro, developed in a private repository, and are
out of scope for pull requests here.)

## Before you start

- **Bug fixes and small improvements**: open a PR directly.
- **New features or anything that changes public behaviour** (a new
  campaign action, a change to the JSON output schema, a new transport
  interface): please open an issue first to discuss the approach. This
  project shares its JSON schema and campaign YAML vocabulary with the
  commercial product and with Xaloqi EDS, so changes here have a wider
  blast radius than they might look.
- **Extending the plugin seam** (`xaloqi_tester.transports`,
  `.runner_actions`, `.runner_hooks`, `.cli_commands` entry-point groups) is
  encouraged — that's the intended way for third parties to add transports
  or campaign actions without needing this repo to know about them.

## Developer certificate of origin

By submitting a contribution, you certify that you wrote it (or have the
right to submit it) and that you're licensing it under this project's
Apache-2.0 license. Please sign off your commits (`git commit -s`) to
record that certification — this project follows the same
[Developer Certificate of Origin](https://developercertificate.org/)
convention used by the Linux kernel and many other open source projects.

## Development setup

```bash
git clone https://github.com/Xaloqi/xaloqi-testlab-core
cd xaloqi-testlab-core
pip install -e ".[dev]"
XALOQI_LICENSE_SKIP=1 pytest tests/ -v
```

No license key, no hardware, and no network access are needed to build or
test this repository — everything here runs against the in-process
simulator.

## Style

- Match the surrounding code. Type hints and docstrings are used
  throughout; keep using them.
- Every behaviour change — including output-format or JSON-field changes —
  needs a `CHANGELOG.md` entry (this project follows
  [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)).
- Tests are required for new behaviour. `pytest tests/ -v` must pass clean.

## Reporting a security issue

Please see [SECURITY.md](SECURITY.md) rather than opening a public issue.
