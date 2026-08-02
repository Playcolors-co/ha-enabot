"""Numbers: movement speed, speaker volume, talkback volume."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([
        EboNumber(c, entry, "speed", "Speed", "speed/set", "speed", 1, 100, "mdi:speedometer"),
        # Two distinct volumes, as in the app: the robot's own sounds/voice, and how loud YOU are
        # when talking through it.
        EboNumber(c, entry, "volume", "Speaker volume", "volume/set",
                  "volume", 0, 100, "mdi:volume-high", config=True),
        EboNumber(c, entry, "talkback_volume", "Talkback volume", "talkback_volume/set",
                  "talkback_volume", 0, 100, "mdi:account-voice", config=True),
    ])


class EboNumber(EboEntity, NumberEntity):
    def __init__(self, coordinator, entry, key, name, suffix, field, lo, hi, icon,
                 config=False):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._suffix = suffix
        self._field = field
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = 1
        self._attr_icon = icon
        if config:
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self):
        try:
            return float(self._state.get(self._field))
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.cmd(self._node, self._suffix, int(value))
