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
from ebo_log import log, raw     # MUST be first: silences the Agora SDK's stdout noise

import json
import os
import subprocess
import sys
import queue
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
# The robot's mic streams at 8 kHz mono (measured on the real app). Override if it ever differs.
AUDIO_RATE = int(os.environ.get("EBO_AUDIO_RATE", "8000"))
# Auto-standby: seconds of no commands after which we leave the Agora session so the robot can go
# to sleep (the ZZ eyes) like it does when you close the official app. 0 disables it (the add-on
# then keeps the robot awake for as long as it runs — the old behaviour).
STANDBY_TIMEOUT = int(os.environ.get("EBO_STANDBY_TIMEOUT", "300"))

OP_HANDSHAKE = 101003
OP_HEARTBEAT = 101005
OP_GET_SETTINGS = 101027
OP_MOVE = 101007
OP_TELEMETRY = 101026
OP_SETTINGS = 101028
OP_INFO = 101004
OP_SET_SPEED = 103009
OP_LASER = 103051
# extra commands, derived from the app's command set for interoperability.
# See docs/COMANDI.md for the full catalog. Simple, well-formed payloads only.
OP_SAY = 103501         # text-to-speech: {"userId":..,"text":".."} — robot speaks
OP_SLEEP = 101047       # sleep/wake: {"isSleeping": bool} — no movement
OP_VOLUME = 102023      # {"playbackVolume": int, "isPlaybackMuted": bool}
OP_SPORTS_REC = 101049  # motion recording: {"sportsRecord": bool}
OP_CALL_REC = 103071    # auto-record calls: {"callAutoRecording": int 0/1}
OP_UPLOAD_CLOUD = 104099  # upload recordings to cloud: {"videoUploadCloud": bool}
OP_TALKBACK_VOL = 102031  # {"talkbackVolume": int 0..100}
# THE missing piece for two-way audio. Subscribing to the robot's audio track is not enough: the
# robot only STARTS PUBLISHING its microphone when it is told to open that direction, with
# {"type":1,"open":1}. Verified live: sending 102001 made the mic come up within a second
# (bitrate ~73 kbps, 8 kHz mono), and open:0 stopped it (bitrate 0, loss 100). In the app these sit
# right next to the local mute calls: 102001 pairs with muteRemoteAudio(uid) → LISTEN, 102003 pairs
# with the local-mic mute → TALK.
OP_AUDIO_LISTEN = 102001  # {"type":1,"open":0|1} — robot mic -> us
OP_AUDIO_TALK = 102003    # {"type":1,"open":0|1} — us -> robot speaker
OP_MOVE_MODE = 103011   # {"moveMode": int}
OP_NIGHT_MODE = 102035  # {"shootMode": int} — the Air 2's day/night vision mode (0 Auto, 1 Day, 2 Night)
OP_SHOOT_MODE = OP_NIGHT_MODE  # legacy alias
OP_PLAY_MOTION = 103005  # {"cycleMode": int, "moveId": int} — preset motion (MOVES)
OP_PLAY_VOICE = 103007   # {"cycleMode": int, "voiceId": int}
OP_DOCK = 103043         # manual return-to-base / start charging: {"startUp": bool} (MOVES)
OP_PATROL = 103061       # start patrol: {"mode","trackTarget","routeId","voiceId"} (MOVES)
OP_PATROL_STOP = 103063  # stop the running patrol (no payload)
OP_GET_ROUTES = 104001   # ask the robot for the saved patrol routes
RESP_ROUTES = 104002     # robot's reply: {"status", "list":[{id, routeName, routeFile}]}
# Route recording = "teach-by-driving": start recording, DRIVE the robot (move commands trace the
# path), stop, then save with a name. Confirmed from the app (Air2LiveModel + RouteViewModel):
OP_ROUTE_REC_START = 103201   # start recording a route (no payload); robot acks with 103202
RESP_ROUTE_REC_ACK = 103202   # {"status": int} — recording started
RESP_ROUTE_PROGRESS = 103204  # RouteReportInfo streamed while recording (progress)
OP_ROUTE_REC_STOP = 103205    # stop recording; robot replies 103206 with the recorded route
RESP_ROUTE_DATA = 103206      # RouteDataInfo {status, routeFile, routeName, tempId, ...} to save
OP_ROUTE_SAVE = 104003        # save the recorded route (RouteDataInfo with a routeName)
OP_ROUTE_DELETE = 104005      # {"ids": [int, ...]} — delete saved routes

# --- extra controls mapped from the decompiled command builder (docs/COMMANDS-APK.md) ---
OP_ROTATE = 103001        # {"angle": int} — rotate the head/body by an angle
OP_VIDEO_QUALITY = 102055  # {"videoQuality": int}  3=High 2=Medium 1=Low
OP_IMAGE_STYLE = 102057   # {"imageStyle": int}  0/1/2
OP_PLAY_VOICE = 103007    # {"cycleMode": int, "voiceId": int}
OP_ROAM = 101061          # {"isRoamOn": bool, "sensitivity": int} — autonomous roaming
OP_AI_TRACK = 103049      # StartAiTrackData {"mode": int, "trackTarget": int}
OP_EYES = 104057          # EyesEmojiModeData {"status","mode",...}
OP_AI_ASK = 103301        # AI chat: {"modelType","session","question","userId"}
# Motion/Sport settings (the app's fullscreen "Sport settings"). The whole MotionSettings object is
# sent at once (103023); the current values are requested with 103021 (robot replies 103022, and
# echoes 103024 after a set). MotionSettings = {status, pickUpCheck, autoDesktopMode, avoidobstacle,
# steeringSensitivity (0..3), abnormalExerciseReminder}.
OP_MOTION_GET = 103021
OP_MOTION_SET = 103023
RESP_MOTION = 103022
RESP_MOTION_ECHO = 103024
# Obstacle avoidance ALSO has a dedicated single-field setter (103045, {"avoidobstacle": bool}) — the
# app's "Collision Avoidance Assist" toggle. Prefer this over the whole-MotionSettings write: it never
# clobbers the other bundle fields, and the value is echoed back in the normal settings report, so we
# can read it too. (The 103023 bundle stays only for steering/pickup/desktop/abnormal, which the robot
# doesn't report back — those we don't surface yet.)
OP_AVOID_OBSTACLE = 103045

# value tables (from the app's UI): name shown in HA -> integer sent to the robot
VIDEO_QUALITY_MAP = {"Low": 1, "Medium": 2, "High": 3}
IMAGE_STYLE_MAP = {"Standard": 0, "Vivid": 1, "Soft": 2}
# Day/night vision (the app's fullscreen day/night button = shootMode). Confirmed from the app's
# LiveDayNightLayout: 0 = Auto (autoIv), 1 = Day (dayIv), 2 = Night (nightIv). Echoed in the settings
# report, so we can read it back.
NIGHT_MODE_MAP = {"Auto": 0, "Day": 1, "Night": 2}
# Driving mode = the app's "Driving Mode" radio (Smooth Mode / Racing Mode). moveMode 0/1.
MOVE_MODE_MAP = {"Smooth": 0, "Racing": 1}
# steeringSensitivity has 4 levels (0..3) in the app; names are our own (the app's strings are obfuscated)
STEERING_MAP = {"Low": 0, "Medium": 1, "High": 2, "Max": 3}
# Eyes/emoji display (opcode 104057). Reconstructed from the Air 2 app: the payload is
# EyesEmojiModeData {status, mode, dynamicEyes{autoFollow,styleId}, timeEyes{styleId}, customEyes{timeStyle}}.
# mode 1=Dynamic (styleId 1..6), 2=Clock (styleId 1..2), 3=Custom. The style lists are hardcoded in
# the app. We expose a single flattened select; each option maps to (mode, styleId).
EYES_STYLES = {
    "Dynamic 1": (1, 1), "Dynamic 2": (1, 2), "Dynamic 3": (1, 3),
    "Dynamic 4": (1, 4), "Dynamic 5": (1, 5), "Dynamic 6": (1, 6),
    "Clock 1": (2, 1), "Clock 2": (2, 2),
    "Custom": (3, 1),
}


def _rev(m, v):
    """Reverse a value map: integer -> display name (or None)."""
    for k, iv in m.items():
        if iv == v:
            return k
    return None
# patrol mode 0 = auto (no route, routeId -1); mode 1 = follow a saved route (needs routeId).
# trackTarget is hard-coded to 7 in the app for both. AI tracking (103049) stays raw-only
# (it's interactive: pick a subject {mode,trackTarget}) — see COMANDI.md.
PATROL_AUTO = "auto (no route)"

DISCOVERY_PREFIX = "homeassistant"
# per-robot: one add-on can run a bridge per robot, each with its own MQTT prefix / camera
# path. Single robot keeps the classic "ebo" so existing entities are untouched.
NODE = os.environ.get("EBO_NODE", "ebo")


