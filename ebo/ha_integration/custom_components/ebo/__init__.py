"""The EBO for Home Assistant integration — a device + live camera + native entities per robot.

Each config entry is one robot, created automatically from the add-on (Supervisor discovery / the
add-on's data API) or manually. No MQTT: entities are fed by the add-on's HTTP API.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_API, CONF_TOKEN, DOMAIN
from .coordinator import EboCoordinator

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one robot from a config entry."""
    hass.data.setdefault(DOMAIN, {})
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
