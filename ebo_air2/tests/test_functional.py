"""Functional tests: every MQTT command routes to the right opcode + payload,
and telemetry/settings/info map to the right HA state fields."""
import pytest
from conftest import deliver

N = "ebo_air2"


# ---- command routing: topic -> (opcode, exact data) ----
@pytest.mark.parametrize("topic,payload,opcode,data", [
    ("laser/set", "on", 103051, {"laser": True}),
    ("laser/set", "off", 103051, {"laser": False}),
    ("speed/set", "50", 103009, {"moveSpeed": 50}),
    ("sleep/set", "on", 101047, {"isSleeping": True}),
    ("volume/set", "40", 102023, {"playbackVolume": 40, "isPlaybackMuted": False}),
    ("talkback_volume/set", "30", 102031, {"talkbackVolume": 30}),
    ("sports_record/set", "on", 101049, {"sportsRecord": True}),
    ("call_rec/set", "on", 103071, {"callAutoRecording": 1}),
    ("call_rec/set", "off", 103071, {"callAutoRecording": 0}),
    ("upload_cloud/set", "on", 104099, {"videoUploadCloud": True}),
    ("dock", "PRESS", 103043, {"startUp": True}),
    ("rotate/set", "45", 103001, {"angle": 45}),
    ("rotate/set", "-90", 103001, {"angle": -90}),
    ("video_quality/set", "High", 102055, {"videoQuality": 3}),
    ("video_quality/set", "Low", 102055, {"videoQuality": 1}),
    ("image_style/set", "Vivid", 102057, {"imageStyle": 1}),
    ("shoot_mode/set", "Wide", 102035, {"shootMode": 1}),
    ("move_mode/set", "Mode 2", 103011, {"moveMode": 1}),
    ("eyes/set", "Clock", 104057, {"status": 0, "mode": 1}),
    ("roaming/set", "on", 101061, {"isRoamOn": True, "sensitivity": 5}),
    ("ai_track", "PRESS", 103049, {"mode": 0, "trackTarget": 7}),
    ("motion/set", "3", 103005, {"cycleMode": 0, "moveId": 3}),
    ("voice/set", "2", 103007, {"cycleMode": 0, "voiceId": 2}),
])
def test_command_routing(bridge, topic, payload, opcode, data):
    deliver(bridge, "%s/%s" % (N, topic), payload)
    assert (opcode, data) in bridge.sent, "topic %s -> %s" % (topic, bridge.sent)


def test_say(bridge):
    deliver(bridge, "%s/say" % N, "hello")
    assert (103501, {"userId": "1609", "text": "hello"}) in bridge.sent
    assert ("%s/say/state" % N, "hello", False) in bridge.mqtt.published


def test_ai_ask(bridge):
    deliver(bridge, "%s/ai_ask" % N, "what time is it")
    op, data = bridge.sent[-1]
    assert op == 103301 and data["question"] == "what time is it" and data["userId"] == "1609"


def test_raw_cmd(bridge):
    deliver(bridge, "%s/cmd" % N, '{"id": 103501, "data": {"userId": "x", "text": "hi"}}')
    assert (103501, {"userId": "x", "text": "hi"}) in bridge.sent


# ---- movement: buttons / joystick / vector set the drive vector, not send() ----
def test_move_buttons(bridge):
    deliver(bridge, "%s/move/forward" % N, "")
    assert bridge.vec["ly"] < 0 and bridge.vec["rx"] == 0
    deliver(bridge, "%s/move/back" % N, "")
    assert bridge.vec["ly"] > 0
    deliver(bridge, "%s/move/left" % N, "")
    assert bridge.vec["rx"] < 0
    deliver(bridge, "%s/move/right" % N, "")
    assert bridge.vec["rx"] > 0
    deliver(bridge, "%s/move/stop" % N, "")
    assert not any(bridge.vec[k] for k in ("lx", "ly", "rx", "ry"))


