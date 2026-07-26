# EBO — "app-like" view: video with overlaid controls

Reproduces the app's full-screen screen: the **camera stream** with a **D-pad** on top to move the
robot, plus laser, return to base, "talk", speed and status.

## Prerequisites (Home Assistant side)
1. **Add-on started** with `video: true` (and, if you want audio, `audio: true` / `talk: true`).
2. **A camera entity** pointing at the add-on's RTSP stream. The best way is **go2rtc**
   (included in HA OS) so you also get **audio** and low latency:
   - In `configuration.yaml` (or in the go2rtc config) add the stream:
     ```yaml
     go2rtc:
       streams:
         ebo: rtsp://<IP-ADD-ON>:8554/ebo
     ```
     and create the generic camera that uses it, **or** use the WebRTC card (`custom:webrtc-camera`)
     with `url: ebo`.
   - Quick alternative (video only, no audio): **Settings → Devices → Add
     integration → Generic Camera**, Stream URL = `rtsp://<IP-ADD-ON>:8554/ebo`.
   Name the resulting entity, e.g., `camera.ebo`. **Replace `camera.ebo`** below with yours.
3. Turn on the **EBO camera** switch (or `mqtt.publish` to `ebo_air2/camera/set` = `on`): only then
   does the bridge subscribe to the robot's video.

## Card A — `picture-elements` (native, no extra component)
Paste as a new card (YAML mode). The arrows move the robot with a soft "step" that stops on its own
(`hold`), like a repeatable tap. **Replace `camera.ebo`** and the status `entity` values
with the real IDs (find them in Settings → Devices → "EBO Air 2").

```yaml
type: picture-elements
camera_image: camera.ebo
camera_view: live
elements:
  # ---------- MOVEMENT (D-pad, bottom left) ----------
  - type: icon
    icon: mdi:chevron-up
    title: Forward
    style: {left: 15%, top: 62%, color: white, "--mdc-icon-size": 44px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/move/vector, payload: '{"lx":0,"ly":-55,"rx":0,"ry":0,"hold":0.8}'}
  - type: icon
    icon: mdi:chevron-down
    title: Backward
    style: {left: 15%, top: 90%, color: white, "--mdc-icon-size": 44px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/move/vector, payload: '{"lx":0,"ly":55,"rx":0,"ry":0,"hold":0.8}'}
  - type: icon
    icon: mdi:chevron-left
    title: Turn left
    style: {left: 5%, top: 76%, color: white, "--mdc-icon-size": 44px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/move/vector, payload: '{"lx":0,"ly":0,"rx":-65,"ry":0,"hold":0.6}'}
  - type: icon
    icon: mdi:chevron-right
    title: Turn right
    style: {left: 25%, top: 76%, color: white, "--mdc-icon-size": 44px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/move/vector, payload: '{"lx":0,"ly":0,"rx":65,"ry":0,"hold":0.6}'}
  - type: icon
    icon: mdi:stop-circle-outline
    title: Stop
    style: {left: 15%, top: 76%, color: "#ff5252", "--mdc-icon-size": 40px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/move/stop, payload: ""}

  # ---------- ACTIONS (right column) ----------
  - type: icon
    icon: mdi:laser-pointer
    title: Laser
    style: {right: 4%, top: 60%, color: white, "--mdc-icon-size": 34px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/laser/set, payload: "on"}
    hold_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/laser/set, payload: "off"}
  - type: icon
    icon: mdi:home-import-outline
    title: Return to base
    style: {right: 4%, top: 72%, color: white, "--mdc-icon-size": 34px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/dock, payload: ""}
  - type: icon
    icon: mdi:camera-iris
    title: Snapshot
    style: {right: 4%, top: 84%, color: white, "--mdc-icon-size": 34px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/cmd, payload: '{"id":102101,"data":{}}'}   # optional/best-effort

  # ---------- SPEED (top right) ----------
  - type: icon
    icon: mdi:speedometer-slow
    title: Slower
    style: {right: 16%, top: 8%, color: white, "--mdc-icon-size": 28px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/speed/set, payload: "40"}
  - type: icon
    icon: mdi:speedometer
    title: Faster
    style: {right: 4%, top: 8%, color: white, "--mdc-icon-size": 28px}
    tap_action:
      action: call-service
      service: mqtt.publish
      data: {topic: ebo_air2/speed/set, payload: "95"}

  # ---------- STATUS (top left) — replace with your own entity ids ----------
  - type: state-label
    entity: sensor.ebo_battery
    prefix: "🔋 "
    style: {left: 3%, top: 6%, color: white, font-weight: bold}
  - type: state-label
    entity: sensor.ebo_activity
    style: {left: 3%, top: 12%, color: white}
```

### Notes
- The D-pad uses `ebo_air2/move/vector` with `hold`: each tap moves a little and **stops on its own**.
  Want longer/shorter steps? Change `hold` (seconds) or the `ly`/`rx` values (−100..100).
- **Safety:** these commands *move* the robot. Use them only with the robot in a safe area and
  under your supervision (never remotely if you don't know what's around it).
- Also want to **talk** from the video? Add an `mdi:microphone` icon that opens an `input_text`
  with the audio URL and publishes to `ebo_air2/talk` (requires `talk: true`).

## Card B — analog joystick (faithful to the app, drag)
Requires a custom card. If you want it I'll write it for you: a JS file (Lovelace resource) that draws
a **draggable joystick** on top of the video and publishes continuously to `ebo_air2/move/vector`
(release → stop), exactly like the app's analog pad. Just say "make card B".
