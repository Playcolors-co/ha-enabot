"""
EBO MCP server (in-add-on) — exposes the robot to MCP-capable AI agents with vision in the loop.

Runs INSIDE the add-on (so it reuses the single Agora control session the bridge holds — it just
proxies the local data API at 127.0.0.1:8098). Opt-in: only started when the `mcp` option is on.
Transport = streamable HTTP on EBO_MCP_PORT (default 8100), guarded by the add-on's api_token as a
Bearer token — so a client must present `Authorization: Bearer <api_token>` to drive the robot.

The point vs Home Assistant's generic MCP is the **look → decide → move** loop: ebo_look returns the
live camera image, ebo_move drives, and move REFUSES without a recent look (no blind driving).
"""

from __future__ import annotations

import json
import math
import os
import time

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

API = os.environ.get(
    "EBO_MCP_API", "http://127.0.0.1:%s" % os.environ.get("EBO_API_PORT", "8098")
).rstrip("/")
TOKEN = os.environ.get("EBO_API_TOKEN", "")
PORT = int(os.environ.get("EBO_MCP_PORT", "8100"))
HEADERS = {"X-Enabot-Token": TOKEN}

MAX_SPEED = int(os.environ.get("EBO_MAX_SPEED", "45"))
MAX_SECONDS = float(os.environ.get("EBO_MAX_SECONDS", "2.0"))
LOOK_TTL = 8.0

# Bearer-token auth: the client must present the add-on's api_token. Without a token we refuse to
# start (an unauthenticated robot-driving endpoint on the LAN would be unsafe).
_auth = None
if TOKEN:
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    _auth = StaticTokenVerifier(tokens={TOKEN: {"client_id": "ebo-mcp", "scopes": []}})

mcp = FastMCP("ebo", auth=_auth)

_last_look: dict[str, float] = {}

_DIRS = {
    "forward": (-1, 0), "back": (1, 0), "left": (0, -1), "right": (0, 1),
    "forward_left": (-1, -1), "forward_right": (-1, 1),
    "back_left": (1, -1), "back_right": (1, 1),
}


async def _get(path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=12) as c:
        return await c.get(API + path, headers=HEADERS, **kw)


async def _cmd(node: str, suffix: str, payload: str = "") -> int:
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.post(API + "/api/cmd", headers=HEADERS,
                         json={"node": node, "suffix": suffix, "payload": str(payload)})
        return r.status_code


async def _robots() -> list[dict]:
    r = await _get("/api/robots")
    r.raise_for_status()
    return r.json()


def _node(robots: list[dict], node: str) -> str:
    return node or (robots[0]["node"] if robots else "ebo")


@mcp.tool()
async def ebo_list() -> str:
    """List the EBO robots and their key state (battery, charging, docked, driving mode, obstacle
    avoidance, night vision). Call this first to learn the node name for the other tools."""
    robots = await _robots()
    if not robots:
        return "No robots found."
    return "\n".join(
        f"- node={r.get('node')} name={r.get('name')} online={r.get('online')} "
        f"battery={(r.get('state') or {}).get('battery')}% "
        f"charging={(r.get('state') or {}).get('charging')} "
        f"docked={(r.get('state') or {}).get('docked')} "
        f"driving_mode={(r.get('state') or {}).get('move_mode')} "
        f"obstacle_avoid={(r.get('state') or {}).get('avoid_obstacle')} "
        f"night_vision={(r.get('state') or {}).get('night_vision')} camera={r.get('camera')}"
        for r in robots
    )


@mcp.tool()
async def ebo_state(node: str = "") -> dict:
    """Full live state of one robot. node from ebo_list(); empty = the first robot."""
    robots = await _robots()
    node = _node(robots, node)
    for r in robots:
        if r.get("node") == node:
            return {"node": node, "online": r.get("online"), "camera": r.get("camera"),
                    **(r.get("state") or {})}
    return {"error": f"robot '{node}' not found"}


@mcp.tool()
async def ebo_look(node: str = "") -> Image:
    """SEE what the robot sees: a fresh live snapshot (JPEG). ALWAYS call this right before ebo_move
    to check the path is clear. If the image is black, call ebo_wake and retry after ~2 s."""
    robots = await _robots()
    node = _node(robots, node)
    r = await _get("/api/snapshot", params={"node": node})
    if r.status_code != 200 or not r.content:
        raise RuntimeError("no snapshot (robot asleep? call ebo_wake) — HTTP %s" % r.status_code)
    _last_look[node] = time.time()
    return Image(data=r.content, format="jpeg")


