"""Switch: camera on/off."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([EboCameraSwitch(c, entry)])


class EboCameraSwitch(EboEntity, SwitchEntity):
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "camera")
        self._attr_name = "Camera"

    @property
    def is_on(self) -> bool:
        return self._robot.get("camera") == "on"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "camera/set", "on")

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "camera/set", "off")
