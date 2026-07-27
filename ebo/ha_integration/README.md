# EBO for Home Assistant (unofficial)

Companion to the **EBO add-on**. On Home Assistant OS / Supervised it's the **only thing you
configure**: you enter your Enabot account here and the integration writes it into the add-on and
starts it for you — no need to open the add-on's Configuration tab. Each robot then becomes a
**device named after the robot** with a **live camera** (RTSP → HA `stream`/go2rtc → WebRTC) and
native entities.

> ⚠️ Independent, unofficial project. Not affiliated with Enabot or ThroughTek/Agora. Uses **your
> own** account and devices. Use at your own risk.

## Install (HACS)
1. Install the **EBO add-on** from the repo `https://github.com/Playcolors-co/ha-enabot`
   (you don't need to fill its Configuration — the integration does it).
2. HACS → Integrations → ⋮ → Custom repositories → add `Playcolors-co/ha-enabot-integration` as
   **Integration** → install → restart Home Assistant.
3. **Settings → Devices & Services → + Add Integration → EBO** → enter your Enabot **email +
   password** and the two **app keys** (`payload_key` / `sign_key`, constants from the EBO HOME
   app — see the add-on docs → "App crypto keys"). That's it.

The integration provisions and starts the add-on; your robots appear automatically, each with its
own device + camera. Update your password later from the integration's **Configure** (it
re-provisions the add-on).

## Why the add-on is still needed

Live video and real-time control ride on Enabot's **Agora** cloud (RTC for video, RTM for
commands), which needs the native **amd64 Agora SDK + ffmpeg** — that can't live inside a HACS
integration (which must run on any architecture). So the add-on is the engine; this integration is
the Home Assistant face of it, and now also its configuration surface.

## No Supervisor? (HA Container / Core)

Add-ons don't exist there, so run the engine yourself (its Docker image) and use **Add Integration
→ EBO → manual**: enter the robot's **RTSP URL** and, for native entities, the engine's **API URL +
token**.

## How robot discovery works

The add-on publishes a retained MQTT message per robot on `ebo/discovery/<node>` (name, serial,
MAC, model, RTSP URL, and the data-API URL + token). This integration turns each into a device with
a camera and native entities (battery, Wi-Fi, charging, online, camera switch, dock/laser/wake/
standby buttons, speed & volume numbers, video-quality/image-style/eyes selects). Set the add-on
option `expose_mqtt: false` only if you add robots purely manually.
