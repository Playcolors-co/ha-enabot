"""Binary sensors: charging, online."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
        EboBinary(c, entry, "charging", "Charging",
                  lambda r: str((r.get("state") or {}).get("charging")).lower() == "true",
                  dclass=BinarySensorDeviceClass.BATTERY_CHARGING),
        EboBinary(c, entry, "online", "Online", lambda r: bool(r.get("online")),
                  dclass=BinarySensorDeviceClass.CONNECTIVITY, diag=True),
    ])


class EboBinary(EboEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry, key, name, fn, dclass=None, diag=False):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._fn = fn
        self._attr_device_class = dclass
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self._fn(self._robot)

    @property
    def available(self) -> bool:
        # 'online' must stay available to report offline
        return True
