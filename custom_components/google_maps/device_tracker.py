"""Support for Google Maps location sharing."""
from __future__ import annotations

from collections.abc import Mapping
from copy import copy
import logging
from typing import Any, cast

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.const import ATTR_BATTERY_CHARGING
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ADDRESS,
    ATTR_LAST_SEEN,
    ATTR_NICKNAME,
    ATTRIBUTION,
    CONF_MAX_GPS_ACCURACY,
    DT_NO_RECORD_ATTRS,
)
from .coordinator import GMConfigEntry, GMDataUpdateCoordinator
from .helpers import (
    CFG_UNIQUE_IDS,
    ConfigID,
    LocationData,
    PersonData,
    UniqueID,
    dev_ids,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the device tracker platform."""
    cid = ConfigID(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    unique_ids = hass.data[CFG_UNIQUE_IDS]

    max_gps_accuracy = entry.options[CONF_MAX_GPS_ACCURACY]
    entities: dict[UniqueID, GoogleMapsDeviceTracker] = {}

    @callback
    def update_entities() -> None:
        """Update entities for people."""
        uids = frozenset(coordinator.data)
        # NOTE: Unique ID of "account entity" is the account's email address.
        if remove_uids := entities.keys() - uids:
            for remove_uid in remove_uids:
                entity = entities.pop(remove_uid)
                _LOGGER.warning("Data no longer available for %s", entity.name)
                entry.async_create_background_task(
                    hass, entity.async_remove(), "Remove GoogleMapsDeviceTracker entity"
                )
                # Do not release uid from unique_ids in case data comes back and entity
                # can be created again. It will still be in the Entity Registry, at least
                # if and until the user removes it from the registry. And even then, no
                # need to release the uid from unique_ids since it's not likely to come
                # back and it will be released if entry gets unloaded and it won't get
                # added next time entry loads.
        if create_uids := unique_ids.take(cid, uids) - entities.keys():
            new_entities = {
                uid: GoogleMapsDeviceTracker(coordinator, uid, max_gps_accuracy)
                for uid in create_uids
            }
            async_add_entities(new_entities.values())
            entities.update(new_entities)

    update_entities()
    entry.async_on_unload(coordinator.async_add_listener(update_entities))


class GoogleMapsDeviceTracker(
    CoordinatorEntity[GMDataUpdateCoordinator], TrackerEntity, RestoreEntity
):
    """Google Maps Device Tracker."""

    _unrecorded_attributes = DT_NO_RECORD_ATTRS
    _attr_attribution = ATTRIBUTION
    # With name == None and has_entity_name == True, entity will get device's name.
    _attr_name = None
    _attr_has_entity_name = True
    _attr_translation_key = "tracker"

    _loc: LocationData | None = None
    _skip_reason: str = ""

    def __init__(
        self, coordinator: GMDataUpdateCoordinator, uid: UniqueID, max_gps_accuracy: int
    ) -> None:
        """Initialize Google Maps Device Tracker."""
        super().__init__(coordinator)
        # NOTE: Unique ID of "account entity" is the account's email address.
        self._attr_unique_id = uid
        self._max_gps_accuracy = max_gps_accuracy

        # Use misc data now. Loc data will be handled in async_added_to_hass.
        data = coordinator.data[uid]
        assert data.misc
        self._misc = copy(data.misc)
        full_name = data.misc.full_name
        name = f"Google Maps {full_name}"
        # For some reason, the device_tracker component doesn't allow entity to be
        # associated with a device. This appears to be for some legacy reason that no
        # longer exists or makes sense. E.g., some built-in integrations assign device
        # info for device_tracker entities.
        self._attr_device_info = dr.DeviceInfo(  # type: ignore[assignment]
            identifiers=dev_ids(uid), name=name, serial_number=uid
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return entity specific state attributes."""
        attrs: dict[str, Any] = {ATTR_NICKNAME: self._misc.nickname}
        if (charging := self._misc.battery_charging) is not None:
            attrs[ATTR_BATTERY_CHARGING] = charging
        if self._loc:
            attrs[ATTR_ADDRESS] = self._loc.address
            attrs[ATTR_LAST_SEEN] = dt_util.as_local(self._loc.last_seen)
        return dict(sorted(attrs.items()))

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture to use in the frontend, if any."""
        return self._misc.entity_picture

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True

    @property
    def force_update(self) -> bool:
        """Return True if state updates should be forced."""
        return False

    @property
    def battery_level(self) -> int | None:
        """Return the battery level of the device."""
        return self._misc.battery_level

    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy of the device."""
        if self._loc is None:
            return 0
        return self._loc.gps_accuracy

    @property
    def latitude(self) -> float | None:
        """Return the latitude value of the device."""
        if self._loc is None:
            return None
        return self._loc.latitude

    @property
    def longitude(self) -> float | None:
        """Rerturn the longitude value of the device."""
        if self._loc is None:
            return None
        return self._loc.longitude

    @property
    def extra_restore_state_data(self) -> PersonData:
        """Return Google Maps specific state data to be restored."""
        # TODO: Still save/restore misc???
        return PersonData(self._loc, self._misc)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        # Restore state if possible.
        if (last_extra_data := await self.async_get_last_extra_data()) and (
            last_person_data := PersonData.from_dict(last_extra_data.as_dict())
        ):
            # Always restore loc data as "previous location" first, then overwrite
            # with new location below if available and "better."
            self._loc = last_person_data.loc

        # Now that previous state has been restored, update with new data if possible.
        # Note that although the Entity was created only when data for this uid was
        # available, it's possible (although not very likely) for the coordinator to
        # have updated since then and now there is no data for this uid.
        if not (data := self.coordinator.data.get(cast(UniqueID, self.unique_id))):
            return
        assert data.loc
        self._update_loc(copy(data.loc))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not (data := self.coordinator.data.get(cast(UniqueID, self.unique_id))):
            # Data no longer availble. Entity will be removed.
            return
        assert data.misc
        assert data.loc
        self._misc = copy(data.misc)
        self._update_loc(copy(data.loc))
        super()._handle_coordinator_update()

    def _update_loc(self, loc: LocationData) -> None:
        """Update location data if possible."""
        last_seen = loc.last_seen
        # Don't use "new" loc data if it really isn't new.
        if prev_seen := self._loc and self._loc.last_seen:
            if last_seen < prev_seen:
                self._log_ignore_reason(
                    "timestamp went backwards: "
                    f"{dt_util.as_local(last_seen)} < {dt_util.as_local(prev_seen)}"
                )
                return
            if last_seen == prev_seen:
                return

        last_gps_accuracy = loc.gps_accuracy
        if prev_gps_accuracy := self._loc and self._loc.gps_accuracy:
            # We have previous loc data.
            if prev_gps_accuracy <= self._max_gps_accuracy:
                # Previous loc data is "accurate."
                # Don't use new loc data if it is inaccurate.
                if last_gps_accuracy > self._max_gps_accuracy:
                    self._log_ignore_reason(
                        f"GPS accuracy ({last_gps_accuracy}) is greater than limit "
                        f"({self._max_gps_accuracy})"
                    )
                    return
            # Previous loc data is inaccurate.
            # Don't use new data if it is less accurate.
            elif last_gps_accuracy > prev_gps_accuracy:
                self._log_ignore_reason(
                    f"GPS accuracy ({last_gps_accuracy}) is greater than limit "
                    f"({self._max_gps_accuracy}) and worse than previous "
                    f"({prev_gps_accuracy})"
                )
                return

        self._loc = loc

    def _log_ignore_reason(self, reason: str) -> None:
        """Log reason for ignoring location data."""
        if reason != self._skip_reason:
            self._skip_reason = reason
            _LOGGER.debug("Ignoring %s location data because %s", self.name, reason)
