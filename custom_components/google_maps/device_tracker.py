"""Support for Google Maps location sharing."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from copy import copy
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import logging
from typing import Any, cast

from homeassistant.components.device_tracker.config_entry import (
    DOMAIN as DT_DOMAIN,
    TrackerEntity,
)
from homeassistant.const import ATTR_BATTERY_CHARGING
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_entity_registry_updated_event,
)
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
    MISSING_DATA_GRACE_PERIOD,
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


@dataclass
class MissingParams:
    """Missing parameters."""

    name: str
    unsub: CALLBACK_TYPE | None


async def async_setup_entry(
    hass: HomeAssistant, entry: GMConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the device tracker platform."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    cid = ConfigID(entry.entry_id)
    coordinator = entry.runtime_data.coordinator
    max_gps_accuracy = entry.options[CONF_MAX_GPS_ACCURACY]
    unique_ids = hass.data[CFG_UNIQUE_IDS]

    lock = asyncio.Lock()
    entities: dict[UniqueID, GoogleMapsDeviceTracker] = {}
    missing: dict[UniqueID, MissingParams] = {}
    registered_entities: dict[str, UniqueID] = {
        ent.entity_id: UniqueID(ent.unique_id)
        for ent in er.async_entries_for_config_entry(ent_reg, cid)
        if ent.domain == DT_DOMAIN
    }
    unsub_ent_reg_updates: CALLBACK_TYPE | None = None

    def stop_tracking_ent_reg_updates() -> None:
        """Stop tracking entity registry updates."""
        nonlocal unsub_ent_reg_updates

        if unsub_ent_reg_updates:
            unsub_ent_reg_updates()
            unsub_ent_reg_updates = None

    def track_ent_reg_updates() -> None:
        """Track entity registry updates."""
        nonlocal unsub_ent_reg_updates

        stop_tracking_ent_reg_updates()
        assert not unsub_ent_reg_updates
        if registered_entities:
            unsub_ent_reg_updates = async_track_entity_registry_updated_event(
                hass,
                # Provide a copy of the current keys so tracking/removing is not
                # affected by updates to the dict.
                list(registered_entities),
                entity_registry_updated,
            )

    # Needs to be a callback so it doesn't get run in an executor by
    # async_track_entity_registry_updated_event.
    @callback
    def entity_registry_updated(
        event: Event[er.EventEntityRegistryUpdatedData],
    ) -> None:
        """Handle entity removed from entity registry."""
        if (action := event.data["action"]) not in ("remove", "update"):
            return

        entity_id = event.data["entity_id"]
        if action == "remove":
            uid = registered_entities.pop(entity_id)
            track_ent_reg_updates()

            device = dev_reg.async_get_device(dev_ids(uid))
            if not device:
                # TODO: Remove warning???
                _LOGGER.warning(
                    "Could not find device for removed entity: %s", entity_id
                )
                return
            dev_reg.async_remove_device(device.id)

        # update
        elif old_entity_id := cast(str | None, event.data.get("old_entity_id")):
            track_entity(registered_entities.pop(old_entity_id), entity_id)

    def track_entity(uid: UniqueID, entity_id: str) -> None:
        """Track entity registry updates for entity."""
        if entity_id in registered_entities:
            assert registered_entities[entity_id] == uid
            return
        registered_entities[entity_id] = uid
        track_ent_reg_updates()

    def schedule_entity_removal(uid: UniqueID) -> None:
        """Schedule an entity to be removed."""
        # NOTE: The entity is not removed immediately because there are times when a
        #       person's data goes missing for a little while, but then comes back again
        #       on its own. The state of the entity will just not update during the time
        #       the data is missing.
        assert uid not in missing
        missing[uid] = MissingParams(
            name := entities[uid].log_name,
            async_call_later(
                hass, MISSING_DATA_GRACE_PERIOD, partial(remove_entity, uid)
            ),
        )
        _LOGGER.warning("Data missing for %s", name)

    def unschedule_entity_removal(uid: UniqueID) -> None:
        """Unschedule removal of entity."""
        params = missing.pop(uid)
        if params.unsub:
            params.unsub()
        _LOGGER.warning("Data available again for %s", params.name)

    async def remove_entity(uid: UniqueID, _: datetime) -> None:
        """Remove entity."""
        # NOTE: They will still be in the entity registry, so they will still exist in
        #       HA's state machine, but they will become unavailable. The user can then
        #       completely remove them if they want. Unless, of course, their data comes
        #       back again, in which case, the entity will be recreated.
        # Need to do this with a lock so that update_entities can't create a new Entity
        # for the same UID that would add itself to the state machine, then the
        # async_remove here would remove it from the state machine.
        async with lock:
            assert missing[uid].unsub
            missing[uid].unsub = None
            entity = entities.pop(uid)
            await entity.async_remove()
        # Do not release uid from unique_ids in case data comes back and entity can
        # be created again. It will still be in the Entity Registry, at least if and
        # until the user removes it from the registry. And even then, no need to
        # release the uid from unique_ids since it's not likely to come back and it
        # will be released if entry gets unloaded and it won't get added next time
        # entry loads.

    async def update_entities() -> None:
        """Update entities for people."""
        async with lock:
            # NOTE: Unique ID of "account entity" is the account's email address.
            uids = frozenset(coordinator.data)

            # For any entity that was scheduled to be removed due to its data being
            # missing, cancel the removal if the data is available again.
            for uid in missing.keys() & uids:
                unschedule_entity_removal(uid)

            # For any newly missing data, schedule a task to remove the entity after a
            # grace period.
            for uid in entities.keys() - uids - missing.keys():
                schedule_entity_removal(uid)

            # Attempt to take ownership of any unique IDs that have not yet been taken
            # by other config entries, and create entities for those that have been
            # "taken" and have not had entities created yet.
            if create_uids := unique_ids.take(cid, uids) - entities.keys():
                new_entities = {
                    uid: GoogleMapsDeviceTracker(
                        coordinator, uid, max_gps_accuracy, partial(track_entity, uid)
                    )
                    for uid in create_uids
                }
                async_add_entities(new_entities.values())
                entities.update(new_entities)

    @callback
    def update_entities_cb() -> None:
        """Update entities for people."""
        entry.async_create_background_task(
            hass, update_entities(), f"Update entities for {entry.title}"
        )

    track_ent_reg_updates()
    await update_entities()
    entry.async_on_unload(coordinator.async_add_listener(update_entities_cb))
    entry.async_on_unload(stop_tracking_ent_reg_updates)


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
        self,
        coordinator: GMDataUpdateCoordinator,
        uid: UniqueID,
        max_gps_accuracy: int,
        track_entity: Callable[[str], None],
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
        self._track_entity = track_entity

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

    @property
    def log_name(self) -> str:
        """Return name to be used in log messages."""
        return (
            (self.registry_entry and self.registry_entry.name)
            or self._friendly_name_internal()
            or self.entity_id
            or self._misc.full_name
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        self._track_entity(self.entity_id)

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
            # Data not availble. Keep current data for now.
            # If data is not available long enough, Entity will be removed.
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
            _LOGGER.debug("Ignoring %s location data because %s", self.log_name, reason)
