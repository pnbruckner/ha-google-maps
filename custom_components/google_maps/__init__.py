"""The google_maps component."""
from __future__ import annotations

from functools import partial
import logging
from typing import cast

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_COOKIES_FILE, CONF_CREATE_ACCT_ENTITY, DOMAIN, NAME_PREFIX
from .coordinator import GMDataUpdateCoordinator, GMIntegData
from .helpers import ConfigID, ConfigUniqueIDs, cookies_file_path

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.DEVICE_TRACKER]


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

    if not (gmi_data := cast(GMIntegData | None, hass.data.get(DOMAIN))):
        hass.data[DOMAIN] = gmi_data = GMIntegData(ConfigUniqueIDs(hass))

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
