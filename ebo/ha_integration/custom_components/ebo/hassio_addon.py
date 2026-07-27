"""Thin async client for the Supervisor REST API — used to locate the EBO add-on and read the
data-API endpoint (internal hostname + token) so the integration can reach it, all without MQTT.

Only reachable on Home Assistant OS / Supervised (``is_hassio`` is True); we talk to the documented
Supervisor REST API at http://supervisor with the SUPERVISOR_TOKEN the core process is given.
"""

from __future__ import annotations

import os

import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ADDON_SLUG_SUFFIX, API_PORT, CONF_API_TOKEN


class AddonError(Exception):
    """A Supervisor API call failed (or the add-on isn't installed)."""


def _headers() -> dict[str, str]:
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


async def _get(hass: HomeAssistant, path: str) -> dict:
    session = async_get_clientsession(hass)
    try:
        async with async_timeout.timeout(30):
            async with session.get("http://supervisor" + path, headers=_headers()) as r:
                body = await r.json()
    except Exception as err:  # noqa: BLE001
        raise AddonError(f"Supervisor GET {path} failed: {err}") from err
    if body.get("result") != "ok":
        raise AddonError(body.get("message") or f"Supervisor GET {path} error")
    return body.get("data") or {}


async def async_find_slug(hass: HomeAssistant) -> str | None:
    """Return the slug of the installed EBO add-on, or None if it isn't installed."""
    data = await _get(hass, "/addons")
    for addon in data.get("addons", []):
        slug = addon.get("slug", "")
        if slug.endswith(ADDON_SLUG_SUFFIX):
            return slug
    return None


async def async_get_endpoint(hass: HomeAssistant) -> tuple[str, str] | None:
    """Find the add-on and return (api_base_url, token), or None if unavailable.

    The API base uses the add-on's internal Supervisor hostname (reachable from HA core regardless
    of LAN/VLAN firewalls); the token is the add-on option persisted by the add-on on first run.
    """
    slug = await async_find_slug(hass)
    if not slug:
        return None
    info = await _get(hass, f"/addons/{slug}/info")
    host = info.get("hostname")
    token = (info.get("options") or {}).get(CONF_API_TOKEN)
    if not host or not token:
        return None
    return (f"http://{host}:{API_PORT}", token)
