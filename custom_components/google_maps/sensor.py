"""Google Maps sensor."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, override

from propcache.api import cached_property

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfRatio, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ATTRIBUTION
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator
from .helpers import PersonData, UniqueID

SCAN_INTERVAL = timedelta(seconds=15)


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""

    async def add_new_sensors() -> None:
        """Add new sensors."""
        new_sensors_queue = entry.runtime_data.new_sensors_queue
        while True:
            async_add_entities(await new_sensors_queue.get())

    entry.async_create_background_task(
        hass, add_new_sensors(), f"Add new sensor entities for {entry.title}"
    )


class GoogleMapsSensor(CoordinatorEntity[GMDataUpdateCoordinator], SensorEntity, ABC):
    """Google Maps Sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GMDataUpdateCoordinator,
        uid: UniqueID,
        device_info: dr.DeviceInfo,
        track_entity: Callable[[str], None],
    ) -> None:
        """Initialize Google Maps Sensor."""
        super().__init__(coordinator)

        # NOTE: Unique ID of "account entity" is the account's email address.
        self._attr_unique_id = f"{uid}-{self._attr_translation_key}"
        self._attr_device_info = device_info
        self._attr_native_value = self._native_value(coordinator.data[uid])

        self._uid_base = uid
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
        if data := self.coordinator.data.get(self._uid_base):
            self._attr_native_value = self._native_value(data)
        else:
            self._attr_native_value = None
        super()._handle_coordinator_update()

    @abstractmethod
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""


class GoogleMapsBatLvlSensor(GoogleMapsSensor):
    """Google Maps Battery Level Sensor."""

    _attr_translation_key = "bat_lvl"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfRatio.PERCENTAGE

    @override
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""
        return data.misc.battery_level


class GoogleMapsAddressSensor(GoogleMapsSensor):
    """Google Maps Address Sensor."""

    _attr_translation_key = "address"
    _attr_entity_registry_enabled_default = False

    @override
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""
        return data.loc.address


class GoogleMapsGPSAccuracySensor(GoogleMapsSensor):
    """Google Maps GPS Accuracy Sensor."""

    _attr_translation_key = "gps_accuracy"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_icon = "mdi:map-marker-radius-outline"

    @override
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""
        return data.loc.gps_accuracy


def _nearest_second(time: datetime) -> datetime:
    """Round time to nearest second."""
    return time.replace(microsecond=0) + timedelta(
        seconds=0 if time.microsecond < 500000 else 1
    )


class GoogleMapsLastSeenSensor(GoogleMapsSensor):
    """Google Maps Last Seen Sensor."""

    _attr_translation_key = "last_seen"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @override
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""
        # SensorEntity will display second resolution, but without rounding. So do the
        # rounding here.
        return _nearest_second(data.loc.last_seen)


class GoogleMapsStaleSensor(GoogleMapsSensor):
    """Google Maps Stale Sensor."""

    _attr_translation_key = "stale"
    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    @cached_property
    @override
    def should_poll(self) -> bool:
        """Need to poll to update time since last update."""
        return True

    @override
    async def async_update(self) -> None:
        """Update the entity."""
        if not self.enabled:
            return
        self._attr_native_value = self._min_since_last_seen

    @override
    def _native_value(self, data: PersonData) -> Any | None:
        """Return native value."""
        self._last_seen = data.loc.last_seen
        return self._min_since_last_seen

    @property
    def _min_since_last_seen(self) -> float:
        """Return minutes since last seen."""
        return (dt_util.utcnow() - self._last_seen).total_seconds() / 60


def uid_sensors(
    coordinator: GMDataUpdateCoordinator,
    uid: UniqueID,
    device_info: dr.DeviceInfo,
    track_entity: Callable[[str], None],
) -> list[SensorEntity]:
    """Create sensors for UniqueID."""
    return [
        GoogleMapsBatLvlSensor(coordinator, uid, device_info, track_entity),
        GoogleMapsAddressSensor(coordinator, uid, device_info, track_entity),
        GoogleMapsGPSAccuracySensor(coordinator, uid, device_info, track_entity),
        GoogleMapsLastSeenSensor(coordinator, uid, device_info, track_entity),
        GoogleMapsStaleSensor(coordinator, uid, device_info, track_entity),
    ]
