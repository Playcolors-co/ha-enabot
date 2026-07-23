# Changelog — Enabot integration

## 0.13.5 — finer driving + joystick channel
- **Gentler move buttons** (A): each tap is a shorter, smaller nudge — turns no longer spin
  ~90° per press, forward/back are softer.
- **Joystick channel** (B): new MQTT topic `ebo_air2/joystick` accepting `{"x":-1..1,"y":-1..1}`
  for smooth, continuous driving from a joystick card (x = turn, y = forward). Pair it with the
  EBO joystick Lovelace card. (Cloud latency still applies.)

## 0.13.4 — much lower video latency
- The stream lagged because ffmpeg was forced to 15 fps (`-r`/`-vsync cfr`) while the robot
  sends ~25 fps → it buffered and dropped frames. Now it passes the real frame rate through
  with arrival timestamps + low-delay flags: **latency should drop a lot** (clean DTS kept).
- For the lowest latency/CPU use `video_preset: ultrafast` and a lower `video_max_height`.

## 0.13.3 — audio no longer breaks video
- With `audio: true`, ffmpeg had a second (audio) input; if the robot's PCM didn't arrive it
  **stalled the whole mux and froze the video**. Fixed: (1) the audio observer is now kept
  referenced (it was garbage-collected, so it never fired), and (2) the pipeline feeds
  **silence** when no real audio arrives, so ffmpeg never blocks — **video always flows**,
  with audio overlaid when the robot sends it.

## 0.13.2 — quieter log + log level
- New **`log_level`** option: `info` (default) shows key events only — no more `N frames
  received` spam; `debug` for the chatty lines; `warning` for problems only. Video keeps a
  light "still streaming" heartbeat every few minutes at info level.

## 0.13.1 — audio fix + diagnostics
- Audio didn't work because a required SDK call was missing:
  `set_playback_audio_frame_before_mixing_parameters(1, 16000)` — without it the PCM callback
  never fires. Added it (+ `audio_recv_media_packet=0`). The log now shows
  `[audio] first PCM frame from …` when audio is flowing.
- Silenced transient template warnings on the new entities (defaults).

## 0.13.0 — audio (listen), experimental
- Optional **audio**: the robot's microphone (16 kHz mono PCM from the SDK) is muxed into the
  camera stream as AAC, so the Generic Camera has **sound**. Enable with `audio: true` (needs
  `video: true`). Off by default; if it ever misbehaves the safety net falls back to
  control-only. Two-way *talk* is a separate future step.

## 0.12.1 — camera stream: fix timestamps ("No dts")
- The re-encoded stream could produce timestamps HA's stream backend rejected ("No dts in N
  consecutive packets"). ffmpeg now timestamps incoming frames by arrival
  (`use_wallclock_as_timestamps`) and forces a constant output rate (`-r`, CFR), giving clean
  monotonic DTS/PTS. (The `Connection refused`/`404` errors were just the add-on being down
  during the update — transient.)

## 0.12.0 — "connected" switch + CI
- **EBO connected** switch (default on): turn it **off** to fully leave the cloud session so
  the robot can **sleep** (no control/telemetry while off); turn it back on to reconnect. MQTT
  entities stay available throughout.
- **CI:** a GitHub Actions workflow builds the add-on image on every push/PR, so build breaks
  are caught before release.

## 0.11.0 — more entities
- New controls (verified against the app): **motion recording** (switch), **auto-record calls**
  (switch), **cloud upload** (switch, privacy), **talkback volume** (number). The recording/
  volume ones show real state from the robot's settings report.
- Eyes/emoji, DND and other complex settings stay on the raw `ebo_air2/cmd` channel (they need
  structured payloads) — see COMANDI.md.

## 0.10.0 — video CPU: resolution/quality options
- The robot streams ~2304×1296 (2K); re-encoding that is CPU-heavy on a NUC. New options:
  `video_max_height` (default **720** — big CPU saving; set `0` for native 2K) and
  `video_preset` (libx264 speed/quality). The log shows the chosen resolution/preset.

## 0.9.2 — video works: fix client attach (keyframes)
- 🎉 Live video works (H.265 decoded by the SDK → re-encoded to H.264 → RTSP). Fixed the
  "Timeout while loading URL" when adding the camera: ffmpeg now emits a **keyframe every ~2s**
  (`-g`, `-keyint_min`, no B-frames) so Home Assistant / VLC can attach immediately instead of
  waiting up to ~16s for the default GOP.

## 0.9.1 — the missing video switch: enable_video=1
- The decoded observer got 0 frames because the Agora **service** config was missing
  `enable_video = 1` (found in the official `example_video_yuv_receive.py`). Without it the SDK
  doesn't process video at all. Added it. If this was the blocker, you'll now see
  `[video] first decoded frame WxH` with `video: true` + the **EBO camera** switch on.

## 0.9.0 — video via the SDK's H.265 DECODER (new approach)
- Root cause found in the official SDK docs: the *encoded* frame observer segfaults for H.265,
  but the SDK **decodes H.265 to raw YUV**. Until now the add-on only registered the encoded
  observer (hence 0 frames / crashes).
- Now it registers the **decoded** video-frame observer (`register_video_frame_observer`,
  `auto_subscribe_video=1`), reads the YUV frames and **re-encodes to H.264 with ffmpeg** →
  RTSP. If the robot publishes and the SDK decodes, the log shows `first decoded frame WxH`
  and `N frames received`.
- Enable with `video: true` + the **EBO camera** switch. Watch the log for the frame lines.

