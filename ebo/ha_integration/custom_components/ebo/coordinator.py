"""Polls the add-on's data API and lets entities send commands (native mode, no MQTT)."""

from __future__ import annotations

import logging
from datetime import timedelta

import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


class EboCoordinator(DataUpdateCoordinator):
    """Fetch all robots' state from the add-on's token-guarded API."""

    def __init__(self, hass: HomeAssistant, api: str, token: str) -> None:
        super().__init__(hass, _LOGGER, name="enabot",
                         update_interval=timedelta(seconds=10))
        self._api = (api or "").rstrip("/")
        self._headers = {"X-Enabot-Token": token or ""}
        self._session = async_get_clientsession(hass)

    async def _async_update_data(self):
        if not self._api:
            return {}
        try:
            async with async_timeout.timeout(10):
                async with self._session.get(self._api + "/api/robots",
                                             headers=self._headers) as r:
                    r.raise_for_status()
                    data = await r.json()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed("add-on API unreachable: %s" % err) from err
        return {x.get("node"): x for x in data if x.get("node")}

    async def cmd(self, node: str, suffix: str, payload="") -> None:
        """Send a command to a robot via the add-on API."""
        try:
            async with async_timeout.timeout(10):
                await self._session.post(
                    self._api + "/api/cmd", headers=self._headers,
                    json={"node": node, "suffix": suffix, "payload": str(payload)})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("EBO command %s/%s failed: %s", node, suffix, err)
        await self.async_request_refresh()
