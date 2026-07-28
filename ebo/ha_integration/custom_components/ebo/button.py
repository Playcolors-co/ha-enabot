"""Buttons: wake, standby, dock, and driving (forward/back/left/right/stop). Laser is a switch.

The driving buttons make the robot controllable from any Home Assistant dashboard — i.e. by
non-admin users too (the add-on panel is admin-only). Each press sends a short, watchdog-limited
move (the robot stops on its own after ~1s), so a plain button is safe to expose.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity

_HOLD = 1.1          # seconds each press drives before the watchdog stops the robot
_SPEED = 60          # ±100 scale


def _vec(ly=0, rx=0):
    return '{"ly":%d,"rx":%d,"hold":%.1f}' % (ly, rx, _HOLD)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([
        # Wake joins the RTC session (camera/set on) — a fresh join is what actually wakes the robot
        # from standby, like the app; the plain isSleeping opcode does NOT reliably wake it. Standby
        # leaves the session (connected/set off) so the robot goes back to ZZ. (Laser is a switch now.)
        EboButton(c, entry, "wake", "Wake", "camera/set", "on", "mdi:weather-sunny"),
        EboButton(c, entry, "standby", "Standby", "connected/set", "off", "mdi:sleep"),
        EboButton(c, entry, "dock", "Return to base", "dock", "", "mdi:home-import-outline"),
        # driving — usable on any dashboard (non-admin friendly)
        EboButton(c, entry, "forward", "Forward", "move/vector", _vec(ly=-_SPEED), "mdi:arrow-up-bold"),
        EboButton(c, entry, "back", "Back", "move/vector", _vec(ly=_SPEED), "mdi:arrow-down-bold"),
        EboButton(c, entry, "left", "Turn left", "move/vector", _vec(rx=-_SPEED), "mdi:arrow-left-bold"),
        EboButton(c, entry, "right", "Turn right", "move/vector", _vec(rx=_SPEED), "mdi:arrow-right-bold"),
        EboButton(c, entry, "stop", "Stop", "move/vector", _vec(), "mdi:stop"),
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
