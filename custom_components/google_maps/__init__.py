"""The google_maps component."""
from __future__ import annotations

from functools import partial
import logging
from typing import cast

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIES_FILE, CONF_CREATE_ACCT_ENTITY, DOMAIN, NAME_PREFIX
from .coordinator import GMDataUpdateCoordinator, GMIntegData
from .helpers import ConfigID, ConfigUniqueIDs, cookies_file_path

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

CONFIG_SCHEMA = cv.platform_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up integration."""
    hass.data[DOMAIN] = GMIntegData(ConfigUniqueIDs(hass))
    ent_reg = er.async_get(hass)

    async def device_work_around(_: Event) -> None:
        """Work around for device tracker component deleting devices.

        Applies to HA versions prior to 2024.5:

        The device tracker component level code, at startup, deletes devices that are
        associated only with device_tracker entities. Not only that, it will delete
        those device_tracker entities from the entity registry as well. So, when HA
        shuts down, remove references to devices from our device_tracker entity registry
        entries. They'll get set back up automatically the next time our config is
        loaded (i.e., setup.)
        """
        for c_entry in hass.config_entries.async_entries(DOMAIN):
            for r_entry in er.async_entries_for_config_entry(ent_reg, c_entry.entry_id):
                ent_reg.async_update_entity(r_entry.entity_id, device_id=None)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, device_work_around)
    return True


async def entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry."""
    _LOGGER.debug("%s: Migrating from version %s", entry.title, entry.version)

    if entry.version > 2:
        # Can't downgrade from some unknown future version.
        return False

    if entry.version == 1:
        data = dict(entry.data)
        options = dict(entry.options)
        options[CONF_COOKIES_FILE] = data.pop(CONF_COOKIES_FILE)
        try:
            hass.config_entries.async_update_entry(
                entry, data=data, options=options, version=2
            )
        except TypeError:
            # 2024.2 and earlier did not accept version as a parameter.
            entry.version = 2
            hass.config_entries.async_update_entry(entry, data=data, options=options)

    _LOGGER.debug("%s: Migration to version %s successful", entry.title, entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    cid = ConfigID(entry.entry_id)
    username = entry.data[CONF_USERNAME]
    create_acct_entity = entry.options[CONF_CREATE_ACCT_ENTITY]

    gmi_data = cast(GMIntegData, hass.data.get(DOMAIN))

    # For "account person", unique ID is username (which is also returned in person.id.)
    ent_reg = er.async_get(hass)
    unique_ids = gmi_data.unique_ids
    if create_acct_entity:
        if not unique_ids.own(cid, username) and unique_ids.take(cid, {username}):
            ent_reg.async_get_or_create(
                DT_DOMAIN,
                DOMAIN,
                username,
                config_entry=entry,
                original_name=f"{NAME_PREFIX} {username}",
            )
    elif unique_ids.own(cid, username):
        if entity_id := ent_reg.async_get_entity_id(DT_DOMAIN, DOMAIN, username):
            ent_reg.async_remove(entity_id)
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get_device({(DOMAIN, username)}):
            dev_reg.async_remove_device(device.id)
        unique_ids.release(cid, username)

    coordinator = GMDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    gmi_data.coordinators[cid] = coordinator

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        gmi_data = cast(GMIntegData, hass.data[DOMAIN])
        cid = ConfigID(entry.entry_id)
        del gmi_data.coordinators[cid]
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    gmi_data = cast(GMIntegData, hass.data[DOMAIN])
    gmi_data.unique_ids.remove(ConfigID(entry.entry_id))
    hass.async_add_executor_job(
        partial(
            cookies_file_path(hass, entry.data[CONF_COOKIES_FILE]).unlink,
            missing_ok=True,
        )
    )
