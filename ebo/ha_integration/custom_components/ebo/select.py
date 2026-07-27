"""Selects: video quality, image style, eyes."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([
        EboSelect(c, entry, "video_quality", "Video quality", "video_quality/set",
                  "video_quality", ["Low", "Medium", "High"], "mdi:high-definition"),
        EboSelect(c, entry, "image_style", "Image style", "image_style/set",
                  "image_style", ["Standard", "Vivid", "Soft"], "mdi:image-filter-vintage"),
        EboSelect(c, entry, "eyes", "Eyes", "eyes/set", "eyes",
                  ["Dynamic", "Clock", "Custom"], "mdi:eye"),
    ])


class EboSelect(EboEntity, SelectEntity):
    def __init__(self, coordinator, entry, key, name, suffix, field, options, icon):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._suffix = suffix
        self._field = field
        self._attr_options = options
        self._attr_icon = icon

    @property
    def current_option(self):
        v = self._state.get(self._field)
        return v if v in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.cmd(self._node, self._suffix, option)
