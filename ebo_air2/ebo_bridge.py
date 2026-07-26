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
OP_MOVE_MODE = 103011   # {"moveMode": int}
OP_SHOOT_MODE = 102035  # {"shootMode": int}  (photo/video)
OP_PLAY_MOTION = 103005  # {"cycleMode": int, "moveId": int} — preset motion (MOVES)
OP_PLAY_VOICE = 103007   # {"cycleMode": int, "voiceId": int}
OP_DOCK = 103043         # manual return-to-base / start charging: {"startUp": bool} (MOVES)
OP_PATROL = 103061       # start patrol: {"mode","trackTarget","routeId","voiceId"} (MOVES)
OP_GET_ROUTES = 104001   # ask the robot for the saved patrol routes
RESP_ROUTES = 104002     # robot's reply: {"status", "list":[{id, routeName, routeFile}]}

# --- extra controls mapped from the decompiled command builder (docs/COMMANDS-APK.md) ---
OP_ROTATE = 103001        # {"angle": int} — rotate the head/body by an angle
OP_VIDEO_QUALITY = 102055  # {"videoQuality": int}  3=High 2=Medium 1=Low
OP_IMAGE_STYLE = 102057   # {"imageStyle": int}  0/1/2
OP_PLAY_VOICE = 103007    # {"cycleMode": int, "voiceId": int}
OP_ROAM = 101061          # {"isRoamOn": bool, "sensitivity": int} — autonomous roaming
OP_AI_TRACK = 103049      # StartAiTrackData {"mode": int, "trackTarget": int}
OP_EYES = 104057          # EyesEmojiModeData {"status","mode",...}
OP_AI_ASK = 103301        # AI chat: {"modelType","session","question","userId"}

