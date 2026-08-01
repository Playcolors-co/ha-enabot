"""Switches: camera on/off, laser on/off."""

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
    add([EboCameraSwitch(c, entry), EboLaserSwitch(c, entry),
         EboObstacleSwitch(c, entry), EboListenSwitch(c, entry)])


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


class EboLaserSwitch(EboEntity, SwitchEntity):
    """The pointer laser — a real toggle (the robot reports its state as state.laser)."""

    _attr_icon = "mdi:laser-pointer"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "laser")
        self._attr_name = "Laser"

    @property
    def is_on(self) -> bool:
        return self._state.get("laser") == "true"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "laser/set", "on")

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "laser/set", "off")


class EboObstacleSwitch(EboEntity, SwitchEntity):
    """Collision avoidance assist — the app's fullscreen "Collision Avoidance Assist".
    Single-field setter (opcode 103045); the robot echoes it in the settings report, so this
    reflects the real state."""

    _attr_icon = "mdi:wall"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "avoid_obstacle")
        self._attr_name = "Collision avoidance"

    @property
    def is_on(self) -> bool:
        return self._state.get("avoid_obstacle") == "true"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "avoid_obstacle/set", "on")

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "avoid_obstacle/set", "off")


class EboListenSwitch(EboEntity, SwitchEntity):
    """Listen — the robot's microphone. The robot only publishes its mic once asked to (the add-on
    sends that command); the audio then rides inside the camera stream."""

    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "listen")
        self._attr_name = "Listen"

    @property
    def is_on(self) -> bool:
        return self._state.get("listen") != "false"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "listen/set", "on")

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.cmd(self._node, "listen/set", "off")
