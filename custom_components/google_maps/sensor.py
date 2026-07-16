"""Google Maps sensors."""
from __future__ import annotations

import asyncio
from copy import copy
from datetime import datetime
from functools import partial

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfLength
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_MAX_GPS_ACCURACY, MISSING_DATA_GRACE_PERIOD
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator
from .helpers import (
    CFG_UNIQUE_IDS,
    ConfigID,
    LocationData,
    UniqueID,
    dev_ids,
    resolve_loc_update,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""
    cid = ConfigID(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    max_gps_accuracy = entry.options[CONF_MAX_GPS_ACCURACY]
    unique_ids = hass.data[CFG_UNIQUE_IDS]

    lock = asyncio.Lock()
    entities: dict[UniqueID, list[GoogleMapsSensor]] = {}
    # Value is the unsub callback while the removal timer is pending, or None once it
    # has fired. The key stays in this dict (matching device_tracker.py's approach)
    # until the person's data becomes available again.
    missing: dict[UniqueID, CALLBACK_TYPE | None] = {}

    def schedule_entity_removal(uid: UniqueID) -> None:
        """Schedule entities to be removed."""
        assert uid not in missing
        missing[uid] = async_call_later(
            hass, MISSING_DATA_GRACE_PERIOD, partial(remove_entities, uid)
        )

    def unschedule_entity_removal(uid: UniqueID) -> None:
        """Unschedule removal of entities."""
        if unsub := missing.pop(uid):
            unsub()

    async def remove_entities(uid: UniqueID, _: datetime) -> None:
        """Remove entities whose person data has been missing too long."""
        async with lock:
            missing[uid] = None
            removed = entities.pop(uid)
            await asyncio.gather(*(entity.async_remove() for entity in removed))

    async def update_entities() -> None:
        """Update entities for people."""
        async with lock:
            uids = frozenset(coordinator.data)

            for uid in missing.keys() & uids:
                unschedule_entity_removal(uid)

            for uid in entities.keys() - uids - missing.keys():
                schedule_entity_removal(uid)

            if create_uids := unique_ids.take(cid, uids) - entities.keys():
                new_entities: list[GoogleMapsSensor] = []
                for uid in create_uids:
                    person_entities: list[GoogleMapsSensor] = [
                        GoogleMapsAddressSensor(coordinator, uid, max_gps_accuracy),
                        GoogleMapsLastSeenSensor(coordinator, uid, max_gps_accuracy),
                        GoogleMapsGpsAccuracySensor(
                            coordinator, uid, max_gps_accuracy
                        ),
                        GoogleMapsBatteryLevelSensor(coordinator, uid),
                    ]
                    entities[uid] = person_entities
                    new_entities.extend(person_entities)
                async_add_entities(new_entities)

    @callback
    def update_entities_cb() -> None:
        """Update entities for people."""
        entry.async_create_background_task(
            hass, update_entities(), f"Update sensors for {entry.title}"
        )

    await update_entities()
    entry.async_on_unload(coordinator.async_add_listener(update_entities_cb))


class GoogleMapsSensor(CoordinatorEntity[GMDataUpdateCoordinator], SensorEntity):
    """Base class for Google Maps per-person sensors."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: GMDataUpdateCoordinator, uid: UniqueID, key: str
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._uid = uid
        self._attr_unique_id = f"{uid}_{key}"
        self._attr_translation_key = key
        # Deliberately minimal: the device itself (name, etc.) is established by the
        # GoogleMapsDeviceTracker entity for this uid; this just attaches to it.
        self._attr_device_info = dr.DeviceInfo(identifiers=dev_ids(uid))

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True


class GoogleMapsLocationSensor(GoogleMapsSensor):
    """Base class for sensors driven by (filtered) location data.

    Applies the same "refined filtering" rule as GoogleMapsDeviceTracker so values
    shown here stay consistent with the tracker's own position. Note that, unlike the
    tracker, this filtering memory does not survive a Home Assistant restart, so the
    first update after a restart is always accepted regardless of accuracy.
    """

    _loc: LocationData | None = None

    def __init__(
        self,
        coordinator: GMDataUpdateCoordinator,
        uid: UniqueID,
        key: str,
        max_gps_accuracy: int,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, uid, key)
        self._max_gps_accuracy = max_gps_accuracy
        self._update_loc(copy(coordinator.data[uid].loc))

    def _update_loc(self, loc: LocationData) -> None:
        """Update location data if it passes the same filter as the tracker."""
        self._loc, _ = resolve_loc_update(self._loc, loc, self._max_gps_accuracy)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not (data := self.coordinator.data.get(self._uid)):
            # Data not available. Keep current data for now.
            return
        self._update_loc(copy(data.loc))
        super()._handle_coordinator_update()


class GoogleMapsAddressSensor(GoogleMapsLocationSensor):
    """Google Maps address sensor."""

    _attr_icon = "mdi:map-marker"

    def __init__(
        self, coordinator: GMDataUpdateCoordinator, uid: UniqueID, max_gps_accuracy: int
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, uid, "address", max_gps_accuracy)

    @property
    def native_value(self) -> str | None:
        """Return the address of the device."""
        return self._loc.address if self._loc else None


class GoogleMapsLastSeenSensor(GoogleMapsLocationSensor):
    """Google Maps last seen sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: GMDataUpdateCoordinator, uid: UniqueID, max_gps_accuracy: int
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, uid, "last_seen", max_gps_accuracy)

    @property
    def native_value(self) -> datetime | None:
        """Return when the device was last seen."""
        return self._loc.last_seen if self._loc else None


class GoogleMapsGpsAccuracySensor(GoogleMapsLocationSensor):
    """Google Maps GPS accuracy sensor."""

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: GMDataUpdateCoordinator, uid: UniqueID, max_gps_accuracy: int
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, uid, "gps_accuracy", max_gps_accuracy)

    @property
    def native_value(self) -> int | None:
        """Return the GPS accuracy of the device's location."""
        return self._loc.gps_accuracy if self._loc else None


class GoogleMapsBatteryLevelSensor(GoogleMapsSensor):
    """Google Maps battery level sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _battery_level: int | None = None

    def __init__(self, coordinator: GMDataUpdateCoordinator, uid: UniqueID) -> None:
        """Initialize sensor."""
        super().__init__(coordinator, uid, "battery_level")
        self._battery_level = coordinator.data[uid].misc.battery_level

    @property
    def native_value(self) -> int | None:
        """Return the battery level of the device."""
        return self._battery_level

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not (data := self.coordinator.data.get(self._uid)):
            # Data not available. Keep current data for now.
            return
        self._battery_level = data.misc.battery_level
        super()._handle_coordinator_update()