def test_joystick(bridge):
    deliver(bridge, "%s/joystick" % N, '{"x": 0.5, "y": 1.0}')
    # y=forward(+) -> ly negative; x=turn right(+) -> rx positive
    assert bridge.vec["ly"] == -100 and bridge.vec["rx"] == 50


def test_joystick_clamps(bridge):
    deliver(bridge, "%s/joystick" % N, '{"x": 9, "y": -9}')
    assert bridge.vec["ly"] == 100 and bridge.vec["rx"] == 100  # clamped to +/-1 -> +/-100


def test_move_vector(bridge):
    deliver(bridge, "%s/move/vector" % N, '{"ly": -30, "rx": 20}')
    assert bridge.vec["ly"] == -30 and bridge.vec["rx"] == 20


def test_patrol_auto(bridge):
    deliver(bridge, "%s/patrol/start" % N, "")
    op, data = bridge.sent[-1]
    assert op == 103061 and data["routeId"] == -1 and data["mode"] == 0


# ---- value maps ----
def test_value_maps_reverse(B_mod):
    assert B_mod._rev(B_mod.VIDEO_QUALITY_MAP, 3) == "High"
    assert B_mod._rev(B_mod.IMAGE_STYLE_MAP, 0) == "Standard"
    assert B_mod._rev(B_mod.MOVE_MODE_MAP, 99) is None


# ---- telemetry parsing (101026 + settings 101028 + info 101004) ----
def test_telemetry_mapping(bridge):
    bridge.telemetry = {
        "battery": {"percentage": 87, "chargeStatus": 2, "adapterStatus": 0},
        "status": {"isVideoRecording": True, "safeMode": 1},
        "sdcard": {"isPresent": True, "availableBytes": 20e9, "capacityBytes": 31.2e9},
        "storage": {"availableBytes": 5e9},
        "wifiStrength": -55,
        "tasks": [{"taskId": 6}],
    }
    bridge.settings = {"moveSpeed": 90, "talkbackVolume": 40, "videoQuality": 3,
                       "imageStyle": 1, "shootMode": 0, "moveMode": 1, "sportsRecord": True}
    bridge.info = {"ipcVersion": "v0.8.5-0", "masterMcuVersion": "v1.0.6-0",
                   "ip": "192.168.30.229", "wifiSsid": "XS4TBIOT"}
    bridge._publish_telemetry()
    s = bridge.mqtt.last_state()
    assert s["battery"] == 87
    assert s["charging"] == "true"
    assert s["wifi"] == -55
    assert s["recording"] == "true"
    assert s["sd_present"] == "true"
    assert s["sd_free"] == 20.0 and s["sd_total"] == 31.2
    assert s["storage_free"] == 5.0
    assert s["docked"] == "true"           # adapterStatus != -1
    assert s["safe_mode"] == "true"
    assert s["task"] == "AI tracking"      # taskId 6
    assert s["video_quality"] == "High"
    assert s["image_style"] == "Vivid"
    assert s["move_mode"] == "Mode 2"
    assert s["fw_ipc"] == "v0.8.5-0" and s["fw_mcu"] == "v1.0.6-0"
    assert s["ip"] == "192.168.30.229" and s["ssid"] == "XS4TBIOT"


def test_telemetry_undocked(bridge):
    bridge.telemetry = {"battery": {"percentage": 50, "adapterStatus": -1}, "status": {}}
    bridge._publish_telemetry()
    s = bridge.mqtt.last_state()
    assert s["docked"] == "false"


def test_task_labels(bridge):
    assert bridge._task_label([{"taskId": 7}], {}, {"adapterStatus": -1}) == "on a call"
    assert bridge._task_label([], {"safeMode": 1}, {"adapterStatus": -1}) == "guard mode"
    assert bridge._task_label([], {}, {"adapterStatus": 0, "percentage": 80}) == "charging"
    assert bridge._task_label([], {}, {"adapterStatus": -1}) == "idle"
