# EBO for Home Assistant (unofficial) — documentation

## Supported models

Enabot robots split into two very different families:

**Cloud family (this add-on)** — the models that use the **EBO HOME** app and the Enabot **Agora
cloud** (RTC/RTM). This add-on manages all of these from your account:

| Model | Status |
|---|---|
| **EBO Air 2** | ✅ verified |
| EBO Air 2 Plus / Air 2S / Mini | 🧪 experimental — same cloud/opcodes as the Air 2; core features (video, telemetry, move, sleep) should work, some model-specific commands may differ |
| EBO X / EBO Max | 🧪 experimental — same Agora cloud, more feature differences; expect some commands to need per-model tuning |

The add-on discovers **every robot on your account** and runs a bridge for each, so multiple/mixed
cloud models work together. Non-verified models are best-effort — feedback and issues are welcome.

**EBO SE (LAN, TUTK/Kalay)** — the SE is controlled **locally over LAN via TUTK/Kalay**, not the
Agora cloud, so it is **not** this add-on. Use the community bridge
**[ebo-se-lan-bridge](https://github.com/lilium360/ebo-se-lan-bridge)** (runs on a Raspberry Pi);
it gives Home Assistant an RTSP camera + MQTT entities + its own panel. It coexists with this
add-on. (We can't bundle it — it needs proprietary ARM libraries.)

> ⚠️ **Independent, unofficial project.** Not affiliated with Enabot or ThroughTek/Agora. It
> interoperates with the Enabot cloud through reverse engineering, using **your own** credentials
> and devices. Use at your own risk; it may break if Enabot changes their API.

## App crypto keys (required — not shipped)

Requests to the Enabot cloud must be signed/encrypted with two keys that are **constants embedded
in the official EBO HOME app**. To keep this project clean, **those keys are NOT included** — you
provide them yourself in the configuration:

- **`sign_key`** — the HMAC key for the request signature (`x-ebo-sign`).
- **`payload_key`** — the AES-128-GCM key for the login payload.

They are the same for everyone (app-level constants, not per-user secrets). A technically-inclined
user can read them from **their own copy of the EBO HOME app** (decompile the APK, or hook
`javax.crypto.Mac` / the AES cipher with Frida). Without them the add-on stops with a clear
message. This project does not distribute them.

## Configuration (only 4 fields)

Everything else is managed from the **panel** — the Configuration tab holds only the account +
keys:

| option | description |
|--------|-------------|
| `email` | your Enabot account email |
| `password` | your Enabot password (stays here, in your HA) |
| `payload_key` | the app's AES login key (see above) |
| `sign_key` | the app's HMAC signing key (see above) |

> 💡 **Prefer to configure from the integration?** Install the companion
> **[EBO integration](https://github.com/Playcolors-co/ha-enabot-integration)** (HACS) and enter
> the account there — it writes these four fields into the add-on and starts it for you, so you
> never touch this tab. Leave the fields blank if you go that route.

## The panel (sidebar)

The add-on serves a **web panel** (like Zigbee2MQTT) — open it from the add-on's *Open Web UI* or
the **EBO** sidebar entry. From one place you can:

- see **every robot** on your account: live preview, battery, Wi-Fi, online state;
- click a robot for its **detail page** with quick actions — **Camera on/off, ☀ Wake, 🌙 Standby,
  Laser, Dock** — and per-robot **settings** (video quality, image style, eyes, volume, speed,
  motion recording);
- **➕ Add robot**: pair a NEW cloud robot with a QR code, no phone needed (enter your Wi-Fi → the
  robot scans the QR → it joins and binds to your account);
- **🗑 Remove from account**: unbind a robot (with confirmation);
- **⚙ Settings**: all the operational add-on options (region/host, video on/off + quality/fps/
  bitrate/preset, audio, talk, log level, and **Expose entities over MQTT**). Saving restarts the
  add-on. These are stored in the add-on, not in the Configuration tab.

## Entities & camera

Each robot's entities (battery, Wi-Fi, charging, camera switch, wake/standby/laser/dock, video
quality, speed, volume, eyes…) appear in Home Assistant in one of two ways:

- **MQTT discovery** (default, `expose_mqtt: on`) — needs the *Mosquitto broker* add-on + the MQTT
  integration. Entities appear automatically.
- **Native integration** — install the companion **[EBO integration](https://github.com/Playcolors-co/ha-enabot-integration)**
  from HACS: it creates a **device per robot** (named after the robot) with a **live camera** and
  all entities, talking to the add-on directly. Set `expose_mqtt: off` to let it own everything.

The **video** is a low-latency RTSP stream (H.265 → H.264) on port **8554**; via the native
integration or go2rtc it plays as **WebRTC**. Turning the camera on **wakes** the robot from
standby, like the app.

## Driving from automations / AI

Besides the buttons, publish an analog movement vector:

```yaml
service: mqtt.publish
data:
  topic: ebo_air2/move/vector
  payload: '{"ly":-50,"rx":20,"hold":1.5}'
```

- `ly` < 0 = forward, > 0 = back · `rx` = rotation (< 0 left, > 0 right) · `hold` = seconds (the
  robot stops when it expires). Scale ≈ ±100; the vector is re-sent at 10 Hz until `hold` expires.

The full opcode catalog (motion presets, voice, TTS, camera, eyes, scheduling, system…) is in
[COMANDI.md](COMANDI.md), usable via the raw `ebo_air2/cmd` topic. Commands that move the robot
should only be used when you can see it.

## Known limitations

- **amd64 only** (the Agora SDK is x86_64).
- **Listen (robot microphone)** is best-effort: the robot opens its mic on its own, unpredictably
  — this can't be forced from the server SDK. **Talk** (your audio → robot speaker) works.
- One control session per account: while the add-on is active, the EBO HOME app on the same
  account may be disconnected, and vice versa.
- Depends on Enabot's cloud API — a change on their side may require an update.

## Troubleshooting

- **Add-on stops: "crypto keys not shipped"** → set `payload_key` and `sign_key` (see above).
- **"login failed"** → check email/password and the region/host (panel → ⚙ Settings).
- **No entities** → with `expose_mqtt: on`, ensure Mosquitto + the MQTT integration are running;
  or install the native integration and set `expose_mqtt: off`.
- **Robot doesn't respond** → make sure the EBO HOME app isn't controlling the same account.
- **Camera blank in the panel** → the preview is a smooth snapshot; for full live video use the
  camera entity (WebRTC) from the native integration.

## Support

Free, independent project. If it's useful, an optional coffee is appreciated — never required:

☕ **[buymeacoffee.com/scattolacom](https://www.buymeacoffee.com/scattolacom)**
