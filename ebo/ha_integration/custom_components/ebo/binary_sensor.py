"""Binary sensors: charging, docked, online."""

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
        EboBinary(c, entry, "docked", "Docked",
                  lambda r: str((r.get("state") or {}).get("docked")).lower() == "true",
                  icon="mdi:home-lightning-bolt", diag=True),
        # Always available by design: an "online" sensor that goes unavailable can't report offline.
        EboBinary(c, entry, "online", "Online", lambda r: bool(r.get("online")),
                  dclass=BinarySensorDeviceClass.CONNECTIVITY, diag=True, always=True),
    ])


class EboBinary(EboEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry, key, name, fn, dclass=None, diag=False,
                 icon=None, always=False):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._fn = fn
        self._attr_device_class = dclass
        self._attr_icon = icon
        self._always = always
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self._fn(self._robot)

    @property
    def available(self) -> bool:
        # Only 'online' opts out of the normal availability rule — the others must go unavailable
        # with the rest when the add-on stops answering, instead of showing a frozen value.
        return True if self._always else super().available
