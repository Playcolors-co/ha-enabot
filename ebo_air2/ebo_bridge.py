#!/usr/bin/env python3
"""
ebo_bridge.py — EBO Air 2 ⇆ Home Assistant bridge.

Establishes the robot control session (RTM login + RTC join, like the app), then:
  - publishes telemetry as Home Assistant entities via MQTT Discovery
  - receives commands from HA (speed, laser, movement) and forwards them to the robot
  - keeps a 10 Hz movement loop with a watchdog (dead-man's switch)

Autonomous: with EBO_EMAIL/EBO_PASSWORD it logs into the Enabot cloud, discovers the
robot, gets the Agora tokens and renews them by itself before expiry (~24h). No
emulator, no session.json. See docs/STATO.md.

Config via env:
  EBO_EMAIL, EBO_PASSWORD          the user's Enabot credentials (autonomous)
  EBO_REGION=GB                    account region
  EBO_ROBOT_ID                     optional: if the account has more than one robot
  EBO_MQTT_HOST, EBO_MQTT_PORT=1883, EBO_MQTT_USER, EBO_MQTT_PASS
  (fallback: EBO_SESSION=/app/session.json)
"""
from ebo_log import log          # MUST be first: silences the Agora SDK's stdout noise

import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt

import ebo_cloud

from agora.rtc.agora_service import AgoraService, AgoraServiceConfig
from agora.rtc.agora_base import (
    RTCConnConfig, ClientRoleType, ChannelProfileType, RtcConnectionPublishConfig,
)
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtm.rtm_client import create_rtm_client
from agora.rtm.rtm_base import (
    RtmConfig, PublishOptions, SubscribeOptions,
    RtmChannelType, RtmMessageType, IRtmEventHandler,
)

# The Agora SDK's capabilities callback crashes on a None value (benign, "Exception
# ignored"). Guard it so it doesn't spam a traceback on every connect.
try:
    from agora.rtc import rtc_connection as _rc
    _orig_caps = _rc.RTCConnection._on_capabilities_changed

    def _safe_caps(self, caps_list):
        try:
            return _orig_caps(self, caps_list)
        except TypeError:
            return
    _rc.RTCConnection._on_capabilities_changed = _safe_caps
except Exception:
    pass

# ---- protocol opcodes (see docs/PROTOCOLLO.md) ----
OP_HANDSHAKE = 101003
OP_HEARTBEAT = 101005
OP_GET_SETTINGS = 101027
OP_MOVE = 101007
OP_TELEMETRY = 101026
OP_SETTINGS = 101028
OP_INFO = 101004
OP_SET_SPEED = 103009
OP_LASER = 103051
# extra commands, reverse-engineered from the app's command builder (gb.b).
# See docs/COMANDI.md for the full catalog. Simple, well-formed payloads only.
OP_SAY = 103501         # text-to-speech: {"userId":..,"text":".."} — robot speaks
OP_SLEEP = 101047       # sleep/wake: {"isSleeping": bool} — no movement
OP_VOLUME = 102023      # {"playbackVolume": int, "isPlaybackMuted": bool}
OP_MOVE_MODE = 103011   # {"moveMode": int}
OP_SHOOT_MODE = 102035  # {"shootMode": int}  (photo/video)
OP_PLAY_MOTION = 103005  # {"cycleMode": int, "moveId": int} — preset motion (MOVES)
OP_PLAY_VOICE = 103007   # {"cycleMode": int, "voiceId": int}
OP_DOCK = 103043         # manual return-to-base / start charging: {"startUp": bool} (MOVES)
OP_PATROL = 103061       # start patrol: {"mode","trackTarget","routeId","voiceId"} (MOVES)
OP_GET_ROUTES = 104001   # ask the robot for the saved patrol routes
RESP_ROUTES = 104002     # robot's reply: {"status", "list":[{id, routeName, routeFile}]}
# patrol mode 0 = auto (no route, routeId -1); mode 1 = follow a saved route (needs routeId).
# trackTarget is hard-coded to 7 in the app for both. AI tracking (103049) stays raw-only
# (it's interactive: pick a subject {mode,trackTarget}) — see COMANDI.md.
PATROL_AUTO = "auto (no route)"

DISCOVERY_PREFIX = "homeassistant"
NODE = "ebo_air2"


