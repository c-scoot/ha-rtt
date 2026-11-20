"""Credential-free support diagnostics."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry):
    runtime = entry.runtime_data
    # Build an allowlist instead of redacting a response that may echo tokens.
    return {
        "station": entry.data.get("station"),
        "tracks": {
            key: call.to_dict() for key, call in runtime.coordinator.tracks.items()
        },
        "snapshots": runtime.coordinator.data,
        "budget": runtime.budget.dump(),
        "is_refresh_token": runtime.api.is_refresh_token,
    }
