# EBO Air 2

Control your **Enabot EBO Air 2** from Home Assistant: battery, wifi, laser, speed,
movement (forward/back/left/right) and a "vector" channel meant for driving it from an
automation or an AI agent.

It works with **your own Enabot credentials** (the same as the EBO HOME app): the add-on
signs into the Enabot cloud, discovers your robot, and keeps the session alive by itself.
No phone, no emulator.

> ⚠️ **Independent, unofficial project.** Not affiliated with Enabot or ThroughTek/Agora.
> It interoperates with the Enabot cloud through reverse engineering, using your own
> credentials and device. Use at your own risk; it may break if Enabot changes their API.

> ℹ️ **Tested devices.** So far this has been tested **only on the Enabot EBO Air 2**. It may
> work with other EBO models on the same cloud (SE 2, Max, EBO X…), but that is unverified —
> feedback and issues are welcome.

## Requirements

- Home Assistant **OS** or **Supervised** (add-ons require the Supervisor)
- **amd64** architecture (the Agora SDK is x86_64 only — e.g. HAOS as a VM on Proxmox/NUC ✓)
- An **MQTT** broker in HA (the *Mosquitto broker* add-on) and the **MQTT** integration enabled

## Installation

1. Add the repository (see the [repository README](../README.md)):
   `https://github.com/Playcolors-co/ha-enabot`
2. Find **EBO Air 2** in the store and install it.
3. In the add-on's **Configuration** tab set:
   - `email` / `password` — your Enabot credentials
   - `region` — your account region (e.g. `GB`, `US`, `EU`)
   - `host` — keep the default if you are in Europe; US/other regions may need to change it
     (e.g. `ebox-us.enabotserverintl.com`)
   - `robot_id` — leave `0`: it is discovered automatically (set a value only if you have
     more than one robot on the account)
4. **Start** the add-on. The entities appear in Home Assistant via MQTT Discovery, under the
   **EBO Air 2** device.

## Entities

| entity | type |
|--------|------|
| battery, wifi, SD space | sensor |
| charging, recording | binary_sensor |
| laser, sleep, camera, connected, motion recording, auto-record calls, cloud upload | switch |
| camera URL (RTSP link) | sensor |
| speed (1–100), volume (0–100), talkback volume (0–100) | number |
| say (text-to-speech) | text |
| patrol route | select |
| forward / back / left / right / stop, return to base, start patrol | button |

Plus the MQTT topic `ebo_air2/move/vector` which accepts `{"ly":-50,"rx":0,"hold":1.0}`
for continuous analog control, and `ebo_air2/cmd` which accepts `{"id":<opcode>,"data":{…}}`
to send **any** command from the [full catalog](COMANDI.md) — built for automations or an
AI agent.

## How it works / technical notes

The robot talks to the cloud over **Agora RTM** (commands/telemetry, JSON) + **RTC**
(presence). The add-on replicates the app flow: encrypted login → Agora session → control.
Movement is retransmitted at 10 Hz with a **watchdog** (if the add-on stops, the robot
stops). Details in [DOCS.md](DOCS.md).

## Support

Free, independent project. If it's useful to you, you can support the work:

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95&slug=scattolacom&button_colour=FFDD00&font_colour=000000&font_family=Lato&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/scattolacom)

## License

Original code under **MIT** (see [LICENSE](../LICENSE)). No proprietary Enabot/ThroughTek
component is included or redistributed.
