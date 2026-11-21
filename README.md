# Realtime Trains for Home Assistant

Track selected, dated train arrivals or departures at your home stations using
the Realtime Trains next-generation API. Designed for installation through HACS.

## Features

- UI setup with your own RTT access or refresh token and a searchable home station.
- Add two or more home stations under one integration and one shared API budget.
- Pick one-off arrivals or departures by date, time, origin and destination.
- Monitor several selected trains independently through their event at home.
- Status, signed delay in minutes, platform, scheduled/expected/actual time and
  last successful update as Home Assistant entities.
- A separate Platform status sensor distinguishes planned from confirmed platforms.
- Five-minute polling outside the final 30 minutes; one-minute polling inside
  the final 30 minutes, including overdue trains. This applies to the selected
  arrival or departure, using the earlier of scheduled and expected times so
  a delayed train remains closely monitored.
- Stop polling when the selected event is actually reported, cancelled or
  diverted. Keep its final result visible until you remove it.
- Selections, final results and request accounting survive restarts.
- Shared hard limits: **10/minute, 100/hour, 1,000/day, 10,000/week**.
  Authentication, setup, selection and manual refresh requests all count.

## Install

Requires Home Assistant **2025.8 or later**, HACS, and a personal token from
[the RTT API portal](https://api-portal.rtt.io/). Deprecated legacy RTT API
credentials are not supported. No token is included with this software.

Add `https://github.com/c-scoot/ha-rtt` in HACS as a **custom repository**, category
**Integration**, then download **Realtime Trains** and restart Home Assistant.
Default-store listing is a separate submission and is not implied.

For a local installation, copy `custom_components/realtime_trains` into your
Home Assistant configuration's `custom_components` directory and restart.

## Configure and select trains

1. Open **Settings → Devices & services → Add integration → Realtime Trains**.
2. Enter your token, then choose your home station.
3. To add another home station, choose **Configure → Manage home stations**
   and select all stations you want. Keep using this single integration.
4. Choose **Configure → Add train**, then a home station, arrivals or departures,
   and a UK local date/time. The picker lists
   advertised passenger calls within the following hour. Change the start
   time to search another hour; the dated service identity is retained even
   when a journey began on the previous day.
5. Choose a service. Repeat to track more trains.

Tracking ends at that train's chosen arrival/departure **at home**, not its
ultimate destination. For a train that visits the same station twice, the
scheduled event time distinguishes the selected call. A separate arrival and
departure for the same service can both be selected.

Use **Manage home stations** to add or remove stations from the train picker.
At least one must remain selected. Removing a station keeps its already
tracked trains and results; use **Remove train** to remove those separately.
Existing single-station installations work immediately after updating, with
their selections, entity IDs and final results preserved. The refresh button
updates active selections and respects the same shared budget.

## Status and freshness

`scheduled` means a timetable exists without a live time. `on_time`, `early`
and `delayed` require a live forecast or actual time. A missing board entry,
service lookup or report never proves arrival, departure or cancellation.
`arrived` and `departed` require an actual report for the selected event.

Missing reports show `no_report`. A track that cannot be confirmed within
24 hours of its scheduled event expires as `unconfirmed`; a later predicted
event extends that deadline so it can be checked. Final results stay visible.
The **Platform** sensor keeps the platform number. **Platform status** shows
**Planned** until RTT supplies an actual platform, then **Confirmed**, even
if the number stays the same. A platform number alone never confirms it.
**Not available** means RTT supplies neither number; **Unconfirmed** means a
retained value has no certainty information. The train picker also labels
platforms as planned or confirmed.

Platform attributes expose `planned_platform`, `confirmed_platform`, and
the original `platform_source: actual` or `planned` for compatibility.
When a train changes from planned platform 5 to confirmed platform 6, both
values remain inspectable. An actual platform means RTT's reported platform;
it is independent of whether the train has arrived or departed.

Retained readings carry `stale`, `last_error` and `next_poll` attributes. During
an outage or budget deferral, check freshness before using a reading in an
automation. The API may omit a platform or forecast entirely. Display quality
depends on RTT's available reports and your token's entitlements.

## Polling and quotas

All calls share one rolling-window request budget for a token. Server-reported
remaining allowances, stricter limits and `Retry-After` can postpone requests.
When a limit is reached, active selections remain saved and resume when quota
allows. Several trains can therefore refresh less often than once a minute.
No board is polled continuously when there are no active trains; station
lists are cached and board searches have a short cache.

Adding home stations does not start additional background requests. Each
station-board search is cached separately. A service response includes all
of that train's calls: when the same train is due for refresh at two stations,
one service request updates both. Different trains still need separate
requests and all share the same allowance. There is no reason to add the
integration twice just to monitor two home stations.

For scale: two independently polled trains at one request/minute need 120
requests/hour, exceeding the 100/hour allowance before setup or search calls.
Choose only trains you need. API use by other applications shares the RTT
allowance; response headers help account for that external consumption.

Tokens are stored in Home Assistant's configuration storage. Protect your HA
configuration and backups. Tokens are sent only to RTT and omitted from
diagnostics. Each household supplies its own token; no public proxy is used.
See [RTT API terms](https://www.realtimetrains.co.uk/legal/api/).

## Development and release

Run `python -m pip install -r requirements-test.txt`, then `python -m pytest`
and `ruff check .`. Runtime dependencies come from Home Assistant.
The pure tests exercise API behaviour, train-call lifecycle and quotas.
On Linux with Python 3.13, install `requirements-ha-test.txt` to run the real
Home Assistant 2025.8 test harness as well. It exercises setup, both arrival
and departure selection, entity state, storage, recovery and tracker removal
using mocked RTT HTTP responses. CI runs that suite. A live HA/HACS
installation and an entitled RTT token are needed for final field validation.

Build the local install archive with `python scripts/package.py`. Rebuild
the original brand icon with `python scripts/build_brand.py` (requires Pillow).

Before public release, push the reviewed source to the configured repository,
run HACS validation and Home Assistant hassfest, and tag a version matching
the manifest. The included validation workflow runs on pushes and pull requests.

MIT licence. API client and models adapted from the MIT-licensed
[ha-realtime-trains](https://github.com/craig-b/ha-realtime-trains); see
[NOTICE.md](NOTICE.md). This independent client is not affiliated with RTT.
