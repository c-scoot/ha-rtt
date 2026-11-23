# Changelog

## 0.3.0

- Added optional weekly repeats for new or existing tracked trains.
- Kept stable sensor/device IDs while resolving a genuine dated timetable
  service each week using UK local time, route and operator.
- Added Weekly repeat and Next weekly train sensors and controls for stopping
  repeats while retaining the current dated trip.
- Preserved completed results while waiting, with shared quota handling,
  restart recovery and conservative handling of missing or ambiguous services.
- Weekly rules continue after cancellations and skip missed weeks after outages.

## 0.2.0

- Added a Platform status sensor: Planned, Confirmed, Unconfirmed or Not available.
  Confirmation requires RTT's actual platform, even when it matches the plan.
- Exposed planned and confirmed platform numbers separately, retaining the
  existing platform number and source attributes for automation compatibility.
- Added Configure → Manage home stations for two or more stations under one
  token, API client and persisted request budget. Choose a station per train.
- Kept existing single-station configuration, dated selections, entity IDs
  and final results. Removing a saved station leaves its tracked calls intact.
- Shared a service response across the tracked station calls of the same train.
  Adding a station does not start station-board polling.

Update through HACS and restart Home Assistant. Open the existing integration's
Configure menu to add another station; do not remove or duplicate the integration.

## 0.1.0

Initial dated station-call tracking, configurable home station, live status,
platform, time and delay sensors, adaptive polling and persisted API quotas.