## 0.8.3 — fix camera race (double mediamtx / observer error)
- `connect_agora` and `on_user_joined` could both subscribe at once, starting mediamtx twice
  and double-registering the encoded observer (`unregister_video_encoded_frame_observer`
  error). Now serialized with a lock and made idempotent. Camera URL detection confirmed
  working (`rtsp://<HA-IP>:8554/ebo`).

## 0.8.2 — log the running version (spot stale updates)
- The log now prints the **version actually running** (baked into the image) and compares it
  to what the Supervisor thinks is installed. If they differ, it says the image wasn't rebuilt
  (stale) — so you always know exactly which version you're testing. `VERSION.txt` in the image
  guarantees the code layer is never stale-cached.
- If you ever see the mismatch warning: **uninstall + reinstall** the add-on for a clean build.

## 0.8.1 — fix the camera URL (real IP)
- The camera URL showed the `<HOME-ASSISTANT-IP>` placeholder because the add-on couldn't
  read the host IP. Added `hassio_api` permission to auto-detect it, plus a manual **`host_ip`**
  option as a fallback. The **EBO camera URL** sensor now shows e.g. `rtsp://192.168.88.15:8554/ebo`.
- Reminder: the camera on/off control is the **EBO camera** switch on the *EBO Air 2 device*
  (not on the add-on page).

## 0.8.0 — camera on/off switch + RTSP URL shown
- **EBO camera switch** (default OFF): the add-on no longer subscribes to the robot's video by
  default, so the robot is **not kept in video mode** all the time (saves battery / privacy).
  Control stays on (RTC presence). Flip the switch on only when you want the stream.
- **EBO camera URL** sensor + a log line show the exact RTSP link (with your HA IP) once the
  camera is on, e.g. `rtsp://192.168.88.15:8554/ebo`.
- Video subscribe is now **runtime** (subscribe/unsubscribe on the switch) instead of always-on.

## 0.7.0 — safety net for video experiments
- **Supervisor safety net:** control and video share one Agora/RTC connection (the robot only
  accepts commands while you're present in RTC), so a native video crash takes the bridge
  down. The add-on now **auto-falls back to control-only** after repeated quick crashes — no
  more crash loops; control/telemetry always come back.
- New **`video_encoded`** option (experimental) to try the encoded-H.265 path on demand.
- Agora SDK version **pinned** (build arg `AGORA_SDK_VERSION`) so control is reproducible and
  we can test other versions for the video path.

## 0.6.1 — back to stable (encoded video confirmed crashing)
- Attempt #1 result: the encoded-only subscribe (`auto_subscribe_video=0`) **segfaults this
  Agora SDK build regardless of the subscribe method** (both `subscribe_all_video` and
  `subscribe_video` crash). Reverted to the **stable** config (`auto=1`, no crash, 0 frames
  for H.265). The experimental encoded path is now behind an env flag (`EBO_VIDEO_ENCODED=1`)
  so it can't crash the default setup. `video: true` is safe again (RTSP up, empty).

## 0.6.0 — experimental video attempt #1
- **Video (experimental):** try to receive the robot's **encoded H.265** by subscribing to
  its stream **per-uid** (`subscribe_video`) in encoded-only mode, instead of the
  `subscribe_all_video` call that segfaulted. If the SDK hands over frames, ffmpeg passes the
  raw H.265 to HA (no decoder needed on our side). Enable with `video: true` and watch the log
  for `[video] N frames received`; if it segfaults or shows `0 frames`, set `video: false`
  (control/telemetry are unaffected either way).

## 0.5.4
- **More reliable updates:** an add-on update rebuilds the Docker image; the video-only
  extras (ffmpeg, mediamtx from GitHub) are now **non-fatal** and `pip` retries, so a flaky
  network/GitHub outage can't fail the whole rebuild and leave you stuck on the old version.

## 0.5.3
- **Fix crash on start:** `_on_mqtt_connect` could run before `self.mqtt` was set, throwing
  `AttributeError` and killing the MQTT thread (entities not published). Now assigned early
  and the callback is guarded.
- **Fix segfault:** the v0.5.1 encoded-only video subscribe (`auto_subscribe_video=0`)
  crashed the native Agora SDK and took the whole bridge down — reverted to the stable
  config. Port 8554 stays exposed. (Video via this SDK remains limited by H.265.)

## 0.5.2
- Add this changelog (shown by Home Assistant in the update dialog).

## 0.5.1
- **Video:** expose port **8554** so `rtsp://<HA-IP>:8554/ebo` is reachable (was a missing
  port bind), and subscribe in **encoded-only** mode so the raw H.265 bitstream is forwarded
  to `ffmpeg -c copy` instead of a decoded subscribe that yields 0 frames. Clearer video
  diagnostics in the log.

## 0.5.0
- **Patrol:** new `patrol route` (select, filled from the robot) and `start patrol` (button).
  `auto (no route)` patrols without a saved route; a named route follows it. Routes are
  created in the EBO HOME app.

## 0.4.x
- Full command catalog exposed as entities: **sleep**, **say** (TTS), **volume**, **return to
  base**, plus a raw `ebo_air2/cmd` channel to send any opcode (for automations / AI).
- **Clean shutdown** (no more "error" on stop; logs stay readable).
- Renamed add-on to **Enabot integration**; repository is now the multi-add-on
  **Playcolors.co** collection.
- Fixed **return to base** (correct opcode) and removed the invalid patrol/AI-tracking buttons
  (they need structured payloads — documented in COMANDI.md).

## 0.3.0
- Video off by default (Agora Python SDK can't receive the robot's H.265 at the time).

## 0.2.x
- Initial control + telemetry over the Enabot cloud (Agora RTM/RTC) with MQTT Discovery.
