# Realtime Trains

HACS Home Assistant custom integration for the RTT next-generation API.

Keep the API client, request budget and train-call state machine independent of Home Assistant. Use config flows, config-entry runtime_data, coordinator entities and the HA Store API. Each tracked item is a dated station arrival or departure, not an entire journey. Never infer arrival/departure or cancellation from a missing report or a clock alone.

Every physical HTTP request, including authentication and manual requests, must pass the shared persisted budget: 10/minute, 100/hour, 1000/day, 10000/week. Respect stricter response headers and Retry-After. Poll 300 seconds before the final 30 minutes and 60 seconds within that window, subject to budget. Completed tracks stop polling.

Run pytest and ruff before delivery. Do not alter a live Home Assistant instance or publish a repository without user instruction. Keep tokens out of logs, fixtures, diagnostics and Git. Preserve upstream MIT notices for reused api.py and models.py.
