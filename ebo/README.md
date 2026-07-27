# EBO for Home Assistant (unofficial)

Manage your **Enabot EBO robots** from Home Assistant — **all the robots on your account**,
not just one. The add-on signs into the Enabot cloud with **your own credentials** (the same
as the EBO HOME app), discovers every robot, and keeps a session per robot alive by itself.
No phone, no emulator.

It gives you three things:

1. **A sidebar panel** (like Zigbee2MQTT): one place to see every robot — live preview, battery,
   Wi-Fi, quick controls (camera, wake/standby, laser, dock), per-robot settings, **pair a new
   robot** (QR, no phone), and **remove a robot** from the account.
2. **Home Assistant entities** for each robot: battery, Wi-Fi, charging, camera on/off, video
   quality, speed, volume, eyes, dock/laser/wake/standby, and a **live camera**. These come
   either over **MQTT discovery** (default) or from the companion **Enabot integration** (HACS)
   — toggle `expose_mqtt` in the panel.
3. **The video** of each robot as a Home Assistant camera (RTSP → HA's stream/go2rtc → WebRTC).

## Setup
1. In the add-on **Configuration** tab, enter only your **email** and **password** (the Enabot
   account) — plus the two **app crypto keys** (`payload_key` / `sign_key`), which this project
   does not ship (see DOCS → "App crypto keys"). Everything else is managed from the **panel**.
2. Start the add-on, then open the **panel** (the add-on's *Open Web UI* / sidebar entry).
3. For per-robot **device + live camera** entities, install the companion **Enabot integration**
   from HACS (custom repository `Playcolors-co/ha-enabot-integration`).

> The "Network" ports (camera streams + the integration's data API) are internal plumbing —
> you don't need to change them.

## Supported models
- **Cloud family (this add-on):** EBO **Air 2** (verified), Air 2 Plus / Air 2S / Mini, **EBO X**,
  **Max** — all the models that use the EBO HOME app + Enabot Agora cloud. Non-Air 2 models are
  **experimental** (same cloud; some model-specific commands may differ). All robots on your
  account are discovered automatically.
- **EBO SE (LAN, TUTK/Kalay):** a different, local-only stack — **not** this add-on. Use the
  community bridge **[ebo-se-lan-bridge](https://github.com/lilium360/ebo-se-lan-bridge)** (on a
  Raspberry Pi); it gives Home Assistant an RTSP camera + MQTT entities + its own panel. It
  coexists with this add-on. (We can't bundle it — it needs proprietary ARM libraries.)

> ⚠️ **Independent, unofficial project.** Not affiliated with Enabot or ThroughTek/Agora. It
> interoperates with the Enabot cloud through reverse engineering, using **your own** account and
> devices. Use at your own risk; it may break if Enabot changes their API.
