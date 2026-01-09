"""The google_maps component."""
from __future__ import annotations

from collections import defaultdict
from functools import partial
import logging
from typing import cast

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIES_FILE, CONF_CREATE_ACCT_ENTITY, DOMAIN
from .coordinator import GMConfigEntry, GMConfigEntryParams, GMDataUpdateCoordinator
from .helpers import (
    CFG_UNIQUE_IDS,
    ConfigID,
    ConfigUniqueIDs,
    UniqueID,
    cookies_file_path,
    dev_ids,
)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _duplicate_usernames(hass: HomeAssistant) -> list[str]:
    """Return duplicate usernames in config entries."""
    username_cfgs: defaultdict[str, list[ConfigEntry]] = defaultdict(list)
    for cfg in hass.config_entries.async_entries(DOMAIN):
        username_cfgs[
            cast(str, cfg.data[CONF_USERNAME] if cfg.version < 3 else cfg.unique_id)
        ].append(cfg)
    return [username for username, cfgs in username_cfgs.items() if len(cfgs) > 1]


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up integration."""
    # In previous integration versions it was possible to create more than one config
    # entry with the same username. This is no longer allowed.
    #
    # If the user had created multiple entries that shared the same username while using
    # a previous integration version, create a repair issue that tells user they need to
    # remove all but one entry per username and abort setup (until user fixes problem.)
    if duplicate_usernames := sorted(_duplicate_usernames(hass)):
        ir.async_create_issue(
            hass,
            DOMAIN,
            "duplicate_usernames",
            # TODO: Make it fixable and add repair flow that for each reused username,
            #       show list of configs using it and ask which to enable and whether or
            #       not to remove the others???
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="duplicate_usernames",
            translation_placeholders={"usernames": ", ".join(duplicate_usernames)},
        )
        return False

    hass.data[CFG_UNIQUE_IDS] = ConfigUniqueIDs(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry."""
    _LOGGER.warning("%s: Migrating from version %s", entry.title, entry.version)

    if entry.version > 3:
        # Can't downgrade from some unknown future version.
        return False

    data = dict(entry.data)
    options = dict(entry.options)
    unique_id = entry.unique_id

    if entry.version == 1:
        options[CONF_COOKIES_FILE] = data.pop(CONF_COOKIES_FILE)

    if entry.version <= 2:
        unique_id = cast(str, data.pop(CONF_USERNAME))
        # TODO: Put CONF_COOKIES_FILE back in data (to support reconfig flow)???

    hass.config_entries.async_update_entry(
        entry, data=data, options=options, unique_id=unique_id, version=3
    )
    _LOGGER.warning(
        "%s: Migration to version %s successful", entry.title, entry.version
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    coordinator = GMDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = GMConfigEntryParams(coordinator, entry)

    # TODO: After dropping support for HA versions before 2025.8, entry_updated can be
    #       removed if GoogleMapsOptionsFlow is based on OptionsFlowWithReload instead
    #       of OptionsFlow.
    async def entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Handle config entry update."""
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


def _del_cookies_file(hass: HomeAssistant, cookies_file: str) -> None:
    """Delete cookies file."""
    hass.async_add_executor_job(
        partial(cookies_file_path(hass, cookies_file).unlink, missing_ok=True)
    )


async def async_unload_entry(hass: HomeAssistant, entry: GMConfigEntry) -> bool:
    """Unload a config entry."""
    result = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)

    if entry.disabled_by:
        # Entry was just disabled.
        # Release all the unique IDs that were "owned" by this config entry.
        hass.data[CFG_UNIQUE_IDS].release_all(ConfigID(entry.entry_id))
    else:
        # Entry is being reloaded, possibly due to a reauthentication, reconfiguration,
        # options update or a manual reload initiated by the user.
        if (
            not entry.options[CONF_CREATE_ACCT_ENTITY]
            and entry.runtime_data.setup_options[CONF_CREATE_ACCT_ENTITY]
        ):
            # User turned off the "account entity" option. Clean up the entity & device
            # registry entries that were created for it.

            # The "account" entity's unique ID is the config entry's username, aka the
            # account's email address, which is also the config entry's unique ID.
            uid = UniqueID(cast(str, entry.unique_id))

            ent_reg = er.async_get(hass)
            if entity_id := ent_reg.async_get_entity_id(DT_DOMAIN, DOMAIN, uid):
                ent_reg.async_remove(entity_id)
            dev_reg = dr.async_get(hass)
            if device := dev_reg.async_get_device(dev_ids(uid)):
                dev_reg.async_remove_device(device.id)

        if entry.options[CONF_COOKIES_FILE] != (
            cookies_file := entry.runtime_data.setup_options[CONF_COOKIES_FILE]
        ):
            # Cookies file has changed. Delete the old one.
            _del_cookies_file(hass, cookies_file)

    return result


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    _del_cookies_file(hass, entry.options[CONF_COOKIES_FILE])
    if not _duplicate_usernames(hass):
        ir.async_delete_issue(hass, DOMAIN, "duplicate_usernames")
