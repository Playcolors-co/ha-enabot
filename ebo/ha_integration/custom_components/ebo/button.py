"""Buttons: return to base (dock), laser."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([
        EboButton(c, entry, "wake", "Wake", "wake", "", "mdi:weather-sunny"),
        EboButton(c, entry, "standby", "Standby", "sleep/set", "on", "mdi:sleep"),
        EboButton(c, entry, "dock", "Return to base", "dock", "", "mdi:home-import-outline"),
        EboButton(c, entry, "laser", "Laser", "laser/set", "on", "mdi:laser-pointer"),
    ])


class EboButton(EboEntity, ButtonEntity):
    def __init__(self, coordinator, entry, key, name, suffix, payload, icon):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._suffix = suffix
        self._payload = payload
        self._attr_icon = icon

    async def async_press(self) -> None:
        await self.coordinator.cmd(self._node, self._suffix, self._payload)
