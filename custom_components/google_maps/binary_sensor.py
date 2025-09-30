"""Google Maps binary sensor."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, NAME_PREFIX
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor platform."""
    coordinator = entry.runtime_data

    async_add_entities([GoogleMapsBinarySensor(coordinator)])


class GoogleMapsBinarySensor(
    CoordinatorEntity[GMDataUpdateCoordinator], BinarySensorEntity
):
    """Google Maps Binary Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: GMDataUpdateCoordinator) -> None:
        """Initialize Google Maps Binary Sensor."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = f"{NAME_PREFIX} {entry.title} online"
        self._attr_is_on = super().available

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = super().available
        super()._handle_coordinator_update()