@mcp.tool()
async def ebo_wake(node: str = "") -> str:
    """Wake the robot and start its camera (before looking/driving). Wait ~2-3 s, then ebo_look."""
    robots = await _robots()
    node = _node(robots, node)
    await _cmd(node, "camera/set", "on")
    return f"waking '{node}' — wait ~2-3 s, then ebo_look."


@mcp.tool()
async def ebo_move(node: str = "", direction: str = "forward",
                   speed: int = 25, seconds: float = 1.0) -> str:
    """Drive a short step. direction ∈ {forward, back, left, right, forward_left, forward_right,
    back_left, back_right}. speed 1-100 (capped), seconds capped. SAFETY: you MUST have called
    ebo_look in the last few seconds — this refuses to move otherwise, to avoid driving blind."""
    robots = await _robots()
    node = _node(robots, node)
    if direction not in _DIRS:
        return "invalid direction; use one of: " + ", ".join(_DIRS)
    ago = time.time() - _last_look.get(node, 0)
    if ago > LOOK_TTL:
        return (f"refused: no fresh camera view (last look {ago:.0f}s ago). Call ebo_look, confirm "
                f"the path is clear, then move.")
    st = await ebo_state.fn(node) if hasattr(ebo_state, "fn") else {}
    if str(st.get("charging")) == "true" or str(st.get("docked")) == "true":
        return "refused: the robot is on the charging base."
    spd = max(1, min(int(speed), MAX_SPEED))
    secs = max(0.1, min(float(seconds), MAX_SECONDS))
    ly_u, rx_u = _DIRS[direction]
    mag = math.hypot(ly_u, rx_u) or 1.0
    ly = round(ly_u / mag * spd)
    rx = round(rx_u / mag * spd)
    await _cmd(node, "move/vector", json.dumps({"ly": ly, "rx": rx, "hold": secs, "buttons": 1}))
    return f"moving {direction} at speed {spd} for {secs:.1f}s. Re-look before the next move."


@mcp.tool()
async def ebo_stop(node: str = "") -> str:
    """Stop the robot immediately."""
    robots = await _robots()
    node = _node(robots, node)
    await _cmd(node, "move/vector", json.dumps({"ly": 0, "rx": 0, "hold": 0, "buttons": 0}))
    return "stopped."


@mcp.tool()
async def ebo_dock(node: str = "") -> str:
    """Send the robot back to its charging base (firmware homing; check ebo_state for docked)."""
    robots = await _robots()
    node = _node(robots, node)
    await _cmd(node, "dock", "")
    return "returning to base — check ebo_state for docked/charging."


@mcp.tool()
async def ebo_night_vision(node: str = "", mode: str = "Auto") -> str:
    """Set day/night vision: mode ∈ {Auto, Day, Night}."""
    robots = await _robots()
    node = _node(robots, node)
    if mode not in ("Auto", "Day", "Night"):
        return "mode must be Auto, Day or Night."
    await _cmd(node, "night_vision/set", mode)
    return f"night vision -> {mode}."


@mcp.tool()
async def ebo_laser(node: str = "", on: bool = True) -> str:
    """Turn the pointer laser on/off."""
    robots = await _robots()
    node = _node(robots, node)
    await _cmd(node, "laser/set", "on" if on else "off")
    return f"laser -> {'on' if on else 'off'}."


@mcp.tool()
async def ebo_listen(node: str = "", on: bool = True) -> str:
    """Open or close the robot's microphone. With it on you can hear the room through the robot
    (the audio is carried inside the camera stream)."""
    robots = await _robots()
    node = _node(robots, node)
    await _cmd(node, "listen/set", "on" if on else "off")
    return f"listen -> {'on' if on else 'off'}."


@mcp.tool()
async def ebo_say(node: str = "", text: str = "") -> str:
    """Make the robot speak (text-to-speech through its speaker)."""
    robots = await _robots()
    node = _node(robots, node)
    if not text.strip():
        return "nothing to say."
    await _cmd(node, "say", text)
    return f"saying: {text!r}"


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("[mcp] refusing to start without EBO_API_TOKEN (unauthenticated driving is unsafe)")
    print("[mcp] EBO MCP server on http://0.0.0.0:%d (bearer-token auth)" % PORT, flush=True)
    mcp.run(transport="http", host="0.0.0.0", port=PORT)  # nosec B104 - inside the add-on container; bearer-token auth is enforced above
