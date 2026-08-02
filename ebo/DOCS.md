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

They are the same for everyone (app-level constants, not per-user secrets), and you read them from
**your own copy of the EBO HOME app**. Without them the add-on stops with a clear message.

### How to get them (~10 minutes, once)

1. **Get the APK of the EBO HOME app off your phone** (package `com.enabot.ebox.intl`):
   - with an extractor app — [App Manager](https://github.com/MuntashirAkon/AppManager/releases)
     (also on [F-Droid](https://f-droid.org/packages/io.github.muntashirakon.AppManager/)) → find
     *EBO HOME* → *Export APK*; **or**
   - over USB with [adb](https://developer.android.com/tools/releases/platform-tools):
     `adb shell pm path com.enabot.ebox.intl` then `adb pull <the base.apk path> ebo.apk`
2. **Open it with jadx** — download from
   [github.com/skylot/jadx/releases](https://github.com/skylot/jadx/releases) (needs Java, e.g.
   [Adoptium](https://adoptium.net)). GUI: open `ebo.apk`. CLI: `jadx -d ebo-src ebo.apk`.
3. **Open the class** `com.enabot.lib_ebo.netWork.ServerEncryptHelper`
   (in jadx-gui: Ctrl/Cmd+Shift+F → search `ServerEncryptHelper`). Near the top it holds **two
   16-character string constants** (the field names are obfuscated, e.g. `f24161b`):
   - the one fed to the **AES cipher** for the request body → **`payload_key`**
   - the one used to build the **`x-ebo-sign` signature** (SHA-256) → **`sign_key`**
4. **Paste both** into this add-on's Configuration tab. If login fails with a signature error, you
   probably swapped them — try the other way round.

📖 Full version with more detail:
<https://github.com/Playcolors-co/ha-enabot/blob/main/ebo/docs/GET-APP-KEYS.md>

Why we don't just bundle them (even encrypted): whatever decrypts them would have to ship with the
add-on too, so it would protect nothing — and it would mean redistributing someone else's secrets.
Keeping them user-supplied is both safer and legally cleaner: you use your own app's keys, for your
own robot. Please don't post the keys publicly.

## Configuration (only 4 fields)

Everything else is managed from the **panel** — the Configuration tab holds only the account +
keys:

| option | description |
|--------|-------------|
| `email` | your Enabot account email |
| `password` | your Enabot password (stays here, in your HA) |
| `payload_key` | the app's AES login key (see above) |
| `sign_key` | the app's HMAC signing key (see above) |

A fifth option, `api_token`, is auto-generated (the companion integration uses it to read the
add-on's data API) — you don't need to set it.

> 💡 **The companion integration installs itself.** On start the add-on copies its integration into
> Home Assistant. Restart HA once, then **Settings → Devices & Services → + Add Integration →
> "EBO"** — it finds your robots automatically. No HACS, nothing to type.

## The panel (sidebar)

The add-on serves a **web panel** (like Zigbee2MQTT) — open it from the add-on's *Open Web UI* or
the **EBO** sidebar entry. From one place you can:

- see **every robot** on your account: live preview, battery, Wi-Fi, online state;
- click a robot for its **detail page** with quick actions — **Camera on/off, ☀ Wake, 🌙 Standby,
  Laser, Dock** — and per-robot **settings** (video quality, image style, eyes, volume, speed,
  motion recording);
- drive with a **joystick** (detail page) or a full-screen **dual-stick / joystick** view (choose in
  the full-screen ⚙, plus Laser / return-to-base / speed there);
- **➕ Add robot**: pair a NEW cloud robot with a QR code, no phone needed (enter your Wi-Fi → the
  robot scans the QR → it joins and binds to your account);
- **🗑 Remove from account**: unbind a robot (with confirmation).

**Where settings live**
- **Per-robot** (video quality, image style, eyes, volume, speed, motion rec): on the **robot's
  detail page** — they're sent to the robot.
- **Account & add-on** (email/password, region/host, robot id, the advanced audio/video processing
  options, log level): in the add-on's **Configuration tab** (Settings → Add-ons → EBO →
  Configuration). The defaults are good — you normally only set your login.

## Entities & camera — native, no MQTT

Each robot is a **distinct device under the EBO integration** (named after the robot), with a
**live camera** and all entities (battery, Wi-Fi, charging, camera switch, wake/standby/laser/dock,
video quality, speed, the two volumes, eyes…). Settings you set once — the volumes, image style,
eyes, driving mode, collision avoidance — live in the device's **Configuration** section, so the
main list stays the controls you use while driving. **No MQTT is involved** in your Home Assistant:
the integration reads everything from the add-on's HTTP API. (Internally the add-on uses a private broker on
localhost as glue between its processes — you never see it, and no Mosquitto is required.)

The **video** is a low-latency RTSP stream (H.265 → H.264) on port **8554**; the integration plays
it as **WebRTC** via HA's stream/go2rtc. Turning the camera on **wakes** the robot from standby,
like the app.

## Fluid video & smooth driving (important)

The robot streams **H.265**, which browsers can't decode directly, so the add-on **re-encodes to
H.264** in real time on your Home Assistant host. Two settings decide whether that stays fluid — the
defaults are already correct, but if the video is **choppy or laggy**, check these:

- **Encoder preset = `ultrafast`** (panel → ⚙ Settings → *Video encoder preset*). This is the default
  and the **recommended** value. Heavier presets (`fast`, `faster`, …) look nicer but are too slow to
  re-encode in real time on typical HA hosts (especially low-core NUCs/mini-PCs) — the encoder falls
  behind, frames pile up, and the video lags by **seconds**. Keep it on `ultrafast`.
- **Video quality**: the drive view now picks this **for you** — **High (~720p)** when the browser
  gets the fluid WebRTC path (on your LAN), **Low** when it falls back to HLS (from remote). You can
  still override it in the fullscreen ⚙ → Camera.
  At **High** the robot sends **2304×1296 (3 MP)** — far too much to re-encode live on most hosts, so
  the picture lags. At **Low (848×480)** the encoder keeps up at a full ~25 fps with ~200 ms latency.
  - You don't have to think about this while **driving**: the panel's **fullscreen drive view
    automatically switches to Low** for smooth, low-latency control and **restores your quality** when
    you exit. High is fine for occasional still viewing; just expect lag if you watch it live at High.

Rule of thumb: **`ultrafast` preset + `Low`/`Medium` quality = fluid**. If you have a powerful host you
can raise the quality; if it's still choppy, lower it.

## Video connection: fluid on the LAN, slower from remote (important)

The fullscreen "drive" view has **two ways** to reach the video, and the panel picks automatically. The
current one is shown in the **top-bar badge** (green **WebRTC** or amber **HLS**), and on the robot page
a line tells you what you'll get before you open fullscreen:

- **On your home network (LAN) → WebRTC**, ~200 ms latency: **fluid, good for actually driving.** The
  browser connects directly to the add-on for the media.
- **From outside your home (mobile data, Nabu Casa, a reverse proxy, your own domain) → HLS**, ~1 s
  latency: the panel shows an **amber "HLS" badge and a warning** that the video is delayed. It's fine
  to **watch and command gently**, but **not** for reactive driving.

**Why remote can't be as fluid:** WebRTC's video needs a *direct* connection to the add-on on a UDP
port, which your home router doesn't expose to the internet (and remote-HA access only proxies HTTP).
So from remote the add-on falls back to HLS, which rides over the same proxy and always works — just
with more delay. This is the same reason the official app leans on **Agora's cloud relay** and Reolink
on **their P2P/relay**: a relay in the middle is what makes remote video fluid.

**Want fluid video from remote too?** Give the WebRTC a relay/tunnel — any of:
- **VPN into your home** (e.g. **Tailscale**, WireGuard): your phone acts as if it's on the LAN, so the
  fluid WebRTC just works — no port-forwarding, nothing to host. Simplest for personal use.
- **A TURN server** (self-hosted `coturn`, or a managed one like Cloudflare's): a public relay both
  sides reach. Works for any device without a VPN client, but you host it / pay for bandwidth.
- **Port-forward** the WebRTC UDP port on your router (least recommended — exposes a port, and CGNAT
  breaks it).

Without any of these, remote stays on the (improved) HLS — perfectly usable to look and steer slowly.

## Driving from automations / AI

Besides the buttons, publish an analog movement vector:

```yaml
service: mqtt.publish
data:
  topic: ebo/move/vector
  payload: '{"ly":-50,"rx":20,"hold":1.5}'
```

- `ly` < 0 = forward, > 0 = back · `rx` = rotation (< 0 left, > 0 right) · `hold` = seconds (the
  robot stops when it expires). Scale ≈ ±100; the vector is re-sent at 10 Hz until `hold` expires.

The full opcode catalog (motion presets, voice, TTS, camera, eyes, scheduling, system…) is in
[COMANDI.md](COMANDI.md), usable via the raw `ebo/cmd` topic. Commands that move the robot
should only be used when you can see it.

## Two-way audio (listen & talk)

The robot has a microphone and a speaker, and the add-on can use both.

- **Listen** — hear the room through the robot. Turn it on with the **🔊 button in the fullscreen
  view** (or the *Listen* toggle on the robot page / the `switch` entity). The sound is carried
  **inside the camera stream**, so the video player must be unmuted — the 🔊 button does both for you.
- **Talk** — speak through the robot with your phone's or computer's microphone: press the **🎤
  button** in the fullscreen view and allow the microphone when the browser asks. Press it again to
  stop. Your voice is sent to the add-on over WebRTC and played on the robot's speaker.
- Two small **level meters** in the top bar (🔊 green, 🎤 blue) show that audio is actually flowing in
  each direction.
- You can also make the robot **speak text** (TTS) with the `say` command / entity — handy for
  automations.

Notes: the robot only starts publishing its microphone when asked to, which the add-on does for you.
Talk needs a direct path to the add-on (like the fluid video), so it works on your LAN. Expect a small
delay: the audio travels through Enabot's cloud in both directions.

## AI agents (MCP) — optional

The add-on can expose the robot to **MCP-capable AI assistants** so they can drive it **with the camera
in the loop**: the agent *looks* through the robot, decides, then moves. It's **off by default** —
turn on **"Allow AI agents (MCP)"** in the Configuration tab. When off, nothing runs (no resources used).

- **Endpoint:** `http://<your-HA-host>:8100/mcp` (streamable HTTP).
- **Auth:** send `Authorization: Bearer <api_token>` — the same `api_token` from the Configuration tab.
  Without it the server answers 401, so nobody on your LAN can drive the robot.
- **Tools:** `ebo_list`, `ebo_state`, `ebo_wake`, `ebo_look` (returns the live camera image),
  `ebo_move`, `ebo_stop`, `ebo_dock`, `ebo_night_vision`, `ebo_laser`, `ebo_say`.
- **Safety:** `ebo_move` **refuses to move unless `ebo_look` was called moments before** (no blind
  driving), and caps speed and step duration. It also refuses while the robot sits on its base.

Example client config (Claude Desktop and similar):

```json
{
  "mcpServers": {
    "ebo": {
      "url": "http://192.168.1.50:8100/mcp",
      "headers": { "Authorization": "Bearer YOUR_api_token" }
    }
  }
}
```

Then ask it things like *"look through the EBO and, if the way is clear, nudge it forward."*

## Known limitations

- **amd64 only** (the Agora SDK is x86_64).
- **Listen (robot microphone)** is best-effort: the robot opens its mic on its own, unpredictably
  — this can't be forced from the server SDK. **Talk** (your audio → robot speaker) works.
- One control session per account: while the add-on is active, the EBO HOME app on the same
  account may be disconnected, and vice versa.
- **Routes (teach & repeat) are model-dependent.** They rely on the robot's route/patrol firmware,
  which the **EBO Air 2 does not have** (its firmware ignores the route commands — the official app
  hides patrol for the Air 2 too). The panel detects this at runtime and **hides the Routes UI** when
  the robot doesn't support it; it shows only on models that answer the route query (e.g. SE).
- Depends on Enabot's cloud API — a change on their side may require an update.

## Troubleshooting

- **Add-on stops: "crypto keys not shipped"** → set `payload_key` and `sign_key` (see above).
- **"login failed"** → check email/password and the region/host (panel → ⚙ Settings).
- **No devices in Home Assistant** → after first start, **restart Home Assistant once** (to load
  the integration the add-on installed), then **+ Add Integration → "EBO"**. Check the add-on log
  for the "installed the EBO integration" line.
- **Robot doesn't respond** → make sure the EBO HOME app isn't controlling the same account.
- **Video is choppy or lags by seconds** → see **Fluid video & smooth driving** above: keep the
  encoder preset on `ultrafast` and use `Low`/`Medium` video quality. High (3 MP) is too heavy to
  re-encode live on most hosts.
- **Camera blank in the panel** → the robot must be awake and the camera on; open the robot (or the
  fullscreen drive view) to wake it. The list thumbnail is the last frame before standby.

## Support

Free, independent project. If it's useful, an optional coffee is appreciated — never required:

☕ **[buymeacoffee.com/scattolacom](https://www.buymeacoffee.com/scattolacom)**
