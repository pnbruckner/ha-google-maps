"""The google_maps component."""
from __future__ import annotations

from functools import partial
import logging

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
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
    cookies_file_path,
    dev_ids,
)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _duplicate_usernames(hass: HomeAssistant) -> list[str]:
    """Check configs for duplicate usernames."""
    username_cfgs: dict[str, list[GMConfigEntry]] = {}
    for cfg in hass.config_entries.async_entries(DOMAIN):
        username_cfgs.setdefault(cfg.data[CONF_USERNAME], []).append(cfg)
    return [username for username, cfgs in username_cfgs.items() if len(cfgs) > 1]


async def async_setup(hass: HomeAssistant, _: ConfigType) -> bool:
    """Set up integration."""
    # In previous integration versions it was possible to create more than one config
    # entry with the same username. This is no longer allowed.
    #
    # If the user had created multiple entries that shared the same username while using
    # a previous integration version, warn them via a repair issue that they need to
    # remove all but one entry per username.
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

    hass.data[CFG_UNIQUE_IDS] = ConfigUniqueIDs(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: GMConfigEntry) -> bool:
    """Migrate config entry."""
    _LOGGER.warning("%s: Migrating from version %s", entry.title, entry.version)

    if entry.version > 2:
        # Can't downgrade from some unknown future version.
        return False

    if entry.version == 1:
        data = dict(entry.data)
        options = dict(entry.options)
        options[CONF_COOKIES_FILE] = data.pop(CONF_COOKIES_FILE)
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=2
        )

    _LOGGER.warning(
        "%s: Migration to version %s successful", entry.title, entry.version
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GMConfigEntry) -> bool:
    """Set up config entry."""
    # Per the comment in async_setup, multiple config entries using the same username is
    # no longer allowed. However, if the user had created this situation with an older
    # integration version, refuse to setup any config entries that share a username with
    # other entries.
    if (username := entry.data[CONF_USERNAME]) in _duplicate_usernames(hass):
        # Normally unique IDs would be released when the config entry is unloaded.
        # However, since this one is failing to load, release any it might have owned
        # per the Entity Registry.
        hass.data[CFG_UNIQUE_IDS].release_all(ConfigID(entry.entry_id))
        raise ConfigEntryError(
            f"Username {username} used by multiple integration entries"
        )

    coordinator = GMDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = GMConfigEntryParams(
        coordinator, entry.options[CONF_CREATE_ACCT_ENTITY]
    )

    # TODO: After dropping support for HA versions before 2025.8, entry_updated can be
    #       removed if GoogleMapsOptionsFlow is based on OptionsFlowWithReload instead
    #       of OptionsFlow.
    async def entry_updated(hass: HomeAssistant, entry: GMConfigEntry) -> None:
        """Handle config entry update."""
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GMConfigEntry) -> bool:
    """Unload a config entry."""
    result = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    # Clean up entity & device registry entries for "account entity" if config entry was
    # setup with that option enabled but it is no longer enabled (i.e., this option was
    # just turned off by user, causing the config entry to be reloaded.)
    if (
        entry.runtime_data.setup_with_acct_entity
        and not entry.options[CONF_CREATE_ACCT_ENTITY]
    ):
        # This shouldn't be able to happen if the config entry is now disabled.
        # I.e., if it's getting unloaded due to being disabled, it can't also have had
        # its options changed.
        assert not entry.disabled_by

        username = entry.data[CONF_USERNAME]
        ent_reg = er.async_get(hass)
        if entity_id := ent_reg.async_get_entity_id(DT_DOMAIN, DOMAIN, username):
            ent_reg.async_remove(entity_id)
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get_device(dev_ids(username)):
            dev_reg.async_remove_device(device.id)
    hass.data[CFG_UNIQUE_IDS].release_all(ConfigID(entry.entry_id))
    return result


async def async_remove_entry(hass: HomeAssistant, entry: GMConfigEntry) -> None:
    """Remove a config entry."""
    hass.async_add_executor_job(
        partial(
            cookies_file_path(hass, entry.options[CONF_COOKIES_FILE]).unlink,
            missing_ok=True,
        )
    )
    if not _duplicate_usernames(hass):
        ir.async_delete_issue(hass, DOMAIN, "duplicate_usernames")
