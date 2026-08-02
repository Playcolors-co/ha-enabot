"""Config flow for EBO — no MQTT.

On Home Assistant OS / Supervised the add-on announces itself (Supervisor discovery) and the
integration reaches the add-on's HTTP API directly to enumerate robots and create one device each.
Everything (account, keys) is configured in the add-on; here there's nothing to type.

Without Supervisor (HA Container / Core) the manual step takes the engine's RTSP + API URL + token.
"""

from __future__ import annotations

from typing import Any

import async_timeout
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:  # HA moved is_hassio to helpers.hassio (2025+); fall back for older cores.
    from homeassistant.helpers.hassio import is_hassio
except ImportError:  # pragma: no cover
    from homeassistant.components.hassio import is_hassio

from . import hassio_addon
from .const import (
    CONF_API,
    CONF_MAC,
    CONF_MODEL,
    CONF_NAME,
    CONF_NODE,
    CONF_RTSP,
    CONF_SN,
    CONF_TOKEN,
    DOMAIN,
)

_ROBOT_KEYS = (CONF_NODE, CONF_NAME, CONF_SN, CONF_MAC, CONF_MODEL, CONF_RTSP)


def _uid(data: dict[str, Any]) -> str:
    """Stable per-robot id: prefer serial, fall back to node."""
    return str(data.get(CONF_SN) or data.get(CONF_NODE) or "").strip()


async def _fetch_robots(hass, api: str, token: str) -> list[dict[str, Any]]:
    """Ask the add-on's data API for the current robots."""
    session = async_get_clientsession(hass)
    async with async_timeout.timeout(15):
        async with session.get(
            api.rstrip("/") + "/api/robots", headers={"X-Enabot-Token": token}
        ) as r:
            r.raise_for_status()
            return await r.json()


class EboConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EBO robots."""

    VERSION = 1

    def _entry_data(self, robot: dict[str, Any], api: str, token: str) -> dict[str, Any]:
        data = {k: robot.get(k) for k in _ROBOT_KEYS}
        data[CONF_RTSP] = robot.get(CONF_RTSP) or ""
        data[CONF_API] = api
        data[CONF_TOKEN] = token
        return data

    async def _spawn_new(self, api: str, token: str) -> int:
        """Start an import flow for every robot that isn't configured yet. Returns how many,
        or -1 if the add-on's API couldn't be reached (a different problem than 'nothing new')."""
        try:
            robots = await _fetch_robots(self.hass, api, token)
        except Exception:  # noqa: BLE001
            return -1
        known = self._async_current_ids()
        new = 0
        for robot in robots:
            if _uid(robot) and _uid(robot) not in known:
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": "import"},
                        data=self._entry_data(robot, api, token),
                    )
                )
                new += 1
        return new

    # --- user clicked "＋ Add integration → EBO" ------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not is_hassio(self.hass):
            return await self.async_step_manual()
        endpoint = await hassio_addon.async_get_endpoint(self.hass)
        if not endpoint:
            return self.async_abort(reason="addon_not_found")
        api, token = endpoint
        new = await self._spawn_new(api, token)
        if new < 0:
            return self.async_abort(reason="cannot_connect")
        if new == 0:
            return self.async_abort(reason="no_new_robots")
        return self.async_abort(reason="setup_started")

    # --- import (one entry per robot, spawned by discovery/user) --------------------

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        await self.async_set_unique_id(_uid(data) or data.get(CONF_NAME))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data.get(CONF_NAME) or "EBO", data=data)

    # --- manual (no Supervisor) -----------------------------------------------------

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(_uid(user_input) or user_input[CONF_NAME])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_NODE, default="ebo"): str,
                vol.Required(CONF_NAME, default="EBO"): str,
                vol.Required(CONF_RTSP): str,
                vol.Optional(CONF_API): str,
                vol.Optional(CONF_TOKEN): str,
                vol.Optional(CONF_SN): str,
                vol.Optional(CONF_MAC): str,
                vol.Optional(CONF_MODEL, default="EBO"): str,
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema)
