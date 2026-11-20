"""Home Assistant integration for Realtime Trains."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import RealtimeTrainsCoordinator


class RealtimeTrainsRuntime:
    """Runtime objects shared by the integration platforms."""

    def __init__(self, coordinator: RealtimeTrainsCoordinator) -> None:
        self.coordinator = coordinator
        self.api = coordinator.api
        self.budget = coordinator.budget


_PLATFORMS = ("sensor", "button")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Restore trackers and set up the entity platforms."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from .const import DOMAIN
    from .coordinator import BudgetLedger, RealtimeTrainsCoordinator, entry_store

    store = entry_store(hass, entry)
    coordinator = RealtimeTrainsCoordinator(
        hass,
        entry,
        store,
        await store.async_load() or {},
        await BudgetLedger.async_load(hass),
    )
    entry.runtime_data = RealtimeTrainsRuntime(coordinator)
    await coordinator.async_save()

    # Each tracker has its own managed device and sensors. Keep the station
    # device, while removing trackers deleted through the options flow.
    prefix = f"{entry.entry_id}_"
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith(prefix):
                track_id = identifier.removeprefix(prefix)
                if track_id not in coordinator.tracks:
                    devices.async_update_device(
                        device.id, remove_config_entry_id=entry.entry_id
                    )
                break
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        track_id, _, _ = entity.unique_id.removeprefix(prefix).partition("_")
        if (
            entity.platform == DOMAIN
            and len(track_id) == 64
            and track_id not in coordinator.tracks
        ):
            entities.async_remove(entity.entity_id)

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the entity platforms and shut down their coordinator."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove saved snapshots for a deleted entry."""
    from .coordinator import entry_store

    await entry_store(hass, entry).async_remove()
