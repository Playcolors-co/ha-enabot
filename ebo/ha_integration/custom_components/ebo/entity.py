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


def ebo_device_info(entry: ConfigEntry, robot: dict | None = None) -> DeviceInfo:
    """One device per robot, shared by every platform (camera included) so they merge into a single
    Home Assistant device instead of two. Keep identifiers/connections stable: changing them would
    orphan the user's existing device and its history."""
    sn = str(entry.data.get(CONF_SN) or entry.data.get(CONF_NODE) or entry.entry_id)
    connections = set()
    mac = entry.data.get(CONF_MAC)
    if mac:
        connections = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
    state = (robot or {}).get("state") or {}
    fw = " · ".join(x for x in ("IPC %s" % state["fw_ipc"] if state.get("fw_ipc") else "",
                               "MCU %s" % state["fw_mcu"] if state.get("fw_mcu") else "") if x)
    info = DeviceInfo(
        identifiers={(DOMAIN, sn)},
        connections=connections,
        name=entry.data.get(CONF_NAME, "EBO"),
        manufacturer="Enabot",
        model=entry.data.get(CONF_MODEL, "EBO Air 2"),
        serial_number=sn,
    )
    if fw:
        info["sw_version"] = fw
    return info


class EboEntity(CoordinatorEntity[EboCoordinator]):
    """Common device info + per-node data access."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EboCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._node = entry.data[CONF_NODE]
        sn = str(entry.data.get(CONF_SN) or self._node)
        self._attr_unique_id = f"{sn}_{key}"
        self._attr_device_info = ebo_device_info(
            entry, (coordinator.data or {}).get(self._node))

    @property
    def _robot(self) -> dict:
        return (self.coordinator.data or {}).get(self._node, {})

    @property
    def _state(self) -> dict:
        return self._robot.get("state") or {}

    @property
    def available(self) -> bool:
        # Not just "we saw this robot once": if the add-on stops answering, the data we hold is
        # stale and the entities must go unavailable rather than showing a frozen value.
        return (self.coordinator.last_update_success
                and self._node in (self.coordinator.data or {}))
