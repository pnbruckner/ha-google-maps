"""Google Maps binary sensor."""
from __future__ import annotations

from typing import cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN, NAME_PREFIX
from .coordinator import GMDataUpdateCoordinator, GMIntegData
from .helpers import ConfigID


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor platform."""
    cid = cast(ConfigID, entry.entry_id)
    gmi_data = cast(GMIntegData, hass.data[DOMAIN])
    coordinator = gmi_data.coordinators[cid]

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
