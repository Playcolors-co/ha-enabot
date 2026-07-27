"""Live camera per EBO robot — RTSP source (HA's stream/go2rtc serve it as WebRTC)."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAC, CONF_MODEL, CONF_NAME, CONF_NODE, CONF_RTSP, CONF_SN, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the camera for one robot."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EboCamera(entry, coordinator)])


class EboCamera(Camera):
    """A robot's live camera. Entity name = the device (robot) name."""

    _attr_has_entity_name = True
    _attr_name = None  # -> the entity is named after the device (the robot)
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__()
        data = entry.data
        self._coordinator = coordinator
        self._node = data.get(CONF_NODE)
        self._rtsp_fallback: str = data.get(CONF_RTSP) or ""
        sn = str(data.get(CONF_SN) or data.get(CONF_NODE) or entry.entry_id)
        self._attr_unique_id = f"{sn}_camera"

        connections = set()
        mac = data.get(CONF_MAC)
        if mac:
            # Same MAC as the robot's other entities -> HA MERGES them into ONE device per robot,
            # so the live camera sits next to the sensors/controls automatically.
            connections = {(CONNECTION_NETWORK_MAC, format_mac(mac))}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sn)},
            connections=connections,
            name=data.get(CONF_NAME, "EBO"),
            manufacturer="Enabot",
            model=data.get(CONF_MODEL, "EBO Air 2"),
        )

    async def stream_source(self) -> str | None:
        """Return the RTSP URL.

        Prefer the add-on's *current* URL from the coordinator (self-heals when the add-on's
        internal hostname/IP changes), falling back to whatever was stored at add time.
        """
        robot = (self._coordinator.data or {}).get(self._node) or {}
        return robot.get(CONF_RTSP) or self._rtsp_fallback or None
