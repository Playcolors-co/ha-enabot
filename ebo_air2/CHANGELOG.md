# Changelog — Enabot integration

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