class Bridge:
    def __init__(self, session, mqtt_conf, provider=None, robot_id=None):
        self.provider = provider        # callable -> fresh session dict (login/refresh)
        self.robot_id = robot_id
        self.s = session
        self.account = self.s["rtm_user"].rsplit("_", 1)[-1]
        self.sid = self.s.get("sid")
        self.telemetry = {}
        self.settings = {}
        self.motion = {}          # current MotionSettings (obstacle avoidance, steering, etc.)
        self.info = {}
        self._integ_announced = False    # announced this robot to the companion integration?
        self.rtc_state = None
        self.routes = []                 # [(routeName, id)] from the robot
        self.patrol_choice = PATROL_AUTO  # currently selected patrol route
        self.listen_on = True            # robot mic -> us (102001); you can switch it off
        # The robot never reports these back, so a restart would leave the selects on "unknown".
        # We remember what we last set, on disk, and replay it into the published state.
        self._ui_path = os.path.join(os.environ.get("EBO_DATA_DIR", "/data"), "ui_choices.json")
        self._ui = self._ui_load()
        self.eyes_choice = self._ui.get("eyes")   # last eyes style we set (write-only on the robot)
        if self._ui.get("imageStyle") is not None:
            self.settings["imageStyle"] = self._ui["imageStyle"]
        self._last_activity = time.time()   # last user command (drives auto-standby)
        self._route_rec = False          # True while recording a route (teach-by-driving)
        self._route_pending = None       # RouteDataInfo from 103206, awaiting a name + save
        # Route/patrol support is model-dependent: the EBO Air 2 firmware ignores these opcodes (the
        # official app hides patrol for it). We probe with 104001 (get routes) on connect: a reply
        # (104002) means supported; silence past a timeout means unsupported → the panel hides the UI.
        self._routes_supported = None    # None=unknown, True/False once decided
        self._routes_query_ts = 0.0      # when we first asked for routes

        # current movement vector + watchdog
        self.vec = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "buttons": 0}
        self.vec_deadline = 0.0
        self.lock = threading.Lock()
        # ALL RTM sends go through one sender thread (see _sender_loop): callers only enqueue.
        # The Agora SDK is not thread-safe, and — crucially — a slow cloud send must never run on
        # the MQTT receive thread, or it blocks delivery of every following command.
        self._send_q = queue.Queue(maxsize=256)
        # Movement is COALESCED, not queued: only the latest vector matters, so a new move overwrites
        # any pending one instead of piling up behind slow cloud sends. It's also sent with priority
        # each sender pass, so steering stays responsive even when the RTM link is degrading.
        self._latest_move = None
        self._move_lock = threading.Lock()
        self._send_evt = threading.Event()
        self.stop = threading.Event()

        self.rtm = None
        self.rtc = None
        self.mqtt = None
        self.mqtt_conf = mqtt_conf
        self.video = None
        self.video_enabled = os.environ.get("EBO_VIDEO", "1") == "1"
        # expose HA entities over MQTT discovery (default on). Off = native integration owns them;
        # MQTT is still used for the panel's state/commands, just not for entity discovery.
        self.expose_mqtt = os.environ.get("EBO_EXPOSE_MQTT", "1") == "1"
        self.audio_enabled = os.environ.get("EBO_AUDIO", "0") == "1"   # listen (optional)
        self.talk_enabled = os.environ.get("EBO_TALK", "0") == "1"     # speak TO the robot
        self._talk_lock = threading.Lock()
        self._tx_run = False           # audio TX loop (keep-alive silence + talk) running?
        self._tx_queue = []            # queued 'talk' sources
        self._talk_stop = False        # set by talk/stop to end a live push-to-talk
        self._tx_mode = "silence"      # DIAG: idle TX content — "silence" | "tone"
        self._tx_start_t = 0.0         # DIAG: when we started publishing (to time mic-open)
        self._tone_buf = None          # DIAG: cached tone PCM (built lazily)
        self.tx_test = os.environ.get("EBO_AUDIO_TX_TEST", "off")  # off|silence|tone|auto
        self.rtsp_port = int(os.environ.get("EBO_RTSP_PORT", "8554"))
        self.rtsp_path = os.environ.get("EBO_RTSP_PATH", "ebo")
        self.robot_uid = None            # the robot's RTC uid, learned on_user_joined
        self.connected = True            # master session switch: off => robot can sleep
        # runtime camera switch: controls whether we re-publish the robot's video as RTSP.
        # (control needs RTC presence, but we only subscribe to the robot's video — which is
        # what puts it in video mode — when the user turns the camera switch on.)
        self.video_on = self.video_enabled
        self.host_ip = os.environ.get("EBO_HOST_IP", "")
        self._observers_registered = False
        self.svc = None                        # Agora service (global param handle lives here)
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
                # AUDIO: verified on the real app (Frida) — the "listen" icon just calls
                # muteRemoteAudioStream(robotUid, false), i.e. it SUBSCRIBES to the robot's audio
                # track. The robot publishes audio all along; auto_subscribe_audio didn't engage
                # for us, so subscribe explicitly here — the server-SDK equivalent of that button.
                if self.audio_enabled and self.rtc:
                    def _sub(tagnote):
                        try:
                            lu = self.rtc.get_local_user()
                            r1 = lu.subscribe_audio(str(uid))
                            r2 = lu.subscribe_all_audio()
                            log("[audio] %s subscribe_audio(%s) rc=%s / subscribe_all_audio rc=%s"
                                % (tagnote, uid, r1, r2))
                        except Exception as e:
                            log("[audio] subscribe failed:", e)
                    # Tell the robot to actually PUBLISH its mic (subscribing alone gets you a
                    # subscribed-but-silent track — this is what we were missing all along).
                    try:
                        self.send(OP_AUDIO_LISTEN,
                                  {"type": 1, "open": 1 if self.listen_on else 0})
                        log("[audio] asked the robot to %s its mic (102001)"
                            % ("open" if self.listen_on else "keep closed"))
                    except Exception as e:
                        log("[audio] could not open the robot mic:", e)
                    _sub("join")
                    # the robot's audio track may be published a moment after it joins — retry
                    # once after a short delay so we don't miss it (mirrors the app, where you
                    # tap "listen" well after the robot is already streaming).
                    def _retry():
                        time.sleep(2.5)
                        if self.audio_enabled and self.rtc:
                            _sub("retry")
                    threading.Thread(target=_retry, daemon=True).start()

        bridge = self

        class RtmH(IRtmEventHandler):
            def on_message_event(o, event):
                bridge._on_rtm(event)

            def on_login_result(o, req, err):
                log("[RTM] login result:", err)

        if self.rtm is None:      # reuse an existing RTM login (telemetry) across RTC reconnects
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
        else:
            log("[RTM] reusing existing login")

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
        self.svc = svc
        # AUDIO codec: the robot streams its mic with a custom telephony codec (Agora payload
        # type 8 = monitor / 9 = call). Agora's own guidance for this case (payload 8 = G.711)
        # is that che.audio.codec_unfallback + custom_payload_type must be set on the GLOBAL
        # engine parameter handle BEFORE joining — setting them on the per-connection handle
        # after connect() never takes effect (that's why the PCM observer got 0 frames). The
        # server SDK's global handle is service.get_agora_parameter(). Set them here, pre-join.
        if self.audio_enabled:
            try:
                # "auto" = don't force a payload type at all and let the SDK negotiate. Worth trying:
                # forcing the WRONG type is indistinguishable from "the mic is muted" (subscribed, but
                # nothing decodes). Payload types: 0 = G.711 u-law, 8 = G.711 A-law, 9 = G.722.
                pt_opt = (os.environ.get("EBO_AUDIO_PT", "8") or "8").strip().lower()
                gp = svc.get_agora_parameter()
                params = ['{"che.audio.codec_unfallback":[0,8,9]}', '{"che.audio.aec.enable":false}']
                if pt_opt not in ("auto", ""):
                    pt = int(pt_opt)
                    params.insert(1, '{"che.audio.custom_payload_type":%d}' % pt)
                else:
                    pt = "auto"
                for kv in params:
                    gp.set_parameters(kv)
                log("[audio] codec params set on ENGINE before join "
                    "(codec_unfallback [0,8,9], payload_type %s)" % pt)
            except Exception as e:
                log("[audio] global set_parameters failed:", e)
        # Decoded video path: auto-subscribe so the SDK DECODES the robot's H.265 to raw YUV
        # (this build decodes H.265 but its *encoded* observer segfaults). We re-encode the YUV
        # to H.264 for RTSP. auto_subscribe_video=1 is the stable config.
        ccfg_kw = dict(
            auto_subscribe_audio=1 if self.audio_enabled else 0,
            auto_subscribe_video=1 if self.video_enabled else 0,
            client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
            channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
        )
        if self.audio_enabled:
            # Mirror the WORKING video path: plain auto-subscribe + frame observer, no special
            # AudioSubscriptionOptions. The earlier pcm_data_only=1 put the subscription in a
            # raw-track-PCM mode that bypasses the playout observer (subscribed but 0 PCM).
            # REQUIRED: run the audio decode/playout pipeline so the frame observers fire.
            ccfg_kw["enable_audio_recording_or_playout"] = 1
        ccfg = RTCConnConfig(**ccfg_kw)
        # Enable the PCM publish capability when audio (listen) OR talk is on, so 'talk' can push
        # audio to the robot's speaker on demand. NOTE: we no longer auto-publish a silent track
        # for listen — tested (v0.17.1) that it does NOT make the robot open its mic, it only
        # echoes into the listen feed. The robot's mic opening is still gated behind an RTM
        # command the phone app sends that we haven't captured. The app uses audio scenario 3
        # (GAME_STREAMING) for the intercom; match it.
        if self.audio_enabled or self.talk_enabled:
            from agora.rtc.agora_base import AudioPublishType, AudioScenarioType
            pcfg = RtcConnectionPublishConfig(
                is_publish_audio=True, is_publish_video=False,
                audio_publish_type=AudioPublishType.AUDIO_PUBLISH_TYPE_PCM,
                audio_scenario=AudioScenarioType.AUDIO_SCENARIO_GAME_STREAMING)
        else:
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
        # Also set the codec on the CONNECTION handle after connect — the app sets
        # custom_payload_type on its engine *after* joinChannelEx, so cover that too (harmless
        # if the global pre-join set already took).
        if self.audio_enabled:
            try:
                pt_opt = (os.environ.get("EBO_AUDIO_PT", "8") or "8").strip().lower()
                cp = self.rtc.get_agora_parameter()
                cp.set_parameters('{"che.audio.codec_unfallback":[0,8,9]}')
                if pt_opt not in ("auto", ""):
                    cp.set_parameters('{"che.audio.custom_payload_type":%d}' % int(pt_opt))
                log("[audio] codec params also set on connection after connect (pt=%s)" % pt_opt)
            except Exception as e:
                log("[audio] connection set_parameters failed:", e)

        if self.video_enabled:
            self._setup_video_pipeline()
            if self.video_on:            # restore camera state across reconnects
                self._camera_feed(True)

    def _rtsp_url(self, host=None):
        # host=None -> LAN host IP (for the human-facing 'EBO camera URL' / Generic Camera).
        # For the native integration we pass the add-on's internal hostname, reachable by HA core
        # over the Supervisor network regardless of LAN/VLAN firewalls (same as the data API).
        host = host or self.host_ip or "<HOME-ASSISTANT-IP>"
        return "rtsp://%s:%d/%s" % (host, self.rtsp_port, self.rtsp_path)

    def _setup_video_pipeline(self):
        """Create the RTSP pipeline and register the DECODED (YUV) frame observer on the
        connection — the SDK decodes H.265, we get YUV, ffmpeg re-encodes to H.264."""
        with self._video_lock:
            if self._observers_registered:
                return
            try:
                import ebo_video
                if not self.video:
                    self.video = ebo_video.VideoPipeline(rtsp_port=self.rtsp_port,
                                                         path=self.rtsp_path)
                self.rtc.register_video_frame_observer(self.video)
                self._observers_registered = True
                log("[video] decoded (YUV) video observer registered")
                if self.audio_enabled:
                    self._register_audio_observer()
            except Exception as e:
                log("[video] pipeline setup failed:", e)

    def _register_audio_diag(self):
        """Register a local-user observer purely to diagnose the audio path: does the robot
        actually SEND audio bytes in monitor mode (received_bytes>0 in the stats) or not? This
        distinguishes 'robot isn't publishing mic audio here' from 'bytes arrive but the SDK
        can't decode the custom codec' — which decides whether audio-listen is even feasible."""
        try:
            from agora.rtc.local_user_observer import IRTCLocalUserObserver
        except Exception as e:
            log("[audio-diag] import failed:", e)
            return

        class LUObs(IRTCLocalUserObserver):
            _stat_n = [0]

            def on_user_audio_track_subscribed(o, lu, user_id, track):
                log("[audio-diag] subscribed to robot audio track uid=%s "
                    "(robot IS publishing audio)" % user_id)

            def on_audio_subscribe_state_changed(o, lu, channel, user_id, old, new, elapsed):
                # new: 0=idle 1=no-publisher 2=subscribing 3=subscribed. Tells us if the robot
                # is even publishing audio (state 1 = no publisher) vs we failed to subscribe.
                log("[audio-diag] audio subscribe state %s->%s uid=%s "
                    "(3=subscribed, 1=no-publisher)" % (old, new, user_id))

            def on_user_audio_track_state_changed(o, lu, user_id, track, state, reason, elapsed):
                log("[audio-diag] audio track state=%s reason=%s uid=%s" % (state, reason, user_id))

            def on_first_remote_audio_frame(o, lu, user_id, elapsed):
                log("[audio-diag] first remote audio FRAME uid=%s — bytes ARE arriving" % user_id)

            def on_first_remote_audio_decoded(o, lu, user_id, elapsed):
                log("[audio-diag] first remote audio DECODED uid=%s — codec OK!" % user_id)

            def on_remote_audio_track_statistics(o, lu, track, stats):
                o._stat_n[0] += 1
                if o._stat_n[0] <= 3 or o._stat_n[0] % 15 == 0:
                    log("[audio-diag] stats: bitrate=%s bytes=%s sr=%s ch=%s loss=%s" % (
                        getattr(stats, "received_bitrate", "?"),
                        getattr(stats, "received_bytes", "?"),
                        getattr(stats, "received_sample_rate", "?"),
                        getattr(stats, "num_channels", "?"),
                        getattr(stats, "audio_loss_rate", "?")))
        try:
            self._lu_obs = LUObs()
            r = self.rtc.register_local_user_observer(self._lu_obs)
            log("[audio-diag] local-user observer registered (rc=%s)" % r)
        except Exception as e:
            log("[audio-diag] registration failed:", e)

    def _register_audio_observer(self):
        try:
            self._register_audio_diag()
            from agora.rtc.audio_frame_observer import IAudioFrameObserver
            pipeline = self.video
            lu = self.rtc.get_local_user()
            # Set BOTH frame formats to the robot's native 8 kHz mono. before-mixing = per-user
            # PCM; playback (post-mix) = the mixed remote output. We take whichever fires.
            try:
                lu.set_playback_audio_frame_before_mixing_parameters(1, AUDIO_RATE)
            except Exception as e:
                log("[audio] set before-mixing params failed:", e)
            try:
                # (channels, sample_rate, mode=0 read-only, samples_per_call: 10 ms frame)
                lu.set_playback_audio_frame_parameters(1, AUDIO_RATE, 0, AUDIO_RATE // 100)
            except Exception as e:
                log("[audio] set playback params failed:", e)

            class AudioObs(IAudioFrameObserver):
                _n = [0]

                def _pcm(o, frame, uid):
                    # ONLY the per-remote-user "before mixing" frame carries the robot's mic.
                    # The post-mix (playback/mixed) frames also contain OUR OWN published audio
                    # (the 'talk' track) looped back — routing those would (a) echo talk into the
                    # listen feed and (b) fire a false "audio works" from our own silence. So we
                    # take before-mix only, and only for the robot's uid.
                    try:
                        o._n[0] += 1
                        if o._n[0] == 1:
                            if self._tx_run and self._tx_start_t:
                                dt = time.time() - self._tx_start_t
                                log("[audio] *** ROBOT MIC OPENED *** from %s "
                                    "(tx=%s, %.1fs after TX start)" % (uid, self._tx_mode, dt))
                            else:
                                log("[audio] *** ROBOT MIC OPENED *** from %s "
                                    "(TX was OFF — self-open)" % uid)
                        pipeline.write_audio(frame.buffer)
                    except Exception:
                        pass
                    return 0

                def on_playback_audio_frame_before_mixing(o, lu_, ch, uid, frame,
                                                          vad_state=-1, vad_bytes=None):
                    return o._pcm(frame, uid)

                # post-mix paths intentionally ignored (they include our own talk track)
                def on_playback_audio_frame(o, lu_, ch, frame):
                    return 0

                def on_mixed_audio_frame(o, lu_, ch, frame):
                    return 0
            self._audio_obs = AudioObs()   # keep a reference (else it's GC'd, no callbacks)
            self.rtc.register_audio_frame_observer(self._audio_obs, 0, None)
            log("[audio] PCM observer registered (listen)")

            # The audio pipeline is correct (verified: PCM decodes at 8 kHz). But the robot's
            # mic often starts MUTED and unmutes later on its own (audio track reason=6 =
            # remote-unmuted) — sometimes minutes after connect. So this is NOT an error: just
            # report when audio is flowing and, if not yet, that we're waiting for the
            # robot to open its mic (no codec change needed).
            def _audio_watchdog(obs=self._audio_obs):
                end = time.time() + 20
                while time.time() < end:
                    if obs._n[0] > 0:
                        return   # _pcm already logged "robot mic is OPEN"
                    time.sleep(0.5)
                if obs._n[0] == 0:
                    # No PCM yet: re-send the "open your mic" command (102001). The robot may have
                    # missed it if it was still joining when we first asked.
                    log("[audio] no PCM yet — re-asking the robot to open its mic (102001)")
                    try:
                        self.send(OP_AUDIO_LISTEN, {"type": 1, "open": 1})
                    except Exception:
                        pass
                    time.sleep(4)
                    if obs._n[0] == 0:
                        log("[audio] still no PCM. The robot did not open its microphone; "
                            "listening will start as soon as it does.")
            threading.Thread(target=_audio_watchdog, daemon=True).start()
        except Exception as e:
            log("[audio] observer registration failed:", e)

    # ------------- AUDIO TX (publish to the robot: keep-alive silence + talk) -------------
    def _mk_pcm(self, chunk):
        from agora.rtc.agora_base import PcmAudioFrame
        spc = AUDIO_RATE // 50               # 20 ms
        f = PcmAudioFrame()
        f.data = bytearray(chunk)
        f.samples_per_channel = spc
        f.bytes_per_sample = 2
        f.number_of_channels = 1
        f.sample_rate = AUDIO_RATE
        f.timestamp = 0
        f.present_time_ms = 0
        return f

    def _start_audio_tx(self):
        """Publish our audio track and keep it alive so we can speak TO the robot ('talk').
        Started on demand when a 'talk' clip is queued; kept alive with silence between clips
        (unpublishing/republishing per clip is slow). Stopped when the camera turns off."""
        if not (self.audio_enabled or self.talk_enabled):
            return
        sender = getattr(self.rtc, "_audio_sender", None) if self.rtc else None
        if not sender:
            return
        with self._talk_lock:
            if self._tx_run:
                return
            self._tx_run = True
        self._tx_start_t = time.time()
        try:
            self.rtc.publish_audio()
            log("[audio-tx] publishing our audio track (mode=%s)" % self._tx_mode)
        except Exception as e:
            log("[audio-tx] publish_audio failed:", e)
        threading.Thread(target=self._audio_tx_loop, args=(sender,), daemon=True).start()

    def _stop_audio_tx(self):
        with self._talk_lock:
            if not self._tx_run:
                return
            self._tx_run = False
        try:
            self.rtc.unpublish_audio()
        except Exception:
            pass

    def _tx_test_sequence(self):
        """DIAG (audio_tx_test=auto): cycle baseline→tone→silence, ~75 s each, and let the
        '*** ROBOT MIC OPENED ***' log tell us which condition (if any) makes the robot publish
        its mic. Runs once on camera-on."""
        def phase(name, mode, secs):
            if getattr(self, "_audio_obs", None) is not None:
                self._audio_obs._n[0] = 0
            self._stop_audio_tx()
            if mode:
                self._tx_mode = mode
                self._start_audio_tx()
            log("[tx-test] === PHASE '%s' (TX=%s) for %ds — watch for ROBOT MIC OPENED ==="
                % (name, mode or "off", secs))
            time.sleep(secs)
            opened = getattr(self, "_audio_obs", None) and self._audio_obs._n[0] > 0
            log("[tx-test] PHASE '%s' result: mic %s" % (name, "OPENED" if opened else "stayed CLOSED"))
        try:
            phase("baseline", None, 75)
            phase("tone", "tone", 75)
            phase("silence", "silence", 75)
            self._stop_audio_tx()
            log("[tx-test] sequence done. Review which phase opened the mic (if any).")
        except Exception as e:
            log("[tx-test] error:", e)

    def _audio_tx_loop(self, sender):
        frame_bytes = (AUDIO_RATE // 50) * 2             # 20 ms mono s16le
        silence = bytes(frame_bytes)
        while self._tx_run:
            src = None
            with self._talk_lock:
                if self._tx_queue:
                    src = self._tx_queue.pop(0)
            if src:
                log("[talk] playing:", src)
                proc = None
                # A live push-to-talk source (rtsp://…/talk published by the browser) can take a
                # moment to appear; don't give up on the first failure.
                live = src.startswith("rtsp://")
                tries = 6 if live else 1
                extra = ["-rtsp_transport", "tcp", "-fflags", "nobuffer",
                         "-flags", "low_delay", "-probesize", "200k",
                         "-analyzeduration", "300000"] if live else []
                try:
                    proc = subprocess.Popen(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error"] + extra + ["-i", src,
                         "-f", "s16le", "-ac", "1", "-ar", str(AUDIO_RATE), "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    n = 0
                    _t0 = time.time()
                    while self._tx_run and not self._talk_stop:
                        chunk = proc.stdout.read(frame_bytes)
                        if not chunk:
                            break
                        if len(chunk) < frame_bytes:
                            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
                        try:
                            sender.send_audio_pcm_data(self._mk_pcm(chunk))
                        except Exception as e:
                            log("[talk] send error:", e)
                            break
                        n += 1
                        time.sleep(0.02)
                    log("[talk] done — %d frames (~%.1fs)" % (n, n * 0.02))
                    if live and n == 0 and not self._talk_stop and tries > 1 \
                            and time.time() - _t0 < 10:
                        time.sleep(0.8)          # publisher not up yet — try again shortly
                        with self._talk_lock:
                            self._tx_queue.insert(0, src)
                except Exception as e:
                    log("[talk] error:", e)
                finally:
                    if proc:
                        try:
                            proc.stdout.close()
                            proc.terminate()
                        except Exception:
                            pass
            else:
                # keep-alive: silence, or (DIAG) a low tone to test whether the robot opens its
                # mic only when it hears real audio energy (VAD), not a silent publisher.
                if self._tx_mode == "tone":
                    chunk = self._tone_chunk()
                else:
                    chunk = silence
                try:
                    sender.send_audio_pcm_data(self._mk_pcm(chunk))
                except Exception:
                    pass
                time.sleep(0.02)

    def _tone_chunk(self):
        """One 20 ms chunk of a looping ~400 Hz tone (DIAG). 400 Hz @ 8 kHz = 20 samples/period,
        so the cached buffer loops click-free. Moderate amplitude for clear VAD energy."""
        import math
        n = AUDIO_RATE // 50                 # samples per 20 ms
        if self._tone_buf is None:
            per = max(AUDIO_RATE // 400, 1)  # samples per period
            length = per * 40                # whole number of periods
            amp = 6000                       # ~ -15 dBFS
            buf = bytearray(length * 2)
            for i in range(length):
                v = int(amp * math.sin(2.0 * math.pi * (i % per) / per))
                buf[2 * i] = v & 0xFF
                buf[2 * i + 1] = (v >> 8) & 0xFF
            self._tone_buf = bytes(buf)
            self._tone_pos = 0
        out = bytearray(n * 2)
        tb = self._tone_buf
        pos = self._tone_pos
        for j in range(n * 2):
            out[j] = tb[pos]
            pos = (pos + 1) % len(tb)
        self._tone_pos = pos
        return bytes(out)

    def _talk(self, source):
        """Queue an audio source to play through the robot's speaker. Anything ffmpeg can read:
        an http(s) URL (e.g. a Home Assistant TTS media URL) or a file path."""
        source = (source or "").strip()
        if not source:
            return
        if not (self.audio_enabled or self.talk_enabled):
            log("[talk] enable 'audio' (or 'talk') in the add-on options first")
            return
        self._talk_stop = False
        try:   # open the "us -> robot speaker" direction, like the app's mic button does
            self.send(OP_AUDIO_TALK, {"type": 1, "open": 1})
        except Exception:
            pass
        with self._talk_lock:
            self._tx_queue.append(source)
        # if the TX loop isn't running (talk enabled but camera off), start it now
        if not self._tx_run:
            self._start_audio_tx()

    def _camera_feed(self, on):
        """Turn our RTSP feed on/off. The robot streams whenever we're present in RTC; this
        just controls whether we re-publish it as RTSP."""
        if not self.video:
            self._setup_video_pipeline()
        if not self.video:
            return
        if on:
            # Wake the robot first — like the app, opening the camera wakes it from standby.
            self._wake()
            self.video.start_feed()
            if self.robot_uid:
                try:
                    self.rtc.send_intra_request(self.robot_uid)
                except Exception:
                    pass
            log("[video] ON — camera stream: %s" % self._rtsp_url())
            threading.Thread(target=self._video_diag, daemon=True).start()
            # NOTE: normal operation does NOT auto-publish audio. Tested: publishing a silent
            # track does not open the robot mic (v0.17.1). Listen is pure subscribe.
            # DIAG A/B (audio_tx_test option): drive the TX to learn what opens the robot's mic.
            if self.tx_test in ("silence", "tone"):
                self._tx_mode = self.tx_test
                log("[tx-test] audio_tx_test=%s — publishing to see if the mic opens" % self.tx_test)
                self._start_audio_tx()
            elif self.tx_test == "auto":
                threading.Thread(target=self._tx_test_sequence, daemon=True).start()
        else:
            self.video.stop_feed()
            self._stop_audio_tx()
            log("[video] OFF — camera stream stopped")

    def _video_diag(self):
        """Nudge keyframes, re-wake, and warn if no decoded frames arrive."""
        started = time.time()
        warned = False
        last_wake = time.time()
        while not self.stop.is_set() and self.video and self.video.feeding:
            if self.video.frames == 0:
                if self.robot_uid:
                    try:
                        self.rtc.send_intra_request(self.robot_uid)
                    except Exception:
                        pass
                # the robot may still be waking from standby — re-send wake every ~8s
                if time.time() - last_wake > 8:
                    last_wake = time.time()
                    self._wake()
                if not warned and time.time() - started > 20:
                    warned = True
                    log("[video] ⚠ still 0 decoded frames after 20s — the robot may not be "
                        "publishing, or the SDK isn't decoding. RTSP is up but empty.")
                self.stop.wait(1)
            else:
                self.stop.wait(8)

    def _check_sleep_on_dock(self):
        """After a 'dock' command: as soon as the robot is actually on the charger, leave the
        session so it can go to sleep (ZZ). Sending it home means you're done with it — otherwise
        our presence would keep it awake on the base. Disabled when auto-standby is off (0), and it
        gives up after 10 minutes in case the docking never completed."""
        started = getattr(self, "_sleep_on_dock", 0)
        if not started or STANDBY_TIMEOUT <= 0 or not self.connected:
            return
        if time.time() - started > 600:            # docking didn't finish — stop waiting
            self._sleep_on_dock = 0
            return
        b = (self.telemetry or {}).get("battery") or {}
        on_charger = b.get("adapterStatus", -1) != -1 or bool(b.get("chargeStatus"))
        if on_charger:
            self._sleep_on_dock = 0
            log("[dock] robot is on the charger — releasing the session so it can sleep")
            try:
                self.set_connected(False)
            except Exception as e:
                log("[dock] releasing the session failed:", e)

    def _wake(self):
        """Nudge the robot awake (isSleeping=false, opcode 101047). Cheap and safe to repeat: this
        is called from the connect path and from the 'waiting for frames' retry loop, so it must
        NOT reconnect anything (that would recurse). The heavier, cloud-backed wake used for deep
        sleep lives in _wake_full()."""
        try:
            self.send(OP_SLEEP, {"isSleeping": False})
            log("[wake] sent wake (isSleeping=false)")
        except Exception as e:
            log("[wake] failed:", e)

    def _wake_full(self):
        """User-initiated wake. Two different sleeps exist:
          * light standby (we left the channel, or it dozed while we watched) -> a fresh viewer join
            brings it back;
          * DEEP sleep (it drove home to the dock and shows the ZZ eyes) -> it left Agora entirely,
            so no opcode of ours reaches it; only a fresh CLOUD session does.
        Only ever called from the explicit 'wake' command, never from the connect path."""
        self._wake()
        try:
            if not self.connected:
                self.set_connected(True)          # refreshes the cloud session on its own
            elif not (self.video and self.video.is_streaming()):
                self._force_rejoin()              # deep sleep: needs the fresh cloud session
        except Exception as e:
            log("[wake] rejoin after wake failed:", e)

    def set_camera(self, on):
        self.video_on = on
        # The robot wakes/sleeps by our PRESENCE in the Agora RTC channel — a *fresh viewer join* is
        # what wakes it, exactly like opening the app. The isSleeping opcode alone does NOT reliably
        # wake it from standby. Crucially, the robot can drift back to ZZ on its own (charging/idle)
        # while WE are still "connected" — so "connected" is not enough to know it's awake. We check
        # whether live frames are actually arriving:
        #   - not connected            -> fresh join (set_connected) wakes it + feeds
        #   - connected but NO frames  -> robot slept under us -> force a fresh RTC rejoin to wake it
        #   - connected AND streaming  -> already awake -> just make sure the feed is on (no rejoin,
        #                                 so re-asserting camera/on as a keep-alive won't blip video)
        if on:
            streaming = bool(self.video and self.video.is_streaming())
            if not self.connected:
                self.set_connected(True)
            elif not streaming:
                self._force_rejoin()
            elif self.video and not self.video.feeding:
                self._camera_feed(True)
            # else: already connected, feeding and streaming — nothing to do. This is the keep-alive
            # path (camera/on re-asserted every ~20 s); doing nothing avoids spawning a new diag
            # thread each time and avoids a needless video blip.
        else:
            self._camera_feed(False)
        self._publish_camera_state()

    def _force_rejoin(self):
        """Leave and rejoin the Agora RTC channel: a fresh viewer join is what actually WAKES the
        robot from standby. Mirrors the app reconnecting when you reopen it. connect_agora() restarts
        the video feed on its own because video_on is True."""
        # Rate-limit: a rejoin tears down and rebuilds the whole Agora session (and now asks the
        # cloud for a fresh one). Video needs a few seconds to produce its first frame, so anything
        # that retries on "not streaming yet" could otherwise spin here and hammer the cloud.
        now = time.time()
        if now - getattr(self, "_last_rejoin", 0) < 15:
            return
        self._last_rejoin = now
        log("[wake] robot not streaming — forcing a fresh RTC rejoin to wake it")
        # DEEP sleep (the robot parked itself on the dock and shows the ZZ eyes) is different from
        # the standby we trigger ourselves: the robot leaves Agora entirely and only keeps its link
        # to Enabot's cloud. Re-joining the channel with our CACHED tokens then reaches nobody —
        # which is why the robot could only be revived from the official app. The app asks the cloud
        # for a FRESH session every time it opens a robot, and it's that cloud call which tells the
        # robot to come back online. So do the same here before rejoining.
        if self.provider:
            try:
                self.refresh_session()
            except Exception as e:
                log("[wake] session refresh failed (continuing):", e)
        try:
            if self.rtc:
                try:
                    self.rtc.disconnect()
                except Exception:
                    pass
            try:
                if self.rtm:
                    self.rtm.logout()
            except Exception:
                pass
        except Exception:
            pass
        self.connected = True
        try:
            self.connect_agora()
            self.send(OP_HANDSHAKE, {"userId": self.account})
        except Exception as e:
            log("[wake] rejoin failed:", e)

    def set_connected(self, on):
        """Master session switch. OFF: leave the Agora session so the robot can sleep (no
        control/telemetry). ON: reconnect. MQTT/entities stay up throughout."""
        if on == self.connected:
            self._publish_conn_state()
            return
        if on:
            self.connected = True
            log("[*] connecting session…")
            # Ask the cloud for a FRESH session first (like the app does when you open a robot):
            # that call is what brings a deeply-sleeping robot — one that parked on the dock and
            # left Agora — back online. Reconnecting with cached tokens alone would not reach it.
            if self.provider:
                try:
                    self.refresh_session()
                except Exception as e:
                    log("[*] session refresh failed (continuing):", e)
            try:
                self.connect_agora()
                self.send(OP_HANDSHAKE, {"userId": self.account})
                time.sleep(1)
                self.send(OP_GET_SETTINGS)
                self.send(OP_MOTION_GET)
                self.send(OP_GET_ROUTES)
            except Exception as e:
                log("[!] reconnect failed:", e)
        else:
            self.connected = False
            log("[*] disconnecting session — robot can sleep")
            try:
                if self.video:
                    self.video.stop_feed()
            except Exception:
                pass
            try:
                if self.rtc:
                    self.rtc.disconnect()
            except Exception:
                pass
            # Full disconnect (known-good standby): also log out RTM so the robot reliably sleeps.
            # (The 0.26.32 "keep RTM" experiment is deferred to the auto-standby plan — reverted here
            # so the shipped standby stays reliable until we can verify the RTC-only behaviour.)
            try:
                if self.rtm:
                    self.rtm.logout()
            except Exception:
                pass
            self.rtm = None
            self.rtc = None
            self._observers_registered = False
            # Standby stops the video too — reflect it so Home Assistant shows a clear change
            # (otherwise the camera switch stays "on" and it looks like nothing happened).
            self.video_on = False
            self._publish_camera_state()
        self._publish_conn_state()

    def _publish_conn_state(self):
        if self.mqtt:
            self.mqtt.publish("%s/connected/state" % NODE,
                              "on" if self.connected else "off", retain=True)

    def _opts(self):
        return PublishOptions(
            channel_type=RtmChannelType.RTM_CHANNEL_TYPE_USER,
            message_type=RtmMessageType.RTM_MESSAGE_TYPE_BINARY,
        )

    def send(self, mid, data=None):
        """Build the message and ENQUEUE it — never touches the SDK directly, so a slow cloud send
        can't block the caller (MQTT receive thread, control loop…). _sender_loop does the publish."""
        if not (self.connected and self.rtm):   # the "connected" switch is off
            return
        msg = {"id": mid, "type": 0, "timestamp": time.time() * 1000}
        if self.sid:
            msg["sid"] = self.sid
        if data is not None:
            msg["data"] = data
        payload = json.dumps(msg, separators=(",", ":")).encode()
        if mid == OP_MOVE:
            # coalesce: only the newest steering vector matters — replace any pending one
            with self._move_lock:
                self._latest_move = payload
        else:
            try:
                self._send_q.put_nowait((mid, payload))
            except queue.Full:
                pass   # under backpressure drop the stale command
        self._send_evt.set()

    def _publish_now(self, mid, payload):
        rtm = self.rtm
        if not rtm:
            return
        t0 = time.perf_counter()
        try:
            r, _ = rtm.publish(self.s["robot_rtm"], payload, self._opts())
        except Exception as e:
            log("[!] publish %s error: %s" % (mid, e))
            return
        dt = (time.perf_counter() - t0) * 1000.0
        if dt > 2000:   # only when genuinely slow (cloud degrading) — not normal jitter
            log("[timing] ⚠ slow RTM dispatch of cmd %s: %.0f ms — the cloud link is degrading"
                % (mid, dt))
        if r != 0:
            log("[!] publish %s failed: %s" % (mid, rtm.get_error_reason(r)))

    def _sender_loop(self):
        """The ONLY thread that calls rtm.publish(): serializes every send (the SDK is not
        thread-safe) and keeps slow cloud sends off the receive/control threads. Movement is sent
        FIRST each pass (and coalesced) so steering stays responsive even when the link is slow."""
        while not self.stop.is_set():
            self._send_evt.wait(0.5)
            self._send_evt.clear()
            while not self.stop.is_set():
                mv = None
                with self._move_lock:
                    if self._latest_move is not None:
                        mv, self._latest_move = self._latest_move, None
                if mv is not None:
                    self._publish_now(OP_MOVE, mv)   # priority: latest steering vector
                    continue
                try:
                    mid, payload = self._send_q.get_nowait()
                except queue.Empty:
                    break
                self._publish_now(mid, payload)

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
        # RTM sniffer (debug): the app and the bridge publish commands to the SAME robot RTM
        # channel we're subscribed to — so with debug on, whatever the app sends (e.g. tapping
        # the "audio/listen" icon) is captured here with its exact opcode. Skip the frequent
        # telemetry to keep the noise down. This is how we find the mic-enable trigger command.
        if mid != OP_TELEMETRY:
            try:
                log("[rtm-raw] id=%s %s" % (mid, json.dumps(data, separators=(",", ":"))),
                    level="debug")
            except Exception:
                pass
        if obj.get("rsid"):
            self.sid = obj["rsid"]
        if mid == OP_TELEMETRY:
            self.telemetry = data
            self._publish_telemetry()
            self._check_sleep_on_dock()
        elif mid == OP_SETTINGS:
            # MERGE (not replace): the robot's settings report omits some write-only fields
            # (imageStyle, callAutoRecording — confirmed absent live), so we keep the values we
            # optimistically set on command; a replace would wipe them on every report.
            if isinstance(data, dict):
                self.settings.update(data)
            # debug: show exactly which fields the robot reports (e.g. is imageStyle /
            # callAutoRecording echoed back?) — helps diagnose read-back gaps.
            log("[settings] %s" % json.dumps(data, sort_keys=True), level="debug")
            self._publish_settings()
        elif mid in (RESP_MOTION, RESP_MOTION_ECHO):
            # MotionSettings (obstacle avoidance, steering sensitivity, pickup, desktop mode, …)
            if isinstance(data, dict):
                self.motion.update(data)
            log("[motion] %s" % json.dumps(data, sort_keys=True))
            self._publish_settings()
        elif mid == OP_INFO:
            self.info = data
            self._publish_telemetry()      # refresh fw/ip/ssid diagnostic sensors
            # Now that we know the robot's mac/sn, announce it to the companion integration and
            # refresh the MQTT device blocks (so the mac connection is present for the merge).
            if not self._integ_announced and self.info.get("mac"):
                self._integ_announced = True
                self._publish_integration_discovery()
                try:
                    self._publish_discovery(self.mqtt)
                except Exception as e:
                    log("[discovery] re-announce failed:", e)
        elif mid == RESP_ROUTES:
            self._routes_supported = True     # the robot answered → route/patrol works on this model
            lst = data.get("list") or []
            self.routes = [(r.get("routeName") or ("route %s" % r.get("id")),
                            r.get("id")) for r in lst if r.get("id") is not None]
            log("[patrol] %d route(s) from the robot" % len(self.routes))
            self._publish_patrol_select()
            self._publish_settings()          # refresh the panel's routes list
        elif mid == RESP_ROUTE_REC_ACK:
            self._route_rec = (data.get("status", 0) == 0)
            log("[route] recording %s" % ("started" if self._route_rec else "start failed"))
            self._publish_settings()
        elif mid == RESP_ROUTE_PROGRESS:
            log("[route] progress %s" % json.dumps(data, separators=(",", ":")), level="debug")
        elif mid == RESP_ROUTE_DATA:
            # recording stopped: the robot hands back the recorded route to save (routeFile + tempId)
            if data.get("status", 0) == 0:
                self._route_pending = data
                self._route_rec = False
                log("[route] recorded, ready to save (tempId=%s)" % data.get("tempId"))
                self._publish_settings()
            else:
                log("[route] recording ended with status %s" % data.get("status"))

    # ---------------- control loop ----------------

    def control_loop(self):
        """Heartbeat every 2 s; movement at 10 Hz only while there's an active vector."""
        last_beat = 0.0
        last_idle_check = 0.0
        was_moving = False
        while not self.stop.is_set():
            now = time.time()
            if now - last_beat >= 2:
                self.send(OP_HEARTBEAT, {"state": 0})
                last_beat = now
            # Auto-standby: the robot only sleeps when nobody is watching, and WE are a viewer that
            # never leaves — which is why it never showed the ZZ eyes while the add-on ran. After
            # STANDBY_TIMEOUT with no command from you, leave the session so it can sleep, exactly
            # like closing the official app. Any command (or opening the camera) wakes it again.
            if STANDBY_TIMEOUT > 0 and self.connected and now - last_idle_check >= 5:
                last_idle_check = now
                idle = now - getattr(self, "_last_activity", 0)
                if idle > STANDBY_TIMEOUT:
                    log("[standby] idle for %d min — leaving the session so the robot can sleep"
                        % (idle / 60))
                    self._last_activity = now      # don't re-trigger immediately
                    try:
                        self.set_connected(False)
                    except Exception as e:
                        log("[standby] failed:", e)
            with self.lock:
                # watchdog: if the command expired, zero it (dead-man's switch)
                if self.vec_deadline and now > self.vec_deadline:
                    self.vec = {"lx": 0, "ly": 0, "rx": 0, "ry": 0,
                                "buttons": self.vec.get("buttons", 0)}   # keep scheme on the stop frame
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

    def set_move(self, lx=0, ly=0, rx=0, ry=0, hold=0.6, buttons=0):
        # buttons = the control scheme the robot should use (1 = dual-stick: independent throttle + a
        # CONTINUOUS turn; 0 = single joystick: the vector is a heading). The panel picks it per control.
        with self.lock:
            self.vec = {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "buttons": int(buttons)}
            self.vec_deadline = time.time() + hold if any((lx, ly, rx, ry)) else 0

    def _set_motion(self, field, value):
        """Change one MotionSettings field and re-send the WHOLE object (opcode 103023) — the robot
        expects the full struct. We start from the last values the robot reported (self.motion), or
        sensible defaults if we haven't read them yet (avoidobstacle is also echoed in the settings
        blob, so we can seed it from there)."""
        m = self.motion or {}
        payload = {
            "status": 0,
            "pickUpCheck": bool(m.get("pickUpCheck", False)),
            "autoDesktopMode": bool(m.get("autoDesktopMode", False)),
            "avoidobstacle": bool(m.get("avoidobstacle", self.settings.get("avoidobstacle", False))),
            "steeringSensitivity": int(m.get("steeringSensitivity", 0)),
            "abnormalExerciseReminder": bool(m.get("abnormalExerciseReminder", False)),
        }
        payload[field] = value
        self.motion.update({k: v for k, v in payload.items() if k != "status"})   # optimistic
        self.send(OP_MOTION_SET, payload)
        self._publish_settings()

    # ---------------- MQTT / Home Assistant ----------------

    def connect_mqtt(self):
        # paho-mqtt 2.x requires the callback API version; fall back for 1.x
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="%s_bridge" % NODE)
        except (AttributeError, TypeError):
            c = mqtt.Client(client_id="%s_bridge" % NODE)
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
        dev = {
            "identifiers": [NODE],
            "name": os.environ.get("EBO_DEVICE_NAME", "EBO Air 2"),
            "manufacturer": "Enabot",
            "model": self.info.get("model", "EBO Air 2"),
            "sw_version": self.info.get("masterMcuVersion", ""),
        }
        # The robot's MAC lets the companion integration's live camera MERGE into THIS same
        # device (HA joins devices that share a connection), so each robot is one device.
        mac = self.info.get("mac")
        if mac:
            dev["connections"] = [["mac", mac]]
        return dev

    def _publish_integration_discovery(self):
        """Announce this robot to the companion HA integration (custom_components/ebo),
        which turns it into a 'device detected → Add' flow that creates a live camera. Retained
        so HA sees it whenever it (re)starts. Topic namespace is fixed regardless of EBO_NODE."""
        if not self.mqtt or not self.info.get("mac"):
            return
        api_port = os.environ.get("EBO_API_PORT", "8098")
        # HA core reaches the API over the internal network (hostname), NOT the LAN host IP —
        # the LAN/VLAN may firewall this port. Fall back to host_ip if the hostname is unknown.
        host_ip = os.environ.get("EBO_API_HOST", "") or self.host_ip or ""
        payload = {
            "node": NODE,
            "name": os.environ.get("EBO_DEVICE_NAME", "EBO Air 2"),
            "sn": self.info.get("sn", ""),
            "mac": self.info.get("mac", ""),
            "model": self.info.get("model", "EBO Air 2"),
            "rtsp": self._rtsp_url(host_ip),   # internal hostname -> reachable by HA core
            "robot_id": self.robot_id,        # cloud robot id (for remove-from-account)
            # for the native HA integration: its data/command API + token
            "api": ("http://%s:%s" % (host_ip, api_port)) if host_ip else "",
            "token": os.environ.get("EBO_API_TOKEN", ""),
        }
        self.mqtt.publish("ebo/discovery/%s" % NODE, json.dumps(payload), retain=True)
        log("[discovery] announced robot to the EBO integration (%s)" % payload["name"])

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
        # ALWAYS subscribe to the command topics FIRST — the bridge must receive commands even in
        # native mode (expose_mqtt off), where the HA-entity discovery below is skipped. (These used
        # to sit AFTER the expose_mqtt gate, so native mode silently stopped receiving commands.)
        for _t in ("laser/set", "speed/set", "move/+", "move/vector", "joystick", "sleep/set",
                   "wake", "say", "talk", "talk/stop", "listen/set", "audio_tx/set", "volume/set", "talkback_volume/set",
                   "sports_record/set", "call_rec/set", "upload_cloud/set", "dock",
                   "patrol/route/set", "patrol/start", "patrol/stop", "camera/set", "connected/set",
                   "route/record/start", "route/record/stop", "route/save", "route/delete",
                   "rotate/set", "video_quality/set", "image_style/set", "night_vision/set",
                   "move_mode/set", "eyes/set", "roaming/set", "ai_track", "motion/set",
                   "avoid_obstacle/set", "steering/set", "pickup_check/set", "desktop_mode/set",
                   "abnormal_reminder/set",
                   "voice/set", "ai_ask", "cmd"):    # "cmd" = raw opcode escape hatch (AI/eyes)
            c.subscribe("%s/%s" % (NODE, _t))
        if not self.expose_mqtt:
            # native-integration mode: skip MQTT entity discovery (the panel/integration still
            # get state via <node>/state and commands via the topics subscribed above).
            log("[MQTT] expose_mqtt=off — subscribed to commands; skipping HA entity discovery")
            return
        st = "%s/state" % NODE

        # clean up entities removed in v0.4.4 (patrol / AI tracking were not real
        # one-shot commands; they live on the raw ebo/cmd channel now)
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
        self._disc("button", "wake", {
            "name": "EBO wake", "command_topic": "%s/wake" % NODE,
            "icon": "mdi:weather-sunny"})
        # text-to-speech: type text, the robot says it (great for automations/AI)
        self._disc("text", "say", {
            "name": "EBO say", "command_topic": "%s/say" % NODE,
            "state_topic": "%s/say/state" % NODE, "icon": "mdi:bullhorn"})
        # talk (you -> robot speaker): only exposed when talk is enabled. Send an audio URL/path
        # (e.g. a Home Assistant TTS media URL) and it plays through the robot's speaker.
        if self.talk_enabled or self.audio_enabled:
            self._disc("text", "talk", {
                "name": "EBO talk (audio URL)", "command_topic": "%s/talk" % NODE,
                "icon": "mdi:microphone-message"})
        # playback volume
        self._disc("number", "volume", {
            "name": "EBO volume", "command_topic": "%s/volume/set" % NODE,
            "min": 0, "max": 100, "step": 1, "optimistic": True,
            "icon": "mdi:volume-high"})
        # talkback (mic) volume — has real state from settings
        self._disc("number", "talkback_volume", {
            "name": "EBO talkback volume", "command_topic": "%s/talkback_volume/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.talkback_volume | default('') }}",
            "min": 0, "max": 100, "step": 1, "icon": "mdi:microphone"})
        # motion recording (records when it detects movement)
        self._disc("switch", "sports_record", {
            "name": "EBO motion recording", "state_topic": st,
            "value_template": "{{ value_json.sports_record | default('false') }}",
            "command_topic": "%s/sports_record/set" % NODE,
            "payload_on": "on", "payload_off": "off", "state_on": "true", "state_off": "false",
            "icon": "mdi:motion-sensor"})
        # auto-record calls
        self._disc("switch", "call_rec", {
            "name": "EBO auto-record calls", "state_topic": st,
            "value_template": "{{ value_json.call_rec | default('false') }}",
            "command_topic": "%s/call_rec/set" % NODE,
            "payload_on": "on", "payload_off": "off", "state_on": "true", "state_off": "false",
            "icon": "mdi:record-rec"})
        # upload recordings to the cloud (privacy) — optimistic (not in the status report)
        self._disc("switch", "upload_cloud", {
            "name": "EBO cloud upload", "command_topic": "%s/upload_cloud/set" % NODE,
            "payload_on": "on", "payload_off": "off", "optimistic": True,
            "icon": "mdi:cloud-upload"})
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
        # master session switch: OFF disconnects from the cloud so the robot can sleep
        self._disc("switch", "connected", {
            "name": "EBO connected", "command_topic": "%s/connected/set" % NODE,
            "state_topic": "%s/connected/state" % NODE,
            "payload_on": "on", "payload_off": "off", "icon": "mdi:lan-connect"})
        # patrol: pick a route (auto = no route) and start it
        self._publish_patrol_select()
        self._disc("button", "patrol_start", {
            "name": "EBO start patrol", "command_topic": "%s/patrol/start" % NODE,
            "icon": "mdi:play-circle-outline"})

        # ---- extra controls from the full command catalog (docs/COMMANDS-APK.md) ----
        # rotate by an angle (degrees). A number that sends 103001 on change.
        self._disc("number", "rotate", {
            "name": "EBO rotate", "command_topic": "%s/rotate/set" % NODE,
            "min": -180, "max": 180, "step": 5, "optimistic": True,
            "unit_of_measurement": "°", "icon": "mdi:rotate-right"})
        # camera: video quality / image style / shoot mode (real state from settings)
        self._disc("select", "video_quality", {
            "name": "EBO video quality", "command_topic": "%s/video_quality/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.video_quality | default('') }}",
            "options": list(VIDEO_QUALITY_MAP.keys()), "icon": "mdi:high-definition"})
        self._disc("select", "image_style", {
            "name": "EBO image style", "command_topic": "%s/image_style/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.image_style | default('') }}",
            "options": list(IMAGE_STYLE_MAP.keys()), "icon": "mdi:image-filter-vintage"})
        self._disc("select", "night_vision", {
            "name": "EBO night vision", "command_topic": "%s/night_vision/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.night_vision | default('') }}",
            "options": list(NIGHT_MODE_MAP.keys()), "icon": "mdi:weather-night"})
        self._disc("select", "move_mode", {
            "name": "EBO driving mode", "command_topic": "%s/move_mode/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.move_mode | default('') }}",
            "options": list(MOVE_MODE_MAP.keys()), "icon": "mdi:steering"})
        # collision avoidance — real state (the robot echoes avoidobstacle in the settings report)
        self._disc("switch", "avoid_obstacle", {
            "name": "EBO collision avoidance", "command_topic": "%s/avoid_obstacle/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.avoid_obstacle | default('false') }}",
            "payload_on": "true", "payload_off": "false", "state_on": "true", "state_off": "false",
            "icon": "mdi:wall"})
        self._disc("select", "eyes", {
            "name": "EBO eyes", "command_topic": "%s/eyes/set" % NODE,
            "options": list(EYES_STYLES.keys()), "optimistic": True, "icon": "mdi:eye"})
        # autonomous roaming
        self._disc("switch", "roaming", {
            "name": "EBO roaming", "command_topic": "%s/roaming/set" % NODE,
            "payload_on": "on", "payload_off": "off", "optimistic": True,
            "icon": "mdi:radar"})
        # AI subject tracking (starts tracking a person/pet in view)
        self._disc("button", "ai_track", {
            "name": "EBO AI track", "command_topic": "%s/ai_track" % NODE,
            "icon": "mdi:target-account"})
        # play a preset motion / voice by id (0-based; handy for automations)
        self._disc("number", "motion_preset", {
            "name": "EBO play motion", "command_topic": "%s/motion/set" % NODE,
            "min": 0, "max": 30, "step": 1, "optimistic": True, "icon": "mdi:run"})
        self._disc("number", "voice_preset", {
            "name": "EBO play voice", "command_topic": "%s/voice/set" % NODE,
            "min": 0, "max": 30, "step": 1, "optimistic": True, "icon": "mdi:account-voice"})
        # ask the built-in AI a question (Air 2 has an LLM agent)
        self._disc("text", "ai_ask", {
            "name": "EBO ask AI", "command_topic": "%s/ai_ask" % NODE,
            "icon": "mdi:robot-happy"})

        # ---- extra telemetry sensors (from the 101026 status report) ----
        self._disc("binary_sensor", "sd_present", {
            "name": "EBO SD card", "state_topic": st,
            "value_template": "{{ value_json.sd_present | default('false') }}",
            "payload_on": "true", "payload_off": "false", "device_class": "connectivity",
            "entity_category": "diagnostic"})
        self._disc("sensor", "sd_free", {
            "name": "EBO SD free", "state_topic": st,
            "value_template": "{{ value_json.sd_free | default('') }}",
            "unit_of_measurement": "GB", "icon": "mdi:sd", "entity_category": "diagnostic"})
        self._disc("sensor", "sd_total", {
            "name": "EBO SD total", "state_topic": st,
            "value_template": "{{ value_json.sd_total | default('') }}",
            "unit_of_measurement": "GB", "icon": "mdi:sd", "entity_category": "diagnostic"})
        self._disc("sensor", "storage_free", {
            "name": "EBO storage free", "state_topic": st,
            "value_template": "{{ value_json.storage_free | default('') }}",
            "unit_of_measurement": "GB", "icon": "mdi:harddisk", "entity_category": "diagnostic"})
        self._disc("binary_sensor", "docked", {
            "name": "EBO docked", "state_topic": st,
            "value_template": "{{ value_json.docked | default('false') }}",
            "payload_on": "true", "payload_off": "false", "icon": "mdi:home-import-outline"})
        self._disc("binary_sensor", "safe_mode", {
            "name": "EBO guard mode", "state_topic": st,
            "value_template": "{{ value_json.safe_mode | default('false') }}",
            "payload_on": "true", "payload_off": "false", "device_class": "safety"})
        self._disc("sensor", "task", {
            "name": "EBO activity", "state_topic": st,
            "value_template": "{{ value_json.task | default('idle') }}", "icon": "mdi:state-machine"})
        # ---- device info (from the 101004 system-info report) — diagnostic ----
        self._disc("sensor", "fw_ipc", {
            "name": "EBO firmware (camera)", "state_topic": st,
            "value_template": "{{ value_json.fw_ipc | default('') }}",
            "icon": "mdi:chip", "entity_category": "diagnostic"})
        self._disc("sensor", "fw_mcu", {
            "name": "EBO firmware (MCU)", "state_topic": st,
            "value_template": "{{ value_json.fw_mcu | default('') }}",
            "icon": "mdi:chip", "entity_category": "diagnostic"})
        self._disc("sensor", "robot_ip", {
            "name": "EBO IP", "state_topic": st,
            "value_template": "{{ value_json.ip | default('') }}",
            "icon": "mdi:ip-network", "entity_category": "diagnostic"})
        self._disc("sensor", "robot_ssid", {
            "name": "EBO WiFi SSID", "state_topic": st,
            "value_template": "{{ value_json.ssid | default('') }}",
            "icon": "mdi:wifi", "entity_category": "diagnostic"})

        c.subscribe("%s/laser/set" % NODE)
        c.subscribe("%s/speed/set" % NODE)
        c.subscribe("%s/move/+" % NODE)
        # generic channel for an agent: JSON {"ly":-50,"rx":0,"hold":1.0}
        c.subscribe("%s/move/vector" % NODE)
        c.subscribe("%s/joystick" % NODE)      # {"x":-1..1,"y":-1..1} from a joystick card
        c.subscribe("%s/sleep/set" % NODE)
        c.subscribe("%s/wake" % NODE)
        c.subscribe("%s/say" % NODE)
        c.subscribe("%s/talk" % NODE)          # play audio (URL/path) through the robot speaker
        c.subscribe("%s/listen/set" % NODE)    # open/close the robot's microphone (102001)
        c.subscribe("%s/talk/stop" % NODE)     # end a live push-to-talk
        c.subscribe("%s/audio_tx/set" % NODE)  # DIAG A/B: off | silence | tone
        c.subscribe("%s/volume/set" % NODE)
        c.subscribe("%s/talkback_volume/set" % NODE)
        c.subscribe("%s/sports_record/set" % NODE)
        c.subscribe("%s/call_rec/set" % NODE)
        c.subscribe("%s/upload_cloud/set" % NODE)
        c.subscribe("%s/dock" % NODE)
        c.subscribe("%s/patrol/route/set" % NODE)
        c.subscribe("%s/patrol/start" % NODE)
        c.subscribe("%s/patrol/stop" % NODE)
        for _rt in ("route/record/start", "route/record/stop", "route/save", "route/delete"):
            c.subscribe("%s/%s" % (NODE, _rt))
        c.subscribe("%s/camera/set" % NODE)
        c.subscribe("%s/connected/set" % NODE)
        # extra controls
        for topic in ("rotate/set", "video_quality/set", "image_style/set",
                      "night_vision/set", "move_mode/set", "avoid_obstacle/set", "eyes/set",
                      "roaming/set", "ai_track", "motion/set", "voice/set", "ai_ask"):
            c.subscribe("%s/%s" % (NODE, topic))
        self._publish_camera_state()
        self._publish_conn_state()
        # RAW escape hatch for an AI/automation: publish {"id":<opcode>,"data":{...}}
        # to ebo/cmd to send ANY command from the full catalog (docs/COMANDI.md).
        c.subscribe("%s/cmd" % NODE)

    def _on_mqtt_message(self, c, u, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace").strip()
        # Any command from you counts as "someone is using the robot" and postpones auto-standby.
        # (Driving in fullscreen re-asserts camera/on every ~20 s, so it stays awake while you drive.)
        self._last_activity = time.time()
        # If you take control back by driving, cancel the "sleep once docked" intent.
        if ("/move" in topic or topic.endswith("/joystick")) and getattr(self, "_sleep_on_dock", 0):
            self._sleep_on_dock = 0
            log("[dock] driving again — cancelling the sleep-on-dock")
        try:
            if topic.endswith("/laser/set"):
                self.send(OP_LASER, {"laser": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/speed/set"):
                self.send(OP_SET_SPEED, {"moveSpeed": int(float(payload))})
            elif topic.endswith("/move/vector"):
                v = json.loads(payload)
                self.set_move(v.get("lx", 0), v.get("ly", 0), v.get("rx", 0),
                              v.get("ry", 0), v.get("hold", 0.6), v.get("buttons", 0))
            elif topic.endswith("/sleep/set"):
                self.send(OP_SLEEP, {"isSleeping": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/wake"):
                self._wake_full()
            elif topic.endswith("/say"):
                if payload:
                    self.send(OP_SAY, {"userId": self.account, "text": payload})
                    self.mqtt.publish("%s/say/state" % NODE, payload)
            elif topic.endswith("/talk/stop"):
                # end a live push-to-talk: drop what's queued and break the current playback
                self._talk_stop = True
                with self._talk_lock:
                    self._tx_queue = []
                self.send(OP_AUDIO_TALK, {"type": 1, "open": 0})
                log("[talk] stopped")
            elif topic.endswith("/listen/set"):
                # open/close the robot's microphone (what the app's speaker button does)
                on = payload.lower() in ("on", "true", "1")
                self.listen_on = on
                self.send(OP_AUDIO_LISTEN, {"type": 1, "open": 1 if on else 0})
                log("[audio] listen -> %s" % ("on" if on else "off"))
                self._publish_settings()
            elif topic.endswith("/talk"):
                # play arbitrary audio (URL/path) through the robot's speaker — YOUR voice/audio
                self._talk(payload)
            elif topic.endswith("/audio_tx/set"):
                # DIAG A/B: control the publish keep-alive at runtime to test what opens the mic
                mode = (payload or "").strip().lower()
                if mode in ("off", "0", "stop"):
                    self._stop_audio_tx()
                    log("[audio-tx] DIAG: stopped (TX off)")
                elif mode in ("silence", "tone"):
                    self._tx_mode = mode
                    if getattr(self, "_audio_obs", None) is not None:
                        self._audio_obs._n[0] = 0   # reset so next MIC-OPENED logs fresh
                    self._stop_audio_tx()
                    time.sleep(0.3)
                    self._start_audio_tx()
                    log("[audio-tx] DIAG: (re)started, mode=%s — watching for ROBOT MIC OPENED"
                        % mode)
                else:
                    log("[audio-tx] DIAG: unknown mode '%s' (use off|silence|tone)" % mode)
            elif topic.endswith("/volume/set"):
                self.send(OP_VOLUME, {"playbackVolume": int(float(payload)),
                                      "isPlaybackMuted": False})
            elif topic.endswith("/talkback_volume/set"):
                self.send(OP_TALKBACK_VOL, {"talkbackVolume": int(float(payload))})
            elif topic.endswith("/sports_record/set"):
                self.send(OP_SPORTS_REC,
                          {"sportsRecord": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/call_rec/set"):
                on = payload.lower() in ("on", "true", "1")
                self.send(OP_CALL_REC, {"callAutoRecording": 1 if on else 0})
                # robot never echoes this field → reflect intent optimistically
                self.settings["callAutoRecording"] = 1 if on else 0
                self._publish_telemetry()
            elif topic.endswith("/upload_cloud/set"):
                self.send(OP_UPLOAD_CLOUD,
                          {"videoUploadCloud": payload.lower() in ("on", "true", "1")})
            elif topic.endswith("/dock"):
                # start returning to the charging base (no-op if already charging)
                self.send(OP_DOCK, {"startUp": True})
                # …and let it fall asleep as soon as it actually gets there: sending it home means
                # you're done with it, so we leave the session once the charger reports contact
                # (see _check_sleep_on_dock). The window guards against sleeping much later because
                # of a dock command that never completed.
                self._sleep_on_dock = time.time()
                log("[dock] returning to base — will release the session once it's charging")
            elif topic.endswith("/patrol/route/set"):
                self.patrol_choice = payload
                self.mqtt.publish("%s/patrol/route" % NODE, payload, retain=True)
            elif topic.endswith("/patrol/start"):
                self._start_patrol()
            elif topic.endswith("/patrol/stop"):
                self.send(OP_PATROL_STOP)
                log("[patrol] stop")
            elif topic.endswith("/route/record/start"):
                self._route_pending = None
                self.send(OP_ROUTE_REC_START)     # start tracing; drive to teach the path
                self._route_rec = True
                log("[route] record start")
                self._publish_settings()
            elif topic.endswith("/route/record/stop"):
                self.send(OP_ROUTE_REC_STOP)      # robot replies 103206 with the recorded route
                self._route_rec = False
                log("[route] record stop")
                self._publish_settings()
            elif topic.endswith("/route/save"):
                # payload = the name to give the just-recorded route
                if not self._route_pending:
                    log("[route] nothing recorded to save")
                else:
                    p = dict(self._route_pending)
                    p["routeName"] = payload or ("route %d" % (len(self.routes) + 1))
                    p.setdefault("status", 0)
                    self.send(OP_ROUTE_SAVE, p)
                    log("[route] save '%s' (tempId=%s)" % (p["routeName"], p.get("tempId")))
                    self._route_pending = None
                    self.send(OP_GET_ROUTES)      # refresh the list
                    self._publish_settings()
            elif topic.endswith("/route/delete"):
                # payload = route id (or name) to delete
                rid = None
                try:
                    rid = int(payload)
                except (TypeError, ValueError):
                    rid = dict(self.routes).get(payload)
                if rid is not None:
                    self.send(OP_ROUTE_DELETE, {"ids": [int(rid)]})
                    log("[route] delete id=%s" % rid)
                    self.send(OP_GET_ROUTES)
            elif topic.endswith("/camera/set"):
                self.set_camera(payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/connected/set"):
                self.set_connected(payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/rotate/set"):
                self.send(OP_ROTATE, {"angle": int(float(payload))})
            elif topic.endswith("/video_quality/set"):
                self.send(OP_VIDEO_QUALITY, {"videoQuality": VIDEO_QUALITY_MAP.get(payload, 2)})
            elif topic.endswith("/image_style/set"):
                iv = IMAGE_STYLE_MAP.get(payload, 0)
                self.send(OP_IMAGE_STYLE, {"imageStyle": iv})
                # robot never echoes this field → reflect intent optimistically, and remember it
                self.settings["imageStyle"] = iv
                self._ui_save(imageStyle=iv)
                self._publish_telemetry()
            elif topic.endswith("/night_vision/set"):
                self.send(OP_NIGHT_MODE, {"shootMode": NIGHT_MODE_MAP.get(payload, 0)})
            elif topic.endswith("/move_mode/set"):
                self.send(OP_MOVE_MODE, {"moveMode": MOVE_MODE_MAP.get(payload, 0)})
            elif topic.endswith("/eyes/set"):
                mode, style = EYES_STYLES.get(payload, (1, 1))
                if payload in EYES_STYLES:
                    self.eyes_choice = payload      # so the UI can show what's selected
                    self._ui_save(eyes=payload)
                self.send(OP_EYES, {
                    "status": 0, "mode": mode,
                    "dynamicEyes": {"autoFollow": False, "styleId": style if mode == 1 else 1},
                    "timeEyes": {"styleId": style if mode == 2 else 1},
                    "customEyes": {"timeStyle": 0},
                })
            elif topic.endswith("/avoid_obstacle/set"):
                on = payload.lower() in ("on", "true", "1")
                self.send(OP_AVOID_OBSTACLE, {"avoidobstacle": on})   # direct setter, no bundle clobber
                self.settings["avoidobstacle"] = on                   # optimistic (settings report confirms)
                self._publish_settings()
            elif topic.endswith("/steering/set"):
                self._set_motion("steeringSensitivity", STEERING_MAP.get(payload, 0))
            elif topic.endswith("/pickup_check/set"):
                self._set_motion("pickUpCheck", payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/desktop_mode/set"):
                self._set_motion("autoDesktopMode", payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/abnormal_reminder/set"):
                self._set_motion("abnormalExerciseReminder", payload.lower() in ("on", "true", "1"))
            elif topic.endswith("/roaming/set"):
                on = payload.lower() in ("on", "true", "1")
                self.send(OP_ROAM, {"isRoamOn": on, "sensitivity": 5})
            elif topic.endswith("/ai_track"):
                self.send(OP_AI_TRACK, {"mode": 0, "trackTarget": 7})
            elif topic.endswith("/motion/set"):
                self.send(OP_PLAY_MOTION, {"cycleMode": 0, "moveId": int(float(payload))})
            elif topic.endswith("/voice/set"):
                self.send(OP_PLAY_VOICE, {"cycleMode": 0, "voiceId": int(float(payload))})
            elif topic.endswith("/ai_ask"):
                if payload:
                    self.send(OP_AI_ASK, {"modelType": 0, "session": "",
                                          "question": payload, "userId": self.account})
            elif topic.endswith("/cmd"):
                # raw command from an AI/automation: {"id":<opcode>,"data":{...}}
                obj = json.loads(payload)
                mid = int(obj["id"])
                self.send(mid, obj.get("data"))
                log("[MQTT] raw cmd id=%s sent" % mid)
            elif topic.endswith("/joystick"):
                # continuous control: {"x":-1..1,"y":-1..1} from a joystick card.
                # x = turn (right +), y = forward (+). Held ~0.4s; the card resends while
                # dragging, the watchdog stops it on release.
                j = json.loads(payload)
                x = max(-1.0, min(1.0, float(j.get("x", 0))))
                y = max(-1.0, min(1.0, float(j.get("y", 0))))
                self.set_move(0, int(-y * 100), int(x * 100), hold=0.4)
            elif "/move/" in topic:
                d = topic.rsplit("/", 1)[-1]
                # gentle nudges for the buttons: forward/back softer, turns smaller so
                # left/right don't spin ~90° each tap.
                mv, tn = 50, 35
                mapping = {
                    "forward": (0, -mv, 0), "back": (0, mv, 0),
                    "left": (0, 0, -tn), "right": (0, 0, tn), "stop": (0, 0, 0),
                }
                if d in mapping:
                    lx, ly, rx = mapping[d]
                    self.set_move(lx, ly, rx, hold=0.35)
        except Exception as e:
            log("[MQTT] command error %s: %s" % (topic, e))

    def _publish_telemetry(self):
        if not self.mqtt:        # telemetry can arrive before MQTT is up
            return
        t = self.telemetry
        b = t.get("battery", {})
        stt = t.get("status", {})
        sd = t.get("sdcard", {})
        stor = t.get("storage", {})
        se = self.settings
        mo = self.motion
        info = self.info

        def gb(x):
            try:
                return round(float(x) / 1e9, 1)
            except (TypeError, ValueError):
                return None

        payload = {
            "battery": b.get("percentage"),
            "charging": "true" if b.get("chargeStatus") else "false",
            "wifi": t.get("wifiStrength"),
            "volume": se.get("playbackVolume"),   # speaker volume (robot's own voice/sounds) — was missing
            "recording": "true" if stt.get("isVideoRecording") else "false",
            "laser": "true" if stt.get("laserStatus") else "false",
            "speed": se.get("moveSpeed"),
            "talkback_volume": se.get("talkbackVolume"),
            "listen": "true" if self.listen_on else "false",
            # Write-only on the robot (it never reports them back), so we echo what we last set —
            # otherwise these selects sit on "unknown" forever.
            "eyes": self.eyes_choice or "",
            "sports_record": "true" if se.get("sportsRecord") else "false",
            "call_rec": "true" if se.get("callAutoRecording") else "false",
            # routes / patrol (teach-and-repeat): the saved routes, plus recording state.
            # routes_supported: the panel shows the Routes UI only when the robot actually answers the
            # route query — the Air 2 firmware ignores route/patrol, so it stays hidden there.
            "routes_supported": ("true" if self._routes_supported
                                 else ("false" if (self._routes_query_ts
                                                   and time.time() - self._routes_query_ts > 15)
                                       else "unknown")),
            "routes": [{"id": rid, "name": name} for (name, rid) in self.routes],
            "route_recording": "true" if self._route_rec else "false",
            "route_pending": "true" if self._route_pending else "false",
            # camera / movement settings (current values feed the selects)
            "video_quality": _rev(VIDEO_QUALITY_MAP, se.get("videoQuality")),
            "image_style": _rev(IMAGE_STYLE_MAP, se.get("imageStyle")),
            "night_vision": _rev(NIGHT_MODE_MAP, se.get("shootMode")),
            "move_mode": _rev(MOVE_MODE_MAP, se.get("moveMode")),
            # motion / sport settings (MotionSettings, opcode 103022). avoidobstacle is also echoed
            # in the settings blob, so fall back to it when the MotionSettings read hasn't arrived.
            "avoid_obstacle": "true" if mo.get("avoidobstacle", se.get("avoidobstacle")) else "false",
            "steering": _rev(STEERING_MAP, mo.get("steeringSensitivity")),
            "pickup_check": "true" if mo.get("pickUpCheck") else "false",
            "desktop_mode": "true" if mo.get("autoDesktopMode") else "false",
            "abnormal_reminder": "true" if mo.get("abnormalExerciseReminder") else "false",
            # storage
            "sd_present": "true" if sd.get("isPresent") else "false",
            "sd_free": gb(sd.get("availableBytes")),
            "sd_total": gb(sd.get("capacityBytes")),
            "storage_free": gb(stor.get("availableBytes")),
            # dock / guard
            # adapterStatus alone is unreliable (it reported -1 while the robot was visibly on the
            # base and reporting chargeStatus) — so trust either signal, like _check_sleep_on_dock.
            "docked": ("true" if (b.get("adapterStatus", -1) != -1 or b.get("chargeStatus"))
                       else "false"),
            "safe_mode": "true" if stt.get("safeMode") else "false",
            "task": self._task_label(t.get("tasks"), stt, b),
            # device info (101004)
            "fw_ipc": info.get("ipcVersion", ""),
            "fw_mcu": info.get("masterMcuVersion", ""),
            "ip": info.get("ip", ""),
            "ssid": info.get("wifiSsid", ""),
        }
        self.mqtt.publish("%s/state" % NODE, json.dumps(payload), retain=True)

    def _ui_load(self):
        try:
            with open(self._ui_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}          # missing or corrupt: start clean, it's only a UI convenience

    def _ui_save(self, **kw):
        """Remember a write-only setting across restarts (eyes style, image style).
        Written atomically: a half-written file here would make the next boot lose both choices."""
        self._ui.update(kw)
        tmp = self._ui_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._ui, f)
            os.replace(tmp, self._ui_path)
        except Exception as e:
            log("could not persist UI choices: %s" % e)

    _TASK_LABELS = {1: "moving", 6: "AI tracking", 7: "on a call", 8: "shared view",
                    9: "guard mode", 10: "charging", 11: "upgrading"}

    def _task_label(self, tasks, stt, b):
        """Human-readable current activity from the tasks[] array / status flags."""
        # same rule as the docked flag: adapterStatus alone misses some charging states
        if ((b.get("adapterStatus", -1) != -1 or b.get("chargeStatus"))
                and (b.get("percentage") or 0) < 100):
            base = "charging"
        else:
            base = "idle"
        try:
            for task in (tasks or []):
                tid = task.get("taskId")
                if tid in self._TASK_LABELS:
                    return self._TASK_LABELS[tid]
        except Exception:
            pass
        if stt.get("safeMode"):
            return "guard mode"
        return base

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
        threading.Thread(target=self._sender_loop, daemon=True).start()   # single RTM sender
        threading.Thread(target=self.control_loop, daemon=True).start()
        self.send(OP_HANDSHAKE, {"userId": self.account})
        time.sleep(1)
        self.send(OP_GET_SETTINGS)
        self.send(OP_MOTION_GET)          # fetch obstacle-avoidance / steering / etc.
        self.send(OP_GET_ROUTES)          # populate the patrol-route select (+ probe route support)
        if not self._routes_query_ts:
            self._routes_query_ts = time.time()
        log("[*] bridge running")
        last_check = time.time()
        try:
            # short, interruptible wait so a stop signal is honoured within ~1 s
            while not self.stop.wait(1):
                if time.time() - last_check < 30:
                    continue
                last_check = time.time()
                if self.connected and self.provider and not self._token_age_ok():
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


def discover_robots():
    """Log in and return [(robot_id, robot_name)] for every robot on the account."""
    c = ebo_cloud.EboCloud(host=os.environ.get("EBO_HOST", "ebox-eu.enabotserverintl.com"))
    r = c.login(os.environ.get("EBO_EMAIL"), os.environ.get("EBO_PASSWORD"),
                region=os.environ.get("EBO_REGION", "GB"))
    if r.get("code") != 200:
        raise RuntimeError("login failed: %s" % r.get("msg"))
    out = []
    for rb in c.robots().get("data", {}).get("list", []):
        ri = rb.get("robot_info", {})
        rid = ri.get("robot_id")
        out.append((rid, ri.get("robot_name") or ("EBO %s" % rid)))
    return out


def main():
    # discovery mode (used by run.sh to enumerate robots): print "id\tname" per robot to the
    # real stdout, then exit.
    if os.environ.get("EBO_DISCOVER") == "1":
        try:
            for rid, name in discover_robots():
                raw("ROBOT\t%s\t%s" % (rid, name))
        except Exception as e:
            raw("ERR\t%s" % e)
        return 0

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
