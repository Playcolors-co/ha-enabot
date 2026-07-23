# EBO Air 2 — documentation

> ℹ️ **Tested devices.** So far this add-on has been tested **only on the Enabot EBO Air 2**.
> It may work with other EBO models that talk to the same Enabot cloud (EBO SE 2, Max, EBO X…),
> but that is **unverified** — if you try it on another model, feedback and issues are very
> welcome.
>
> ⚠️ **Independent, unofficial project.** Not affiliated with Enabot or ThroughTek/Agora. It
> interoperates with the Enabot cloud through reverse engineering, using **your own**
> credentials and device. Use at your own risk; it may break if Enabot changes their API.

## Configuration

| option | description |
|--------|-------------|
| `email` | your Enabot account email |
| `password` | Enabot password (stored only here, in HA) |
| `region` | account region (e.g. `GB`, `US`, `EU`) |
| `host` | regional cloud endpoint. Default is EU; US ≈ `ebox-us.enabotserverintl.com` |
| `robot_id` | `0` = auto-discovery. Set an id only if you have more than one robot |
| `video` | startup state of the **EBO camera** switch. `false` (default) = camera off at start, so the robot is **not** kept in video mode. You can turn it on anytime from the switch. |
| `video_encoded` | **experimental.** `true` makes the camera use the encoded-H.265 path (may crash the Agora SDK; if it does, the add-on auto-falls back to control-only). Leave `false`. |
| `audio` | **experimental.** `true` adds the robot's microphone audio to the camera stream (listen-only, AAC). Optional — leave `false` if you only want video. |
| `video_max_height` | downscale the re-encoded stream to this height to save CPU (native is ~1296p). `720` (default) is a good balance; `0` = keep native resolution. |
| `video_preset` | libx264 speed/quality preset: `ultrafast` (default, lowest CPU) … `fast`. Slower presets look a bit better but use more CPU. |
| `log_level` | add-on log verbosity: `info` (default, clean) shows key events; `debug` adds chatty lines (per-N-frames…); `warning` shows only problems. |
| `host_ip` | optional. The IP of your Home Assistant machine, used to build the RTSP camera URL. Leave empty to auto-detect; set it (e.g. `192.168.88.15`) if the URL shows `<HOME-ASSISTANT-IP>`. |

Your credentials stay in the add-on configuration (in HA) and are sent only to Enabot's
servers, exactly like the official app does.

## MQTT

The add-on requests the Supervisor `mqtt` service: it automatically picks up host, port and
credentials of the Home Assistant broker. Make sure the *Mosquitto broker* add-on and the
MQTT integration are enabled.

## Driving from automations / AI

Besides the buttons, you can publish an analog vector:

```yaml
service: mqtt.publish
data:
  topic: ebo_air2/move/vector
  payload: '{"ly":-50,"rx":20,"hold":1.5}'
```

- `ly` < 0 = forward, > 0 = back
- `rx` = rotation (< 0 left, > 0 right)
- `hold` = duration in seconds; when it expires the robot stops (watchdog)

Value scale is ~±100. The vector must be "held": the add-on retransmits it at 10 Hz until
`hold` expires or a new command arrives.

## More entities (v0.4)

Besides battery/wifi/charging/recording/laser/speed and the move buttons, the add-on now
exposes: **sleep** (switch), **say** (text — the robot speaks what you type), **volume**
(number), and **return to base** (button — starts driving to the dock; only works when the
robot is *not* already charging).

### Patrol

- **patrol route** (select) — lists the patrol routes saved in the EBO HOME app, plus
  `auto (no route)`. The list is fetched from the robot at start-up.
- **start patrol** (button) — starts patrolling: with `auto (no route)` the robot does a
  free patrol (no route needed); with a named route it follows that saved route.

There is **no dedicated "stop patrol"** command in the robot's protocol — to interrupt a
patrol, just send any movement (e.g. the *stop* button). Routes are **created in the EBO
HOME app** (the add-on can only list and start them).

> **AI tracking** stays raw-only: it is interactive (you pick the subject, `{mode,
> trackTarget}`). Trigger it via the raw command channel below — see [COMANDI.md](COMANDI.md).

## Full command catalog + raw channel (AI)

The robot understands many more commands than there are entities. The topic **`ebo_air2/cmd`**
accepts a raw command for an automation or an AI agent:

```yaml
service: mqtt.publish
data:
  topic: ebo_air2/cmd
  payload: '{"id": 103501, "data": {"userId": "<yourUserId>", "text": "hello"}}'
```

The complete opcode catalog (movement, motion presets, voice, TTS, camera, eyes emoji,
scheduling, system…) is in [COMANDI.md](COMANDI.md). Commands marked *(moves)* drive the
robot — use them only when you can see it.

## Video (camera) — decoded path (v0.9)

> **Status (v0.9.0):** new approach. The SDK's *encoded* frame path segfaults for H.265, but
> the SDK **can decode H.265 to raw YUV**. So the add-on now subscribes to the **decoded**
> video (`register_video_frame_observer`, `auto_subscribe_video=1`), takes the YUV frames and
> **re-encodes them to H.264 with ffmpeg**, then serves RTSP. If the robot publishes and the
> SDK decodes, you'll see `[video] first decoded frame WxH` and `N frames received` in the
> log — then the camera works. Enable with `video: true` and the **EBO camera** switch.

### The camera switch

The **EBO camera** switch controls the video. It is **off by default**: the add-on stays in
RTC only for control (so commands work), but it does **not** subscribe to the robot's video —
which is what keeps the robot in "video mode". Turn the switch **on** only when you want the
stream; turn it **off** to let the robot leave video mode (saves battery, more privacy).

When you turn it on, the add-on subscribes to the robot's Agora video and republishes it as
**RTSP** on port **8554**. The exact URL is shown:
- in the **EBO camera URL** sensor (e.g. `rtsp://192.168.88.15:8554/ebo`), and
- in the add-on **log** (`[video] ON — … Camera stream: rtsp://…`).

To see it in Home Assistant:
1. Turn the **EBO camera** switch **on**.
2. **Settings → Devices & Services → + Add Integration → Generic Camera**
3. Stream URL = the value of the **EBO camera URL** sensor (`rtsp://<HA-IP>:8554/ebo`).
4. Leave the rest at defaults → Submit.

The stream is passed through without transcoding (`-c copy`). Check the log for
`[video] N frames received` to confirm frames are actually flowing (with the current SDK they
usually are **not**, for H.265 — see the status note above).

## Known limitations

- **amd64 only** (Agora SDK is x86_64).
- **Video** requires the robot to publish its stream; if the log shows 0 frames, the
  robot may only stream on demand (open an issue).
- One control client at a time: while the add-on is active, the EBO HOME app on the same
  account may be disconnected from control.
- Depends on Enabot's cloud API: a change on their side may require an update.

## Troubleshooting

- **"login failed"**: check email/password and the correct `region`/`host`.
- **No entities in HA**: make sure MQTT (Mosquitto + integration) is running.
- **Robot does not respond to commands**: make sure no other session (the app) is
  controlling the robot at the same time.

## Support

This is a free, independent project. If it's useful to you, you can support the work:

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95&slug=scattolacom&button_colour=FFDD00&font_colour=000000&font_family=Lato&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/scattolacom)

☕ **[buymeacoffee.com/scattolacom](https://www.buymeacoffee.com/scattolacom)**
