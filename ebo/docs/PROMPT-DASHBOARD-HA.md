# Prompt to give to the Claude that configures Home Assistant

Copy everything that follows and paste it to the Claude that manages your Home Assistant.

---

You are the assistant that configures my Home Assistant. I have an **Enabot EBO Air 2** robot
(a camera on wheels) integrated via a custom add-on that acts as a cloud→MQTT bridge. The add-on
exposes the robot as **MQTT Discovery entities** under a single device called **"EBO Air 2"**
(all entities have a name starting with `EBO …`). The MQTT prefix is `ebo`.

I have **two tasks** for you:
1. **Generate/update a complete Lovelace dashboard** for the robot.
2. **Test that every control actually works**, using the `mqtt.publish` service to
   send commands and observing the status entities / the robot's behavior.

## How it works
- The entities appear on their own via MQTT Discovery: open them from **Settings → Devices →
  "EBO Air 2"**. You don't need to create them by hand.
- Every **control** has an MQTT *command topic*: you can operate it from the entity **or**
  by publishing to the topic with the `mqtt.publish` service (handy for automated tests).
- The robot's **real state** arrives on the retained topic `ebo/state` (a JSON) and is already
  mapped into the sensor entities. Many "stateful" controls (video quality, volumes, speed…)
  update when the robot confirms → that's how you verify a command took effect.
