"""The EBO for Home Assistant integration — a device + live camera + native entities per robot.

Each config entry is one robot, created automatically from the add-on (Supervisor discovery / the
add-on's data API) or manually. No MQTT: entities are fed by the add-on's HTTP API.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

try:  # HA moved is_hassio to helpers.hassio (2025+); fall back for older cores.
    from homeassistant.helpers.hassio import is_hassio
except ImportError:  # pragma: no cover
    from homeassistant.components.hassio import is_hassio

from . import hassio_addon
from .const import CONF_API, CONF_TOKEN, DOMAIN
from .coordinator import EboCoordinator

_LOGGER = logging.getLogger(__name__)

# Camera works from RTSP alone; the rest come from the add-on's data API.
PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
]


async def _async_refresh_endpoint(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-read the add-on's API URL and token from Supervisor.

    They were stored when the robot was added; if the add-on's token is regenerated or its internal
    hostname changes, the stored pair goes stale and every entity would sit unavailable forever with
    no way back except deleting and re-adding the device.
    """
    if not is_hassio(hass):
        return
    try:
        endpoint = await hassio_addon.async_get_endpoint(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("could not re-read the add-on endpoint: %s", err)
        return
    if not endpoint:
        return
    api, token = endpoint
    if api == entry.data.get(CONF_API) and token == entry.data.get(CONF_TOKEN):
        return
    _LOGGER.info("EBO add-on endpoint changed — updating the stored API URL/token")
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_API: api, CONF_TOKEN: token})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one robot from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Before anything else, make sure we hold a *current* endpoint (no listener is registered yet,
    # so updating here can't trigger a reload loop).
    await _async_refresh_endpoint(hass, entry)
    coordinator = EboCoordinator(hass, entry.data.get(CONF_API, ""),
                                 entry.data.get(CONF_TOKEN, ""))
    # Don't block setup if the API is momentarily down — the camera still works; the coordinator
    # entities show unavailable until the API responds.
    await coordinator.async_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
