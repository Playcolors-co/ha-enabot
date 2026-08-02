"""Sensors: battery, Wi-Fi signal, SSID."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    c = hass.data[DOMAIN][entry.entry_id]
    add([
        EboSensor(c, entry, "battery", "Battery", lambda s: s.get("battery"),
                  unit=PERCENTAGE, dclass=SensorDeviceClass.BATTERY,
                  sclass=SensorStateClass.MEASUREMENT),
        # dBm + signal-strength class: without them Home Assistant shows a bare "-59" and can't
        # graph it or keep long-term statistics.
        EboSensor(c, entry, "wifi", "Wi-Fi signal", lambda s: s.get("wifi"),
                  unit="dBm", dclass=SensorDeviceClass.SIGNAL_STRENGTH,
                  sclass=SensorStateClass.MEASUREMENT, diag=True),
        EboSensor(c, entry, "ssid", "Wi-Fi SSID", lambda s: s.get("ssid"), diag=True),
    ])


class EboSensor(EboEntity, SensorEntity):
    def __init__(self, coordinator, entry, key, name, fn, unit=None, dclass=None,
                 sclass=None, diag=False):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._fn = fn
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = dclass
        self._attr_state_class = sclass
        if diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self._fn(self._state)
