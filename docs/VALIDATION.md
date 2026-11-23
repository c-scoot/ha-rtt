# Validation — 23 November 2026 (0.3.0)

- 58 tests passed against the Home Assistant 2025.8.0 fixture on Python 3.13.
- Ruff checks and Python compilation passed.
- The install ZIP was built from an explicit file allowlist and its CRCs checked.
- No live RTT requests, live Home Assistant changes or HACS install were made.

Coverage includes config and options flows, both station-event directions,
terminal retention and reload, stale recovery, registry cleanup, midnight/DST,
the 30-minute polling boundary, shared rolling quotas and persisted budgets,
authentication requests, aiohttp retries, redirects and malformed responses.
The 0.2.0 regressions also verify planned-to-confirmed platform transitions
without a number change, platform withdrawal, multi-station options and cache
isolation, sharing one response across staggered calls of the same train, and
preservation of existing entity IDs and terminal results after station changes.
The 0.3.0 regressions verify weekly rollover across clock changes, matching
actual dated timetable identities, refusing ambiguous or incomplete routes,
skipping missed weeks, cancellation recovery, quota deferral across restarts,
stable entity IDs with cleared old platform/time data, disabling recurrence
without reverting the current dated trip, legacy route verification and
duplicate prevention after rollover.

## Reproduce on Linux

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-ha-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

## Local Windows accommodation

The Home Assistant test harness imports the POSIX `resource` module. Local
verification injected an empty module for that unused runner import. The
harness's blanket socket-disable hook was bypassed to permit Windows asyncio
loopback initialization; its loopback host allowlist remained active, and all
RTT HTTP calls were mocked. Neither shim is part of the integration or CI.

Test-only native wheel substitutions were ciso8601 2.4.1 and lru-dict 1.4.1;
production Home Assistant requirements are unchanged. pycares is constrained
below 5 in the HA test requirements for compatibility with baseline aiodns.
Linux CI uses the baseline's native dependency pins without these substitutions.

## Before distribution

Metadata points to the public repository `https://github.com/c-scoot/ha-rtt`
with `@c-scoot` as maintainer. GitHub Actions runs the HACS validator,
hassfest and tests on pushes and pull requests. Inspect the checks for the
current commit before installing or tagging a release.
Use an actual RTT token and a disposable HA installation to verify field
availability and the HACS install/update path. No live token is in this project.
