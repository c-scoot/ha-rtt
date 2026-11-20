"""Budget-aware manual refresh."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, NAME


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RefreshButton(entry)])


class RefreshButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"

    def __init__(self, entry):
        self._coordinator = entry.runtime_data.coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="Home station",
            entry_type="service",
        )

    async def async_press(self):
        await self._coordinator.async_refresh_tracked()
