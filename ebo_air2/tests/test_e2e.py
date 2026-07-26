"""End-to-end tests.

- Mocked E2E: MQTT command -> handler -> REAL send() -> exact JSON on the RTM wire,
  and RTM telemetry message -> parsed -> HA state on MQTT. Full pipeline, no network.
- Live smoke (opt-in, EBO_LIVE_TEST=1): logs into the real Enabot cloud and reads the
  robot list + firmware versions. READ-ONLY — never sends a command to the robot.
"""
import json
import os
import subprocess
import sys

import pytest

from conftest import deliver

HERE = os.path.dirname(__file__)
ADDON = os.path.dirname(HERE)
N = "ebo_air2"


class _Event:
    def __init__(self, obj):
        self.message = json.dumps(obj).encode()


def test_e2e_command_reaches_rtm_wire(bridge_rtm):
    deliver(bridge_rtm, "%s/laser/set" % N, "on")
    peer, msg = bridge_rtm.rtm.sent[-1]
    assert peer == "robot_rtm_1609"
    assert msg["id"] == 103051
    assert msg["type"] == 0
    assert msg["sid"] == "SID123"
    assert msg["data"] == {"laser": True}
    assert "timestamp" in msg


def test_e2e_rotate_and_select_on_wire(bridge_rtm):
    deliver(bridge_rtm, "%s/rotate/set" % N, "90")
    deliver(bridge_rtm, "%s/video_quality/set" % N, "High")
    ids = [(m["id"], m.get("data")) for _, m in bridge_rtm.rtm.sent]
    assert (103001, {"angle": 90}) in ids
    assert (102055, {"videoQuality": 3}) in ids


def test_e2e_disconnected_sends_nothing(bridge_rtm):
    bridge_rtm.connected = False           # master "connected" switch OFF
    deliver(bridge_rtm, "%s/laser/set" % N, "on")
    assert bridge_rtm.rtm.sent == []       # guarded: nothing goes out


def test_e2e_telemetry_message_to_ha_state(bridge_rtm):
    bridge_rtm._on_rtm(_Event({"id": 101026, "data": {
        "battery": {"percentage": 42, "chargeStatus": 0, "adapterStatus": -1},
        "status": {"isVideoRecording": False},
        "wifiStrength": -61,
    }}))
    s = bridge_rtm.mqtt.last_state()
    assert s["battery"] == 42
    assert s["charging"] == "false"
    assert s["wifi"] == -61
    assert s["docked"] == "false"


def test_e2e_rsid_captured_from_robot(bridge_rtm):
    bridge_rtm._on_rtm(_Event({"id": 101006, "rsid": "NEWSID", "data": {}}))
    assert bridge_rtm.sid == "NEWSID"


def test_e2e_routes_populate_patrol_select(bridge_rtm):
    bridge_rtm._on_rtm(_Event({"id": 104002, "data": {"status": 0, "list": [
        {"id": 5, "routeName": "Kitchen"}, {"id": 6, "routeName": "Lounge"}]}}))
    names = [n for (n, _rid) in bridge_rtm.routes]
    assert names == ["Kitchen", "Lounge"]


# ---------------- live smoke (opt-in, read-only) ----------------
_LIVE = os.environ.get("EBO_LIVE_TEST") == "1" and os.environ.get("EBO_PASSWORD")

_LIVE_SCRIPT = r"""
import os, sys, json
sys.path.insert(0, os.environ["EBO_ADDON"])
import ebo_cloud
c = ebo_cloud.EboCloud(host=os.environ.get("EBO_HOST", "ebox-eu.enabotserverintl.com"))
c.login(os.environ["EBO_EMAIL"], os.environ["EBO_PASSWORD"], region=os.environ.get("EBO_REGION", "GB"))
assert c.sessionid, "no sessionid"
r = c.robots()
lst = r["data"]["list"]
assert lst, "no robots"
info = lst[0]["robot_info"]
sn, mv = info["machine_code"], info["machine_version"]
import urllib.parse
q = urllib.parse.urlencode({"machine_version": mv, "machine_code": sn})
v = c._req("GET", "/api/v1/ebox/firmwares/versions", query=q)
assert v["code"] == 200 and "ipc" in v["data"], v
print("OK robots=%d ipc=%s" % (len(lst), v["data"]["ipc"]["firmware_version"]))
"""


@pytest.mark.skipif(not _LIVE, reason="set EBO_LIVE_TEST=1 + EBO_EMAIL/EBO_PASSWORD for the live smoke")
def test_e2e_live_cloud_readonly():
    env = dict(os.environ, EBO_ADDON=ADDON)
    out = subprocess.run([sys.executable, "-c", _LIVE_SCRIPT],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    assert "OK robots=" in out.stdout
