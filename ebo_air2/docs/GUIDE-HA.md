# Practical HA guide — smooth video, audio, app-like controls

Answers the concrete questions: how to enable audio send/receive, how to make the video
smoother, and how to get an interface with the camera + control pads like in the app.

---

## 1. What to enable (add-on toggles)

These are not runtime buttons: they are add-on **options**. Settings → EBO Air 2 →
Configuration. After changing them, **restart the add-on**.

| Option | What it does | Status |
|---|---|---|
| `video: true` | Publishes the RTSP stream → camera in HA | ✅ works |
| `audio: true` | **Receive**: hear the robot's microphone in the camera | ⚠️ best-effort (see below) |
| `talk: true` | **Send**: YOU send audio to the robot's speaker | ✅ works (separate channel) |
| `audio_codec: 8` | Microphone codec (leave it at 8) | — |

### The truth about receive audio (`audio: true`)
Decoding works, but **the robot keeps the microphone off** and opens it **on its own,
unpredictably** (sometimes after a few minutes, sometimes never). In the app it's immediate because
it sends an internal command (RTM) that **I haven't captured yet**. Until I isolate it, in HA the
audio will only be heard **when the robot decides to open the mic**. In the log you'll honestly
see:
- `[audio] robot mic is OPEN — audio flowing` → at that moment you can hear it;
- `[audio] subscribed OK, but the robot's mic is still MUTED …` → not yet.

(v0.17.1 tried to force it with a silent track: **it didn't work** and falsely reported
"audio works". Removed in 0.17.2.)

### Talking to the robot (`talk: true`)
You send audio to the speaker by publishing a **URL or path** (anything ffmpeg can read)
to the MQTT topic `ebo_air2/talk`. Automation example: HA TTS → media URL → `mqtt.publish`.
```yaml
# Example: make the robot "speak" with HA's TTS voice
service: tts.speak
data:
  cache: true
  media_player_entity_id: media_player.any   # required by the service, not used by the robot
  message: "Hi, I am home!"
target:
  entity_id: tts.google_translate_en   # or your TTS engine
# …then in a second step publish the generated URL to ebo_air2/talk.
```
Easiest way to try it right now: publish the URL of a public mp3 to `ebo_air2/talk` from
Developer Tools → MQTT.

> **Note:** this makes the robot **emit sound**. It's your action, but keep it in mind: it's a
> device in your home.

---

## 2. Smoother video (the part you notice most)

The add-on already streams with low-latency settings. The residual delay/stutter comes
**from the HA card**: the standard camera uses **HLS**, which buffers 1-3 seconds. The
solution is to play the video via **WebRTC** (sub-second). Two paths:

### A) HACS "WebRTC Camera" card (recommended, simpler)
1. HACS → Frontend → install **WebRTC Camera** (`AlexxIT/WebRTC`).
2. Add a manual card:
```yaml
type: custom:webrtc-camera
url: rtsp://YOUR-HA-IP:8554/ebo   # the URL the add-on shows in "EBO camera URL"
mode: webrtc                          # webrtc = low latency; mse as fallback
```
This card opens the stream in WebRTC directly → no HLS buffer.

### B) go2rtc built into HA (no HACS)
HA includes go2rtc. In `configuration.yaml` (or in the go2rtc file), add the source:
```yaml
go2rtc:
  streams:
    ebo:
      - rtsp://YOUR-HA-IP:8554/ebo
```
Then use a `generic` camera pointing at that stream: the card will show WebRTC when
possible.

> If you stay on the standard "Generic Camera", the video works but with the HLS delay.
> The real jump in smoothness comes from WebRTC (A or B).

---

## 3. "App-like" interface (camera + control pad)

Question: better to build it on HACS? **You don't need a custom HACS component** (that's a
project of its own). The practical approach is to compose **existing** cards. Two levels:

### Level 1 — maximum smoothness, controls below (recommended for actually driving)
Full WebRTC video + a row of controls below. Responsive and simple:
```yaml
type: vertical-stack
cards:
  - type: custom:webrtc-camera
    url: rtsp://YOUR-HA-IP:8554/ebo
    mode: webrtc
  - type: horizontal-stack
    cards:
      - type: button
        entity: button.ebo_forward
        name: ▲
      - type: button
        entity: button.ebo_stop
        name: STOP
  - type: horizontal-stack
    cards:
      - type: button
        entity: button.ebo_left
        name: ◄
      - type: button
        entity: button.ebo_back
        name: ▼
      - type: button
        entity: button.ebo_right
        name: ►
  - type: entities
    entities:
      - switch.ebo_camera
      - switch.ebo_laser
      - number.ebo_speed
      - button.ebo_return_to_base
```

### Level 2 — pad OVERLAID on the video ("full-screen app" look)
Use `picture-elements` with the buttons on top of the video. Note: in `picture-elements` the video
uses the standard camera path (a touch more latency than the pure WebRTC option). Ready-made YAML in
[`DASHBOARD-CONTROL-OVERLAY.md`](DASHBOARD-CONTROL-OVERLAY.md), which publishes the **movement
vector** to `ebo_air2/move/vector` (`{"lx":..,"ly":..,"hold":0.6}`) for continuous, joystick-style
control instead of jerky steps.

> Tip: **Level 1** for driving (responsiveness), **Level 2** if you want the aesthetic effect.
> You can also keep the WebRTC card for watching and a separate tab with the overlay.

---

## Honest summary

- **Video, movement, sensors, snapshot, patrol, eyes, TTS**: they work.
- **Smooth video**: achieved on the HA side with WebRTC (section 2). The add-on is already low-latency.
- **Talking to the robot** (`talk`): works, via `ebo_air2/talk`.
- **Hearing the robot** (`audio`): best-effort until I capture the microphone's RTM command.
  It's the only unreliable piece, and now the log tells you plainly, with no false "it works".