- The **live video** is RTSP: there's an `EBO camera URL` sensor with the URL
  (`rtsp://<IP-HA>:8554/ebo`). First turn on the **EBO camera** switch, then use a
  *Generic Camera*/*WebRTC* card with that URL.

## CONTROLS (command topic → payload)
To test via the service, example:
`service: mqtt.publish` with `topic: ebo/laser/set`, `payload: "on"`.

### Movement (WARNING: these move the robot — supervise, keep it away from stairs/edges)
| Entity | Topic | Payload | Notes |
|---|---|---|---|
| Buttons **EBO forward/back/left/right/stop** | `ebo/move/<forward\|back\|left\|right\|stop>` | any | soft step |
| (advanced) continuous vector | `ebo/move/vector` | `{"lx":0,"ly":-50,"rx":0,"ry":0,"hold":0.6}` | ly<0 forward, rx turns; stops on its own after `hold`s |
| (advanced) joystick | `ebo/joystick` | `{"x":0.5,"y":1.0}` | x=steering (right+), y=forward(+), range −1..1 |
| Number **EBO rotate** | `ebo/rotate/set` | `-180`…`180` | rotates by N degrees |
| Number **EBO speed** | `ebo/speed/set` | `1`…`100` | movement speed (stateful) |
| Select **EBO move mode** | `ebo/move_mode/set` | `Mode 1\|Mode 2\|Mode 3` | drive mode (stateful) |
| Button **EBO return to base** | `ebo/dock` | any | returns to charge (no-op if already docked) |
| Button **EBO start patrol** | `ebo/patrol/start` | any | starts patrol on the selected route |
| Select **EBO patrol route** | `ebo/patrol/route/set` | a name from the list | “auto (no route)” = no route |

### Camera / media (safe, no movement)
| Entity | Topic | Payload |
|---|---|---|
| Switch **EBO camera** | `ebo/camera/set` | `on`/`off` (turns on the RTSP stream) |
| Select **EBO video quality** | `ebo/video_quality/set` | `Low\|Medium\|High` (stateful) |
| Select **EBO image style** | `ebo/image_style/set` | `Standard\|Vivid\|Soft` (stateful) |
| Select **EBO shoot mode** | `ebo/shoot_mode/set` | `Normal\|Wide\|Follow` (stateful) |
| Switch **EBO laser** | `ebo/laser/set` | `on`/`off` (laser pointer) |
| Number **EBO volume** | `ebo/volume/set` | `0`…`100` (speaker volume) |
| Number **EBO talkback volume** | `ebo/talkback_volume/set` | `0`…`100` (stateful) |
| Text **EBO say** | `ebo/say` | text → the robot speaks it (TTS) |
| Text **EBO ask AI** | `ebo/ai_ask` | question → the onboard AI answers |
| Switch **EBO cloud upload** | `ebo/upload_cloud/set` | `on`/`off` |
| Switch **EBO motion recording** | `ebo/sports_record/set` | `on`/`off` (stateful) |
| Switch **EBO auto-record calls** | `ebo/call_rec/set` | `on`/`off` (stateful) |

### Expressions / autonomy / AI (best-effort — verify carefully)
| Entity | Topic | Payload | Effect |
|---|---|---|---|
| Select **EBO eyes** | `ebo/eyes/set` | `Dynamic\|Clock\|Custom` | eyes display |
| Switch **EBO roaming** | `ebo/roaming/set` | `on`/`off` | autonomous patrol |
| Button **EBO AI track** | `ebo/ai_track` | any | follows a subject |
| Number **EBO play motion** | `ebo/motion/set` | id `0`…`30` | runs a preset choreography |
| Number **EBO play voice** | `ebo/voice/set` | id `0`…`30` | plays a preset voice |
| Switch **EBO sleep** | `ebo/sleep/set` | `on`/`off` | sleep/wake |

### System
| Entity | Topic | Payload |
|---|---|---|
| Switch **EBO connected** | `ebo/connected/set` | `on`/`off` — OFF disconnects from the cloud (the robot can sleep) |

### RAW channel (any command from the full catalog)
Topic `ebo/cmd`, payload `{"id":<opcode>,"data":{...}}`. The catalog of the 112 opcodes is in
`docs/COMMANDS-APK.md` of the add-on. TTS example: `{"id":103501,"data":{"userId":"<id>","text":"hello"}}`.

## SENSORS (read-only — put them in gauge/badge/entity)
`EBO battery` (%), `EBO charging`, `EBO docked`, `EBO wifi` (dBm), `EBO WiFi SSID`, `EBO IP`,
`EBO SD card` (present), `EBO SD free`/`EBO SD total` (GB), `EBO storage free` (GB),
`EBO recording`, `EBO guard mode`, `EBO activity` (moving/charging/AI-track/call/upgrade),
`EBO firmware (camera)`, `EBO firmware (MCU)`, `EBO camera URL`.

## DASHBOARD — what I want
A view with, from top to bottom:
1. **Video card** (Generic/WebRTC camera) with the RTSP URL; above or next to it the **EBO camera** switch.
2. **D-pad / joystick** for movement (use the move buttons, or a joystick card that publishes to
   `ebo/move/vector`), with the **EBO speed** slider and the **EBO rotate** number.
3. **Camera panel**: quality/style/shoot selects, laser switch, volume sliders, “say”/“ask AI” text.
4. **Expressions/AI panel**: eyes, roaming, AI track, play motion/voice.
5. **Status**: battery gauge + charging/docked/wifi badges, SD/storage, current activity, firmware versions.
Group cleanly (sections/`vertical-stack`), sensible icons, and hide the diagnostic
entities in a separate section.

## TEST — procedure for each
For **each control** run it and note PASS/FAIL:
- **"Stateful" controls** (speed, quality/style/shoot/move mode, volumes, sports/call rec):
  send a value via `mqtt.publish`, wait 2–3 s, **verify that the corresponding entity reflects
  the new value** (the robot confirms it on the state topic). If it comes back with the value, the robot
  accepted it → PASS.
- **Actions/movement** (buttons, rotate, dock, patrol, ai_track, play motion): run them with the
  robot **in a safe area and under supervision**; verify the physical effect (it moves/turns/speaks)
  or the change of the **EBO activity** sensor. Always send `stop` after a movement.
- **Always safe**: laser, say, volumes, eyes, video quality — testable without risk.
- **Best-effort** (eyes, roaming, ai_track, ai_ask, play motion/voice): if a command **produces no
  effect**, report it to me with: control name + payload sent + what did (not) happen. It'll be needed to
  fix the payload in the add-on (their formats were reconstructed from reverse-engineering).

At the end give me a **PASS/FAIL table** of all controls, and the updated dashboard.
Do not send **movement** commands if the robot isn't in a safe place: ask me first.

---
