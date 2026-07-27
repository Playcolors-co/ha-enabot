"""Base entity: device grouping (merges with the add-on's MQTT device via MAC) + node data."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, CONF_MODEL, CONF_NAME, CONF_NODE, CONF_SN, DOMAIN
from .coordinator import EboCoordinator


class EboEntity(CoordinatorEntity[EboCoordinator]):
    """Common device info + per-node data access."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EboCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._node = entry.data[CONF_NODE]
        sn = str(entry.data.get(CONF_SN) or self._node)
        self._attr_unique_id = f"{sn}_{key}"
        connections = set()
        mac = entry.data.get(CONF_MAC)
        if mac:
            connections = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sn)},
            connections=connections,
            name=entry.data.get(CONF_NAME, "EBO"),
            manufacturer="Enabot",
            model=entry.data.get(CONF_MODEL, "EBO Air 2"),
        )

    @property
    def _robot(self) -> dict:
        return (self.coordinator.data or {}).get(self._node, {})

    @property
    def _state(self) -> dict:
        return self._robot.get("state") or {}

    @property
    def available(self) -> bool:
        return self._node in (self.coordinator.data or {})