# value tables (from the app's UI): name shown in HA -> integer sent to the robot
VIDEO_QUALITY_MAP = {"Low": 1, "Medium": 2, "High": 3}
IMAGE_STYLE_MAP = {"Standard": 0, "Vivid": 1, "Soft": 2}
SHOOT_MODE_MAP = {"Normal": 0, "Wide": 1, "Follow": 2}
MOVE_MODE_MAP = {"Mode 1": 0, "Mode 2": 1, "Mode 3": 2}
EYES_MODE_MAP = {"Dynamic": 0, "Clock": 1, "Custom": 2}


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
# path. Single robot keeps the classic "ebo_air2" so existing entities are untouched.
NODE = os.environ.get("EBO_NODE", "ebo_air2")


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
        self._integ_announced = False    # announced this robot to the companion integration?
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
        # expose HA entities over MQTT discovery (default on). Off = native integration owns them;
        # MQTT is still used for the panel's state/commands, just not for entity discovery.
        self.expose_mqtt = os.environ.get("EBO_EXPOSE_MQTT", "1") == "1"
        self.audio_enabled = os.environ.get("EBO_AUDIO", "0") == "1"   # listen (optional)
        self.talk_enabled = os.environ.get("EBO_TALK", "0") == "1"     # speak TO the robot
        self._talk_lock = threading.Lock()
        self._tx_run = False           # audio TX loop (keep-alive silence + talk) running?
        self._tx_queue = []            # queued 'talk' sources
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
        self.svc = svc
        # AUDIO codec: the robot streams its mic with a custom telephony codec (Agora payload
        # type 8 = monitor / 9 = call). Agora's own guidance for this case (payload 8 = G.711)
        # is that che.audio.codec_unfallback + custom_payload_type must be set on the GLOBAL
        # engine parameter handle BEFORE joining — setting them on the per-connection handle
        # after connect() never takes effect (that's why the PCM observer got 0 frames). The
        # server SDK's global handle is service.get_agora_parameter(). Set them here, pre-join.
        if self.audio_enabled:
            try:
                pt = int(os.environ.get("EBO_AUDIO_PT", "8"))
                gp = svc.get_agora_parameter()
                for kv in ('{"che.audio.codec_unfallback":[0,8,9]}',
                           '{"che.audio.custom_payload_type":%d}' % pt,
                           '{"che.audio.aec.enable":false}'):
                    gp.set_parameters(kv)
                log("[audio] codec params set on ENGINE before join "
                    "(codec_unfallback [0,8,9], payload_type %d)" % pt)
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
                pt = int(os.environ.get("EBO_AUDIO_PT", "8"))
                cp = self.rtc.get_agora_parameter()
                cp.set_parameters('{"che.audio.codec_unfallback":[0,8,9]}')
                cp.set_parameters('{"che.audio.custom_payload_type":%d}' % pt)
                log("[audio] codec params also set on connection after connect (pt=%d)" % pt)
            except Exception as e:
                log("[audio] connection set_parameters failed:", e)

        if self.video_enabled:
            self._setup_video_pipeline()
            if self.video_on:            # restore camera state across reconnects
                self._camera_feed(True)

    def _rtsp_url(self):
        host = self.host_ip or "<HOME-ASSISTANT-IP>"
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
                    log("[audio] subscribed OK, but the robot's mic is still MUTED. It opens on "
                        "its own, unpredictably (sometimes minutes later, sometimes not at all). "
                        "This is a known limitation: the phone app sends an RTM command to open "
                        "it that we haven't captured yet. Audio will play if/when the robot "
                        "unmutes — no action needed.")
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
                try:
                    proc = subprocess.Popen(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
                         "-f", "s16le", "-ac", "1", "-ar", str(AUDIO_RATE), "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    n = 0
                    while self._tx_run:
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

    def _wake(self):
        """Wake the robot from standby (sends isSleeping=false, opcode 101047). Not movement —
        mirrors the app, where opening the live camera wakes the robot."""
        try:
            self.send(OP_SLEEP, {"isSleeping": False})
            log("[wake] sent wake (isSleeping=false)")
        except Exception as e:
            log("[wake] failed:", e)

    def set_camera(self, on):
        self.video_on = on
        self._camera_feed(on)
        self._publish_camera_state()

    def set_connected(self, on):
        """Master session switch. OFF: leave the Agora session so the robot can sleep (no
        control/telemetry). ON: reconnect. MQTT/entities stay up throughout."""
        if on == self.connected:
            self._publish_conn_state()
            return
        if on:
            self.connected = True
            log("[*] connecting session…")
            try:
                self.connect_agora()
                self.send(OP_HANDSHAKE, {"userId": self.account})
                time.sleep(1)
                self.send(OP_GET_SETTINGS)
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
            try:
                if self.rtm:
                    self.rtm.logout()
            except Exception:
                pass
            self.rtm = None
            self.rtc = None
            self._observers_registered = False
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
        if not (self.connected and self.rtm):   # the "connected" switch is off
            return
        msg = {"id": mid, "type": 0, "timestamp": time.time() * 1000}
        if self.sid:
            msg["sid"] = self.sid
        if data is not None:
            msg["data"] = data
        payload = json.dumps(msg, separators=(",", ":")).encode()
        try:
            r, _ = self.rtm.publish(self.s["robot_rtm"], payload, self._opts())
        except Exception as e:
            log("[!] publish %s error: %s" % (mid, e))
            return
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
            lst = data.get("list") or []
            self.routes = [(r.get("routeName") or ("route %s" % r.get("id")),
                            r.get("id")) for r in lst if r.get("id") is not None]
            log("[patrol] %d route(s) from the robot" % len(self.routes))
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
        """Announce this robot to the companion HA integration (custom_components/ebo_air2),
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
            "rtsp": self._rtsp_url(),
            "robot_id": self.robot_id,        # cloud robot id (for remove-from-account)
            # for the native HA integration: its data/command API + token
            "api": ("http://%s:%s" % (host_ip, api_port)) if host_ip else "",
            "token": os.environ.get("EBO_API_TOKEN", ""),
        }
        self.mqtt.publish("ebo_air2/discovery/%s" % NODE, json.dumps(payload), retain=True)
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
        if not self.expose_mqtt:
            # native-integration mode: skip MQTT entity discovery (the panel/integration still
            # get state via <node>/state and commands via the topics). Status stays for the panel.
            log("[MQTT] expose_mqtt=off — not publishing HA entity discovery")
            return
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
        self._disc("select", "shoot_mode", {
            "name": "EBO shoot mode", "command_topic": "%s/shoot_mode/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.shoot_mode | default('') }}",
            "options": list(SHOOT_MODE_MAP.keys()), "icon": "mdi:camera-iris"})
        self._disc("select", "move_mode", {
            "name": "EBO move mode", "command_topic": "%s/move_mode/set" % NODE,
            "state_topic": st, "value_template": "{{ value_json.move_mode | default('') }}",
            "options": list(MOVE_MODE_MAP.keys()), "icon": "mdi:cog-transfer"})
        self._disc("select", "eyes", {
            "name": "EBO eyes", "command_topic": "%s/eyes/set" % NODE,
            "options": list(EYES_MODE_MAP.keys()), "optimistic": True, "icon": "mdi:eye"})
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
        c.subscribe("%s/audio_tx/set" % NODE)  # DIAG A/B: off | silence | tone
        c.subscribe("%s/volume/set" % NODE)
        c.subscribe("%s/talkback_volume/set" % NODE)
        c.subscribe("%s/sports_record/set" % NODE)
        c.subscribe("%s/call_rec/set" % NODE)
        c.subscribe("%s/upload_cloud/set" % NODE)
        c.subscribe("%s/dock" % NODE)
        c.subscribe("%s/patrol/route/set" % NODE)
        c.subscribe("%s/patrol/start" % NODE)
        c.subscribe("%s/camera/set" % NODE)
        c.subscribe("%s/connected/set" % NODE)
        # extra controls
        for topic in ("rotate/set", "video_quality/set", "image_style/set",
                      "shoot_mode/set", "move_mode/set", "eyes/set", "roaming/set",
                      "ai_track", "motion/set", "voice/set", "ai_ask"):
            c.subscribe("%s/%s" % (NODE, topic))
        self._publish_camera_state()
        self._publish_conn_state()
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
            elif topic.endswith("/wake"):
                self._wake()
            elif topic.endswith("/say"):
                if payload:
                    self.send(OP_SAY, {"userId": self.account, "text": payload})
                    self.mqtt.publish("%s/say/state" % NODE, payload)
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
            elif topic.endswith("/patrol/route/set"):
                self.patrol_choice = payload
                self.mqtt.publish("%s/patrol/route" % NODE, payload, retain=True)
            elif topic.endswith("/patrol/start"):
                self._start_patrol()
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
                # robot never echoes this field → reflect intent optimistically
                self.settings["imageStyle"] = iv
                self._publish_telemetry()
            elif topic.endswith("/shoot_mode/set"):
                self.send(OP_SHOOT_MODE, {"shootMode": SHOOT_MODE_MAP.get(payload, 0)})
            elif topic.endswith("/move_mode/set"):
                self.send(OP_MOVE_MODE, {"moveMode": MOVE_MODE_MAP.get(payload, 0)})
            elif topic.endswith("/eyes/set"):
                self.send(OP_EYES, {"status": 0, "mode": EYES_MODE_MAP.get(payload, 0)})
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
            "recording": "true" if stt.get("isVideoRecording") else "false",
            "laser": "true" if stt.get("laserStatus") else "false",
            "speed": se.get("moveSpeed"),
            "talkback_volume": se.get("talkbackVolume"),
            "sports_record": "true" if se.get("sportsRecord") else "false",
            "call_rec": "true" if se.get("callAutoRecording") else "false",
            # camera / movement settings (current values feed the selects)
            "video_quality": _rev(VIDEO_QUALITY_MAP, se.get("videoQuality")),
            "image_style": _rev(IMAGE_STYLE_MAP, se.get("imageStyle")),
            "shoot_mode": _rev(SHOOT_MODE_MAP, se.get("shootMode")),
            "move_mode": _rev(MOVE_MODE_MAP, se.get("moveMode")),
            # storage
            "sd_present": "true" if sd.get("isPresent") else "false",
            "sd_free": gb(sd.get("availableBytes")),
            "sd_total": gb(sd.get("capacityBytes")),
            "storage_free": gb(stor.get("availableBytes")),
            # dock / guard
            "docked": "true" if b.get("adapterStatus", -1) != -1 else "false",
            "safe_mode": "true" if stt.get("safeMode") else "false",
            "task": self._task_label(t.get("tasks"), stt, b),
            # device info (101004)
            "fw_ipc": info.get("ipcVersion", ""),
            "fw_mcu": info.get("masterMcuVersion", ""),
            "ip": info.get("ip", ""),
            "ssid": info.get("wifiSsid", ""),
        }
        self.mqtt.publish("%s/state" % NODE, json.dumps(payload), retain=True)

    _TASK_LABELS = {1: "moving", 6: "AI tracking", 7: "on a call", 8: "shared view",
                    9: "guard mode", 10: "charging", 11: "upgrading"}

    def _task_label(self, tasks, stt, b):
        """Human-readable current activity from the tasks[] array / status flags."""
        if b.get("adapterStatus", -1) != -1 and (b.get("percentage") or 0) < 100:
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
