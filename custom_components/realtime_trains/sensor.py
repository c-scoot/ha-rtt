"""Automation-friendly sensors for individual dated station calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .tracking import platform_status

STATUSES = [
    "scheduled",
    "on_time",
    "delayed",
    "early",
    "arrived",
    "departed",
    "cancelled",
    "diverted",
    "unconfirmed",
    "no_report",
]
DESCRIPTIONS = (
    SensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
    ),
    SensorEntityDescription(
        key="delay_minutes",
        translation_key="delay_minutes",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:clock-alert-outline",
    ),
    SensorEntityDescription(
        key="platform", translation_key="platform", icon="mdi:train"
    ),
    SensorEntityDescription(
        key="platform_status",
        translation_key="platform_status",
        device_class=SensorDeviceClass.ENUM,
        options=["planned", "confirmed", "unconfirmed", "not_available"],
        icon="mdi:check-decagram-outline",
    ),
    *(
        SensorEntityDescription(
            key=key,
            translation_key=key,
            device_class=SensorDeviceClass.TIMESTAMP,
        )
        for key in ("scheduled", "expected", "actual")
    ),
    SensorEntityDescription(
        key="last_success",
        translation_key="last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        TrainSensor(coordinator, entry, track_id, description)
        for track_id in coordinator.tracks
        for description in DESCRIPTIONS
    )


class TrainSensor(CoordinatorEntity, SensorEntity):
    """One stable property of a selected station call."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, track_id: str, description):
        super().__init__(coordinator)
        self.entity_description = description
        self._track_id = track_id
        self._attr_unique_id = f"{entry.entry_id}_{track_id}_{description.key}"
        call = coordinator.tracks[track_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{track_id}")},
            name=call.label,
            manufacturer=NAME,
            model=f"{call.direction.title()} tracker",
            entry_type="service",
        )

    @property
    def _snapshot(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._track_id, {})

    @property
    def available(self) -> bool:
        # Keep the final result readable after polling stops; retained live
        # data carries explicit freshness attributes during a temporary outage.
        return bool(self._snapshot)

    @property
    def native_value(self):
        if self.entity_description.key == "platform_status":
            return platform_status(self._snapshot)
        value = self._snapshot.get(self.entity_description.key)
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return datetime.fromisoformat(value) if value else None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._snapshot
        call = self.coordinator.tracks[self._track_id]
        return {
            "service_identity": call.unique_identity,
            "station": call.station,
            "direction": call.direction,
            "terminal": data.get("terminal", False),
            "stale": data.get("stale", True),
            "next_poll": data.get("next_poll"),
            "platform_source": data.get("platform_source"),
            "platform_status": platform_status(data),
            "planned_platform": data.get("planned_platform")
            or (
                data.get("platform")
                if data.get("platform_source") == "planned"
                else None
            ),
            "confirmed_platform": data.get("confirmed_platform")
            or (
                data.get("platform")
                if data.get("platform_source") == "actual"
                else None
            ),
            "reason": data.get("reason"),
            "last_error": data.get("last_error"),
        }
