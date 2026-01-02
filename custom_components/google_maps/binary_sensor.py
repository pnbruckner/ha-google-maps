"""Google Maps binary sensor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator
from .helpers import UniqueID, dev_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor platform."""
    async_add_entities([GoogleMapsBinarySensor(entry.runtime_data.coordinator)])


class GoogleMapsBinarySensor(
    CoordinatorEntity[GMDataUpdateCoordinator], BinarySensorEntity
):
    """Google Maps Binary Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: GMDataUpdateCoordinator) -> None:
        """Initialize Google Maps Binary Sensor."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = uid = UniqueID(entry.entry_id)
        self._attr_translation_placeholders = {"title": entry.title}
        self._attr_device_info = dr.DeviceInfo(
            entry_type=dr.DeviceEntryType.SERVICE,
            identifiers=dev_ids(uid),
            name=entry.title,
        )
        self._attr_is_on = super().available

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    def _friendly_name_internal(self) -> str | None:
        """Return the friendly name.

        It does not make sense to use device name in front of entity name since device
        name is effectively part of entity name since they both come from config entry
        title.
        """
        name = self.name
        if name is UNDEFINED:
            return None
        return name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = super().available
        super()._handle_coordinator_update()