class Bridge:
    def __init__(self, session, mqtt_conf, provider=None, robot_id=None):
        self.provider = provider        # callable -> fresh session dict (login/refresh)
        self.robot_id = robot_id
        self.s = session
        self.account = self.s["rtm_user"].rsplit("_", 1)[-1]
        self.sid = self.s.get("sid")
        self.telemetry = {}
        self.settings = {}
        self.info = {}
        self.rtc_state = None
        self.routes = []                 # [(routeName, id)] from the robot
        self.patrol_choice = PATROL_AUTO  # currently selected patrol route

        # current movement vector + watchdog
        self.vec = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "buttons": 0}
        self.vec_deadline = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()

        self.rtm = None
        self.rtc = None
        self.mqtt = None
        self.mqtt_conf = mqtt_conf
        self.video = None
        self.video_enabled = os.environ.get("EBO_VIDEO", "1") == "1"
        self.rtsp_port = int(os.environ.get("EBO_RTSP_PORT", "8554"))
        self.robot_uid = None            # the robot's RTC uid, learned on_user_joined
        # runtime camera switch: controls whether we re-publish the robot's video as RTSP.
        # (control needs RTC presence, but we only subscribe to the robot's video — which is
        # what puts it in video mode — when the user turns the camera switch on.)
        self.video_on = self.video_enabled
        self.host_ip = os.environ.get("EBO_HOST_IP", "")
        self._observers_registered = False
        self._video_lock = threading.Lock()   # serialize setup/subscribe (2 callers race)

    # ---------------- Agora ----------------

    def connect_agora(self):
        s = self.s

        class RtcObs(IRTCConnectionObserver):
            def on_connected(o, conn, info, reason):
                self.rtc_state = "connected"
                log("[RTC] connected")

            def on_disconnected(o, conn, info, reason):
                self.rtc_state = "disconnected"
                log("[RTC] disconnected")

            def on_connection_failure(o, conn, info, reason):
                self.rtc_state = "failed"
                log("[RTC] connection failed:", reason)

            def on_user_joined(o, conn, uid):
                self.robot_uid = str(uid)
                log("[RTC] robot present:", uid)
                if self.video_on and self.rtc:   # nudge a keyframe so video starts quickly
                    try:
                        self.rtc.send_intra_request(str(uid))
                    except Exception:
                        pass

        bridge = self

        class RtmH(IRtmEventHandler):
            def on_message_event(o, event):
                bridge._on_rtm(event)

            def on_login_result(o, req, err):
                log("[RTM] login result:", err)

        self.rtm = create_rtm_client(RtmConfig(
            app_id=s["app_id"], user_id=s["rtm_user"], use_string_user_id=1,
            presence_timeout=300, heartbeat_interval=5, event_handler=RtmH(),
        ))
        r, _ = self.rtm.login(s["rtm_token"])
        if r != 0:
            raise RuntimeError("RTM login failed: %s" % self.rtm.get_error_reason(r))
        self.rtm.subscribe(s["robot_rtm"],
                           SubscribeOptions(with_message=True, with_presence=True))
        log("[RTM] login and subscribe ok")

        svc = AgoraService()
        scfg = AgoraServiceConfig()
        scfg.appid = s["app_id"]
        # REQUIRED to receive/decode video — without this the frame observer gets 0 frames.
        if self.video_enabled:
            try:
                scfg.enable_video = 1
            except Exception:
                pass
        svc.initialize(scfg)
        # Decoded video path: auto-subscribe so the SDK DECODES the robot's H.265 to raw YUV
        # (this build decodes H.265 but its *encoded* observer segfaults). We re-encode the YUV
        # to H.264 for RTSP. auto_subscribe_video=1 is the stable config.
        ccfg = RTCConnConfig(
            auto_subscribe_audio=0,
            auto_subscribe_video=1 if self.video_enabled else 0,
            client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        pcfg = RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False)
        self.rtc = svc.create_rtc_connection(ccfg, pcfg)
        self.rtc.register_observer(RtcObs())
        self._observers_registered = False
        self.rtc.connect(s["rtc_token"], s["rtc_channel"], s["rtc_uid"])
        for _ in range(20):
            if self.rtc_state:
                break
            time.sleep(0.5)
        log("[RTC] state:", self.rtc_state)

        if self.video_enabled:
            self._setup_video_pipeline()
            if self.video_on:            # restore camera state across reconnects
                self._camera_feed(True)

    def _rtsp_url(self):
        host = self.host_ip or "<HOME-ASSISTANT-IP>"
        return "rtsp://%s:%d/ebo" % (host, self.rtsp_port)

    def _setup_video_pipeline(self):
        """Create the RTSP pipeline and register the DECODED (YUV) frame observer on the
        connection — the SDK decodes H.265, we get YUV, ffmpeg re-encodes to H.264."""
        with self._video_lock:
            if self._observers_registered:
                return
            try:
                import ebo_video
                if not self.video:
                    self.video = ebo_video.VideoPipeline(rtsp_port=self.rtsp_port)
                self.rtc.register_video_frame_observer(self.video)
                self._observers_registered = True
                log("[video] decoded (YUV) video observer registered")
            except Exception as e:
                log("[video] pipeline setup failed:", e)

    def _camera_feed(self, on):
        """Turn our RTSP feed on/off. The robot streams whenever we're present in RTC; this
        just controls whether we re-publish it as RTSP."""
        if not self.video:
            self._setup_video_pipeline()
        if not self.video:
            return
        if on:
            self.video.start_feed()
            if self.robot_uid:
                try:
                    self.rtc.send_intra_request(self.robot_uid)
                except Exception:
                    pass
            log("[video] ON — camera stream: %s" % self._rtsp_url())
            threading.Thread(target=self._video_diag, daemon=True).start()
        else:
            self.video.stop_feed()
            log("[video] OFF — camera stream stopped")

    def _video_diag(self):
        """Nudge keyframes and warn if no decoded frames arrive."""
        started = time.time()
        warned = False
        while not self.stop.is_set() and self.video and self.video.feeding:
            if self.video.frames == 0:
                if self.robot_uid:
                    try:
                        self.rtc.send_intra_request(self.robot_uid)
                    except Exception:
                        pass
                if not warned and time.time() - started > 20:
                    warned = True
                    log("[video] ⚠ still 0 decoded frames after 20s — the robot may not be "
                        "publishing, or the SDK isn't decoding. RTSP is up but empty.")
                self.stop.wait(1)
            else:
                self.stop.wait(8)

    def set_camera(self, on):
        self.video_on = on
        self._camera_feed(on)
        self._publish_camera_state()

    def _opts(self):
        return PublishOptions(
            channel_type=RtmChannelType.RTM_CHANNEL_TYPE_USER,
            message_type=RtmMessageType.RTM_MESSAGE_TYPE_BINARY,
        )

    def send(self, mid, data=None):
        msg = {"id": mid, "type": 0, "timestamp": time.time() * 1000}
        if self.sid:
            msg["sid"] = self.sid
        if data is not None:
            msg["data"] = data
        payload = json.dumps(msg, separators=(",", ":")).encode()
        r, _ = self.rtm.publish(self.s["robot_rtm"], payload, self._opts())
        if r != 0:
            log("[!] publish %s failed: %s" % (mid, self.rtm.get_error_reason(r)))

    def _on_rtm(self, event):
        try:
            raw = event.message
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            obj = json.loads(raw)
        except Exception:
            return
        mid = obj.get("id")
        data = obj.get("data", {})
        if obj.get("rsid"):
            self.sid = obj["rsid"]
        if mid == OP_TELEMETRY:
            self.telemetry = data
            self._publish_telemetry()
        elif mid == OP_SETTINGS:
            self.settings = data
            self._publish_settings()
        elif mid == OP_INFO:
            self.info = data
        elif mid == RESP_ROUTES:
            lst = data.get("list") or []
            self.routes = [(r.get("routeName") or ("route %s" % r.get("id")),
                            r.get("id")) for r in lst if r.get("id") is not None]
            log("[patrol] %d route(s) dal robot" % len(self.routes))
            self._publish_patrol_select()

    # ---------------- control loop ----------------

    def control_loop(self):
        """Heartbeat every 2 s; movement at 10 Hz only while there's an active vector."""
        last_beat = 0.0
        was_moving = False
        while not self.stop.is_set():
            now = time.time()
            if now - last_beat >= 2:
                self.send(OP_HEARTBEAT, {"state": 0})
                last_beat = now
            with self.lock:
                # watchdog: if the command expired, zero it (dead-man's switch)
                if self.vec_deadline and now > self.vec_deadline:
                    self.vec = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "buttons": 0}
                    self.vec_deadline = 0.0
                v = dict(self.vec)
                moving = any(v[k] for k in ("lx", "ly", "rx", "ry"))
            if moving:
                self.send(OP_MOVE, v)          # stream the vector at 10 Hz
                was_moving = True
            elif was_moving:
                self.send(OP_MOVE, v)          # one final zero = stop
                was_moving = False
            time.sleep(0.1)

    def set_move(self, lx=0, ly=0, rx=0, ry=0, hold=0.6):
        with self.lock:
            self.vec = {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "buttons": 0}
            self.vec_deadline = time.time() + hold if any((lx, ly, rx, ry)) else 0

    # ---------------- MQTT / Home Assistant ----------------

    def connect_mqtt(self):
        # paho-mqtt 2.x requires the callback API version; fall back for 1.x
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="ebo_air2_bridge")
        except (AttributeError, TypeError):
            c = mqtt.Client(client_id="ebo_air2_bridge")
        if self.mqtt_conf.get("user"):
            c.username_pw_set(self.mqtt_conf["user"], self.mqtt_conf["pass"])
        c.on_connect = self._on_mqtt_connect
        c.on_message = self._on_mqtt_message
        c.will_set("%s/status" % NODE, "offline", retain=True)
        # assign before connecting: on_connect fires from the loop thread and may run
        # before this method returns — self.mqtt must already be set for it.
        self.mqtt = c
        # the broker (core-mosquitto) may not be ready yet at boot: retry a bit
        for attempt in range(12):
            try:
                c.connect(self.mqtt_conf["host"], self.mqtt_conf["port"], 60)
                break
            except OSError as e:
                if attempt == 0:
                    log("[MQTT] broker not ready, retrying:", e)
                time.sleep(5)
        else:
            raise RuntimeError("MQTT broker unreachable at %s:%s" % (
                self.mqtt_conf["host"], self.mqtt_conf["port"]))
        c.loop_start()

    def _dev(self):
        return {
            "identifiers": [NODE],
            "name": "EBO Air 2",
            "manufacturer": "Enabot",
            "model": self.info.get("model", "EBO Air 2"),
            "sw_version": self.info.get("masterMcuVersion", ""),
        }

    def _disc(self, comp, oid, cfg):
        cfg["device"] = self._dev()
        cfg["unique_id"] = "%s_%s" % (NODE, oid)
        cfg["availability_topic"] = "%s/status" % NODE
        topic = "%s/%s/%s/%s/config" % (DISCOVERY_PREFIX, comp, NODE, oid)
        self.mqtt.publish(topic, json.dumps(cfg), retain=True)

    def _remove_entity(self, comp, oid):
        # publish an empty retained config to delete a previously-discovered entity
        topic = "%s/%s/%s/%s/config" % (DISCOVERY_PREFIX, comp, NODE, oid)
        self.mqtt.publish(topic, "", retain=True)

    def _publish_patrol_select(self):
        """(Re)publish the patrol-route select with the routes known so far."""
        if not self.mqtt:
            return
        options = [PATROL_AUTO] + [name for (name, _rid) in self.routes]
        self._disc("select", "patrol_route", {
            "name": "EBO patrol route",
            "command_topic": "%s/patrol/route/set" % NODE,
            "state_topic": "%s/patrol/route" % NODE,
            "options": options,
            "icon": "mdi:map-marker-path"})
        if self.patrol_choice not in options:
            self.patrol_choice = PATROL_AUTO
        self.mqtt.publish("%s/patrol/route" % NODE, self.patrol_choice, retain=True)

    def _start_patrol(self):
        """Start patrol on the selected route (or auto/no-route when PATROL_AUTO)."""
        if self.patrol_choice == PATROL_AUTO:
            data = {"mode": 0, "trackTarget": 7, "routeId": -1, "voiceId": ""}
        else:
            rid = dict(self.routes).get(self.patrol_choice, -1)
            if rid == -1:
                log("[patrol] route sconosciuta '%s' — chiedo la lista" % self.patrol_choice)
                self.send(OP_GET_ROUTES)
                return
            data = {"mode": 1, "trackTarget": 7, "routeId": rid, "voiceId": ""}
        self.send(OP_PATROL, data)
        log("[patrol] start '%s' -> %s" % (self.patrol_choice, data))

    def _on_mqtt_connect(self, c, u, flags, rc):
        self.mqtt = c            # ensure it's set even if connect_mqtt hasn't returned yet
        log("[MQTT] connected rc=%s" % rc)
        try:
            self._publish_discovery(c)
        except Exception as e:
            log("[MQTT] discovery error:", e)

    def _publish_discovery(self, c):
        c.publish("%s/status" % NODE, "online", retain=True)
        st = "%s/state" % NODE

        # clean up entities removed in v0.4.4 (patrol / AI tracking were not real
        # one-shot commands; they live on the raw ebo_air2/cmd channel now)
        self._remove_entity("button", "patrol")
        self._remove_entity("switch", "ai_track")

        self._disc("sensor", "battery", {
            "name": "EBO battery", "state_topic": st,
            "value_template": "{{ value_json.battery }}",
            "unit_of_measurement": "%", "device_class": "battery"})
        self._disc("sensor", "wifi", {
            "name": "EBO wifi", "state_topic": st,
            "value_template": "{{ value_json.wifi }}",
            "unit_of_measurement": "dBm", "device_class": "signal_strength",
            "entity_category": "diagnostic"})
        self._disc("binary_sensor", "charging", {
            "name": "EBO charging", "state_topic": st,
            "value_template": "{{ value_json.charging }}",
            "payload_on": "true", "payload_off": "false", "device_class": "battery_charging"})
        self._disc("binary_sensor", "recording", {
            "name": "EBO recording", "state_topic": st,
            "value_template": "{{ value_json.recording }}",
            "payload_on": "true", "payload_off": "false"})

        self._disc("switch", "laser", {
            "name": "EBO laser", "state_topic": st,
            "value_template": "{{ value_json.laser }}",
            "command_topic": "%s/laser/set" % NODE,
            "payload_on": "on", "payload_off": "off",
            "state_on": "true", "state_off": "false"})
        self._disc("number", "speed", {
            "name": "EBO speed", "state_topic": st,
            "value_template": "{{ value_json.speed }}",
            "command_topic": "%s/speed/set" % NODE,
            "min": 1, "max": 100, "step": 1})

        # movement: 4 buttons (also handy for an AI agent via MQTT)
        for direction, label in [("forward", "forward"), ("back", "back"),
                                 ("left", "left"), ("right", "right"),
                                 ("stop", "stop")]:
            self._disc("button", "move_%s" % direction, {
                "name": "EBO %s" % label,
                "command_topic": "%s/move/%s" % (NODE, direction)})

        # sleep/wake — no movement, safe to toggle (optimistic switch)
        self._disc("switch", "sleep", {
            "name": "EBO sleep", "command_topic": "%s/sleep/set" % NODE,
            "payload_on": "on", "payload_off": "off", "optimistic": True,
            "icon": "mdi:sleep"})
        # text-to-speech: type text, the robot says it (great for automations/AI)
        self._disc("text", "say", {
            "name": "EBO say", "command_topic": "%s/say" % NODE,
            "state_topic": "%s/say/state" % NODE, "icon": "mdi:bullhorn"})
        # playback volume
        self._disc("number", "volume", {
            "name": "EBO volume", "command_topic": "%s/volume/set" % NODE,
            "min": 0, "max": 100, "step": 1, "optimistic": True,
            "icon": "mdi:volume-high"})
        # return to base (only works when the robot is away from the dock / not charging)
        self._disc("button", "dock", {
            "name": "EBO return to base", "command_topic": "%s/dock" % NODE,
            "icon": "mdi:home-import-outline"})
        # camera on/off: only when ON does the bridge subscribe to the robot's video (i.e.
        # put it in video mode). The RTSP URL to use is published as a sensor.
        self._disc("switch", "camera", {
            "name": "EBO camera", "command_topic": "%s/camera/set" % NODE,
            "state_topic": "%s/camera/state" % NODE,
            "payload_on": "on", "payload_off": "off", "icon": "mdi:cctv"})
        self._disc("sensor", "camera_url", {
            "name": "EBO camera URL", "state_topic": "%s/camera/url" % NODE,
            "icon": "mdi:link-variant", "entity_category": "diagnostic"})
        # patrol: pick a route (auto = no route) and start it
        self._publish_patrol_select()
        self._disc("button", "patrol_start", {
            "name": "EBO start patrol", "command_topic": "%s/patrol/start" % NODE,
            "icon": "mdi:play-circle-outline"})

        c.subscribe("%s/laser/set" % NODE)
        c.subscribe("%s/speed/set" % NODE)
        c.subscribe("%s/move/+" % NODE)
        # canale generico per un agente: JSON {"ly":-50,"rx":0,"hold":1.0}
        c.subscribe("%s/move/vector" % NODE)
        c.subscribe("%s/sleep/set" % NODE)
        c.subscribe("%s/say" % NODE)
        c.subscribe("%s/volume/set" % NODE)
        c.subscribe("%s/dock" % NODE)
        c.subscribe("%s/patrol/route/set" % NODE)
        c.subscribe("%s/patrol/start" % NODE)
        c.subscribe("%s/camera/set" % NODE)
        self._publish_camera_state()
        # RAW escape hatch for an AI/automation: publish {"id":<opcode>,"data":{...}}
        # to ebo_air2/cmd to send ANY command from the full catalog (docs/COMANDI.md).
        c.subscribe("%s/cmd" % NODE)

    def _on_mqtt_message(self, c, u, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace").strip()
        try:
            if topic.endswith("/laser/set"):
                self.send(OP_LASER, {"laser": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/speed/set"):
                self.send(OP_SET_SPEED, {"moveSpeed": int(float(payload))})
            elif topic.endswith("/move/vector"):
                v = json.loads(payload)
                self.set_move(v.get("lx", 0), v.get("ly", 0), v.get("rx", 0),
                              v.get("ry", 0), v.get("hold", 0.6))
            elif topic.endswith("/sleep/set"):
                self.send(OP_SLEEP, {"isSleeping": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/say"):
                if payload:
                    self.send(OP_SAY, {"userId": self.account, "text": payload})
                    self.mqtt.publish("%s/say/state" % NODE, payload)
            elif topic.endswith("/volume/set"):
                self.send(OP_VOLUME, {"playbackVolume": int(float(payload)),
                                      "isPlaybackMuted": False})
            elif topic.endswith("/dock"):
                # start returning to the charging base (no-op if already charging)
                self.send(OP_DOCK, {"startUp": True})
            elif topic.endswith("/patrol/route/set"):
                self.patrol_choice = payload
                self.mqtt.publish("%s/patrol/route" % NODE, payload, retain=True)
            elif topic.endswith("/patrol/start"):
                self._start_patrol()
            elif topic.endswith("/camera/set"):
                self.set_camera(payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/cmd"):
                # raw command from an AI/automation: {"id":<opcode>,"data":{...}}
                obj = json.loads(payload)
                mid = int(obj["id"])
                self.send(mid, obj.get("data"))
                log("[MQTT] raw cmd id=%s sent" % mid)
            elif "/move/" in topic:
                d = topic.rsplit("/", 1)[-1]
                mag = 60
                mapping = {
                    "forward": (0, -mag, 0), "back": (0, mag, 0),
                    "left": (0, 0, -mag), "right": (0, 0, mag), "stop": (0, 0, 0),
                }
                if d in mapping:
                    lx, ly, rx = mapping[d]
                    self.set_move(lx, ly, rx, hold=0.8)
        except Exception as e:
            log("[MQTT] command error %s: %s" % (topic, e))

    def _publish_telemetry(self):
        if not self.mqtt:        # telemetry can arrive before MQTT is up
            return
        t = self.telemetry
        b = t.get("battery", {})
        stt = t.get("status", {})
        payload = {
            "battery": b.get("percentage"),
            "charging": "true" if b.get("chargeStatus") else "false",
            "wifi": t.get("wifiStrength"),
            "recording": "true" if stt.get("isVideoRecording") else "false",
            "laser": "true" if stt.get("laserStatus") else "false",
            "speed": self.settings.get("moveSpeed"),
        }
        self.mqtt.publish("%s/state" % NODE, json.dumps(payload), retain=True)

    def _publish_settings(self):
        # merges moveSpeed into the state
        self._publish_telemetry()

    def _publish_camera_state(self):
        if not self.mqtt:
            return
        self.mqtt.publish("%s/camera/state" % NODE, "on" if self.video_on else "off",
                          retain=True)
        self.mqtt.publish("%s/camera/url" % NODE,
                          self._rtsp_url() if self.video_on else "off", retain=True)

    # ---------------- avvio ----------------

    def _token_age_ok(self):
        # RTC expires ~24h: renew with margin (every 20h)
        return (time.time() - self.s.get("captured_at", 0)) < 20 * 3600

    def refresh_session(self):
        if not self.provider:
            return
        try:
            fresh = self.provider()
            if fresh:
                self.s = fresh
                log("[*] Agora session renewed (auto)")
        except Exception as e:
            log("[!] session refresh failed:", e)

    def _install_signals(self):
        # The Supervisor stops the add-on with SIGTERM: shut down cleanly and promptly
        # (otherwise the container gets force-killed and HA shows an "error").
        import signal

        def _sig(signum, _frame):
            log("[*] signal %s received, shutting down" % signum)
            self.stop.set()
        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, _sig)
            except Exception:
                pass

    def _teardown(self):
        try:
            if self.mqtt:
                self.mqtt.publish("%s/status" % NODE, "offline", retain=True)
        except Exception:
            pass
        try:
            if self.video:
                self.video.stop()
        except Exception:
            pass
        try:
            if self.rtc:
                self.rtc.disconnect()
        except Exception:
            pass
        try:
            if self.rtm:
                self.rtm.logout()
        except Exception:
            pass

    def run(self):
        self._install_signals()
        self.connect_mqtt()       # MQTT first so telemetry has somewhere to go
        self.connect_agora()
        threading.Thread(target=self.control_loop, daemon=True).start()
        self.send(OP_HANDSHAKE, {"userId": self.account})
        time.sleep(1)
        self.send(OP_GET_SETTINGS)
        self.send(OP_GET_ROUTES)          # populate the patrol-route select
        log("[*] bridge running")
        last_check = time.time()
        try:
            # short, interruptible wait so a stop signal is honoured within ~1 s
            while not self.stop.wait(1):
                if time.time() - last_check < 30:
                    continue
                last_check = time.time()
                if self.provider and not self._token_age_ok():
                    self.refresh_session()
                    # reconnect Agora with the new tokens
                    try:
                        self.rtc.disconnect()
                    except Exception:
                        pass
                    self.connect_agora()
                    self.send(OP_HANDSHAKE, {"userId": self.account})
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            self._teardown()
            log("[*] bridge stopped")


def _make_provider():
    """If EBO_EMAIL/EBO_PASSWORD are set, the provider logs in and discovers the robot,
    renewing the session on each call. Returns (provider, robot_id, first_session)."""
    email = os.environ.get("EBO_EMAIL")
    password = os.environ.get("EBO_PASSWORD")
    if not (email and password):
        return None, None, None
    region = os.environ.get("EBO_REGION", "GB")
    host = os.environ.get("EBO_HOST", "ebox-eu.enabotserverintl.com")
    app_id = os.environ.get("EBO_APP_ID", "941ef1b4f14743fc8fdcf96b9331ca01")
    want_robot = os.environ.get("EBO_ROBOT_ID")

    def provider():
        c = ebo_cloud.EboCloud(host=host)
        r = c.login(email, password, region=region)
        if r.get("code") != 200:
            raise RuntimeError("login failed: %s" % r.get("msg"))
        robots = c.robots().get("data", {}).get("list", [])
        if not robots:
            raise RuntimeError("no robot on the account")
        rid = int(want_robot) if want_robot else robots[0]["robot_info"]["robot_id"]
        return ebo_cloud.build_bridge_session_from(c, rid, app_id)

    first = provider()
    rid = int(want_robot) if want_robot else None
    return provider, rid, first


def main():
    provider, robot_id, session = _make_provider()
    if session is None:
        sess_path = os.environ.get("EBO_SESSION", os.path.join(
            os.path.dirname(__file__), "session.json"))
        with open(sess_path) as f:
            session = json.load(f)
    mqtt_conf = {
        "host": os.environ.get("EBO_MQTT_HOST", "127.0.0.1"),
        "port": int(os.environ.get("EBO_MQTT_PORT", "1883")),
        "user": os.environ.get("EBO_MQTT_USER", ""),
        "pass": os.environ.get("EBO_MQTT_PASS", ""),
    }
    Bridge(session, mqtt_conf, provider=provider, robot_id=robot_id).run()
    return 0


if __name__ == "__main__":
    rc = main()
    # The Agora SDK spins native threads that can keep the process alive after a clean
    # shutdown; flush and hard-exit so the container actually stops (no force-kill).
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(rc or 0)
