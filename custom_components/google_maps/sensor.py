"""Google Maps sensor."""

from collections.abc import Callable
from typing import cast, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfRatio
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator
from .helpers import UniqueID, dev_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""

    async def add_new_sensors() -> None:
        """Add new sensors."""
        new_sensor_queue = entry.runtime_data.new_sensor_queue
        while True:
            sensor = await new_sensor_queue.get()
            async_add_entities([sensor])

    entry.async_create_background_task(
        hass, add_new_sensors(), f"Add new sensor entities for {entry.title}"
    )


class GoogleMapsBatLvlSensor(CoordinatorEntity[GMDataUpdateCoordinator], SensorEntity):
    """Google Maps Battery Level Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_has_entity_name = True
    _attr_translation_key = "bat_lvl"
    _attr_native_unit_of_measurement = UnitOfRatio.PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: GMDataUpdateCoordinator,
        uid: UniqueID,
        track_entity: Callable[[str], None],
    ) -> None:
        """Initialize Google Maps Device Tracker."""
        super().__init__(coordinator)

        misc = coordinator.data[uid].misc
        name = f"Google Maps {misc.full_name}"

        # NOTE: Unique ID of "account entity" is the account's email address.
        self._attr_unique_id = uid
        self._attr_device_info = dr.DeviceInfo(
            identifiers=dev_ids(uid), name=name, serial_number=uid
        )
        self._attr_native_value = misc.battery_level

        self._track_entity = track_entity

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Now that entity ID has been determined, add it to list of entity IDs to
        # monitor being removed from entity registry.
        self._track_entity(self.entity_id)

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if data := self.coordinator.data.get(cast(UniqueID, self.unique_id)):
            self._attr_native_value = data.misc.battery_level
        else:
            self._attr_native_value = None
        super()._handle_coordinator_update()
