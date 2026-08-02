"""
Test harness for ebo_bridge.

ebo_bridge imports the Agora SDK (x86_64/glibc only) and paho-mqtt at module load.
We stub those (and ebo_log / ebo_cloud) so the pure logic — command routing, telemetry
parsing, discovery, signing — can be unit-tested on any platform, no robot, no network.
"""
import json
import os
import sys
import tempfile
import types

# The bridge persists write-only UI choices (eyes, image style) under /data. Point that at a temp
# dir so tests never touch a real add-on data directory.
os.environ.setdefault("EBO_DATA_DIR", tempfile.mkdtemp(prefix="ebo-test-"))

HERE = os.path.dirname(__file__)
ADDON = os.path.dirname(HERE)
sys.path.insert(0, ADDON)


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def _stub_class(name):
    return type(name, (), {"__init__": lambda self, *a, **k: None})


# ---- ebo_log: no-op logging ----
elog = _mod("ebo_log")
elog.log = lambda *a, **k: None
elog.raw = lambda *a, **k: None

# ---- paho.mqtt.client ----
# ---- segno: only used by panel.py to draw the pairing QR ----
segno = _mod("segno")
segno.make = lambda *a, **k: types.SimpleNamespace(save=lambda *a, **k: None)

paho = _mod("paho")
pmq = _mod("paho.mqtt")
pmc = _mod("paho.mqtt.client")
paho.mqtt = pmq
pmq.client = pmc
pmc.Client = _stub_class("Client")
pmc.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1)

# ---- ebo_cloud (not used in the tested code paths; live E2E uses the real one via subprocess) ----
_mod("ebo_cloud")

# ---- agora.* SDK stubs ----
_mod("agora")
_mod("agora.rtc")
_mod("agora.rtm")
svc = _mod("agora.rtc.agora_service")
svc.AgoraService = _stub_class("AgoraService")
svc.AgoraServiceConfig = _stub_class("AgoraServiceConfig")
base = _mod("agora.rtc.agora_base")
for n in ("RTCConnConfig", "ClientRoleType", "ChannelProfileType",
          "RtcConnectionPublishConfig", "AudioSubscriptionOptions"):
    setattr(base, n, _stub_class(n))
obs = _mod("agora.rtc.rtc_connection_observer")
obs.IRTCConnectionObserver = type("IRTCConnectionObserver", (), {})
rc = _mod("agora.rtc.rtc_connection")
rc.RTCConnection = type("RTCConnection", (), {"_on_capabilities_changed": lambda self, c: None})
afo = _mod("agora.rtc.audio_frame_observer")
afo.IAudioFrameObserver = type("IAudioFrameObserver", (), {})
rtmc = _mod("agora.rtm.rtm_client")
rtmc.create_rtm_client = lambda *a, **k: None
rtmb = _mod("agora.rtm.rtm_base")
for n in ("RtmConfig", "PublishOptions", "SubscribeOptions",
          "RtmChannelType", "RtmMessageType"):
    setattr(rtmb, n, _stub_class(n))
rtmb.IRtmEventHandler = type("IRtmEventHandler", (), {})

import pytest  # noqa: E402
import ebo_bridge as B  # noqa: E402


class FakeMqtt:
    """Records publish()/subscribe() calls made by the bridge."""

    def __init__(self):
        self.published = []   # list of (topic, payload, retain)
        self.subscribed = []  # list of topics

    def publish(self, topic, payload=None, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscribed.append(topic)

    # discovery config JSON published for a component/object id, or None
    def disc_configs(self):
        out = {}
        for topic, payload, _ in self.published:
            if topic.startswith("homeassistant/") and topic.endswith("/config") and payload:
                try:
                    out[topic] = json.loads(payload)
                except Exception:
                    pass
        return out

    def last_state(self, node="ebo"):
        for topic, payload, _ in reversed(self.published):
            if topic == "%s/state" % node:
                return json.loads(payload)
        return None


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


class FakeRtm:
    """Records publish() so we can assert the exact JSON sent to the robot over RTM."""

    def __init__(self):
        self.sent = []  # list of (peer, dict)

    def publish(self, peer, payload, opts):
        self.sent.append((peer, json.loads(payload.decode())))
        return 0, None

    def get_error_reason(self, r):
        return "err%s" % r


def _session():
    return {
        "rtm_user": "fz_us_ebox-prod_1609", "sid": "SID123", "app_id": "APPID",
        "rtm_token": "rt", "rtc_token": "ct", "rtc_channel": "chan", "rtc_uid": 1,
        "robot_rtm": "robot_rtm_1609", "captured_at": 0,
    }


@pytest.fixture
def B_mod():
    return B


@pytest.fixture
def bridge():
    """A Bridge with mqtt mocked and send() recorded (functional/routing tests)."""
    br = B.Bridge(_session(), {"host": "h", "port": 1883})
    br.mqtt = FakeMqtt()
    br.connected = True
    br.sent = []
    br.send = lambda mid, data=None: br.sent.append((mid, data))
    return br


@pytest.fixture
def bridge_rtm():
    """A Bridge wired to a fake RTM so the REAL send() path is exercised (E2E)."""
    br = B.Bridge(_session(), {"host": "h", "port": 1883})
    br.mqtt = FakeMqtt()
    br.connected = True
    br.rtm = FakeRtm()
    br._opts = lambda: None
    return br


def _pump(br):
    """The real send() only ENQUEUES — a dedicated sender thread does the publishing, and that
    thread isn't running under test. Drain the queue by hand so the E2E assertions see the wire."""
    mv = None
    if hasattr(br, "_move_lock"):
        with br._move_lock:
            if getattr(br, "_latest_move", None) is not None:
                mv, br._latest_move = br._latest_move, None
    if mv is not None:
        br._publish_now(B.OP_MOVE, mv)
    while True:
        try:
            mid, payload = br._send_q.get_nowait()
        except Exception:
            break
        br._publish_now(mid, payload)


def deliver(br, topic, payload):
    """Feed one MQTT command message into the bridge's handler."""
    br._on_mqtt_message(None, None, FakeMsg(topic, payload))
    if getattr(br, "rtm", None) is not None and not isinstance(getattr(br, "sent", None), list):
        _pump(br)          # E2E fixture (real send path) — flush what was queued
