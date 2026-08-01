# Changelog — Enabot integration

## 0.26.93 — a short demo clip in the README
- Added an animated demo of the robot being driven from a phone, next to the feature list, so the
  project shows what it does in the first two seconds. (Trimmed and optimised to ~3 MB.)


## 0.26.92 — screenshots in the README, and two small UI fixes they revealed
- The project finally **shows what it looks like**: the README now leads with the driving view, the
  panel and a robot as a Home Assistant device (`ebo/docs/img/`).
- **Fixed:** the *Eyes* select sat on **"unknown"** forever — the robot never reports that setting
  back, so the add-on now echoes the last value you chose (same treatment the other write-only
  settings already had).
- **Fixed:** in the robot list the battery gauge crowded the percentage next to it, and the info line
  could wrap awkwardly onto two lines.


## 0.26.91 — the other components caught up with the audio work
- **Integration**: new **Listen** switch, so the robot's microphone can be turned on/off from Home
  Assistant (dashboards, automations), not just from the panel.
- **MCP**: new **`ebo_listen`** tool, so an AI agent can open/close the microphone too.
- **Docs**: new "Two-way audio (listen & talk)" section, and the drive-quality guidance now matches
  reality (the drive view picks High on LAN / Low from remote by itself).


## 0.26.90 — the audio level meters are actually readable now
- The two meters were tiny and barely moved. They are now **bigger and brighter** (labelled 🔊 and 🎤,
  with a visible track and a glow), and the level is computed **perceptually** (RMS with gain) instead
  of raw peak — so normal speech clearly moves the bar. Each meter also has a **peak marker** that
  holds briefly and falls back, so short sounds don't flash by unnoticed.


## 0.26.89 — fix the WHIP (talk) proxy path
- The new microphone path was routed with the wrong prefix internally, so the request died before it
  reached mediamtx and talk could never start. Fixed.


## 0.26.88 — talk to the robot with your phone's microphone, and see the audio levels
- **New 🎤 button in the fullscreen view: two-way audio, like the official app.** Your browser publishes
  the microphone to the add-on over WebRTC (WHIP) and the bridge feeds it straight into the robot's
  speaker. Tap to start, tap again to stop; leaving the drive view stops it too.
- **New level meters in the top bar**: a green bar for what the robot hears you play (speaker) and a
  blue one for your microphone, so you can *see* that audio is flowing in each direction instead of
  guessing.
- The bridge gained a `talk/stop` command and now retries briefly when the live microphone stream
  isn\'t published yet.
- Requires a browser microphone permission, and (like the fluid video) a direct path to the add-on —
  so it works on your LAN.


## 0.26.87 — fix: the picture froze after audio was added (and made the robot look unresponsive)
- Adding the Opus audio track broke the **snapshot grabber**: it probed only **32 bytes** of the stream
  (a latency trick) which was no longer enough for ffmpeg to identify the streams, so **every grab
  failed** and the panel kept serving one frozen frame. Driving still worked, but nothing on screen
  moved — so it looked like the robot had stopped responding to commands.
- The grabber now probes a sensible amount and takes **video only** (`-an -map 0:v:0`).


## 0.26.86 — the robot's audio is no longer seconds behind
- Now that you can hear the microphone, it arrived **badly delayed**. The video path drops stale frames
  to bound latency; the audio path had **no such control** and simply queued up:
  - the pipe feeding ffmpeg is a default **64 KB** buffer — at 8 kHz mono that is **~4 seconds** of
    audio sitting in a queue. It is now **8 KB (~0.5 s)**, so a backlog cannot build: when it is full
    the newest chunk is dropped and playback stays near real time;
  - the audio input used a 1024-packet queue and no low-latency flags → now **64** with
    `nobuffer`/`low_delay`, like the video input;
  - Opus now encodes in **lowdelay** mode with **10 ms** frames, and `aresample=async=1` keeps the
    audio glued to the timeline instead of drifting further behind.


## 0.26.85 — THE reason you could not hear the robot: WebRTC can't carry AAC
- The stream did contain the microphone audio, but it was encoded as **AAC** — and **WebRTC does not
  support AAC** (only Opus, G.711 and G.722). So in the drive view the browser was handed a
  video-only stream: there was literally nothing to unmute.
- The audio is now encoded as **Opus** (48 kHz mono, VoIP mode), which WebRTC carries natively and
  mediamtx's fMP4 HLS also supports. Combined with 0.26.81 (opening the robot mic) and 0.26.84 (the
  speaker button unmuting the player), listening now works end to end.


## 0.26.84 — you can now actually HEAR the robot in the drive view
- The microphone audio was reaching Home Assistant, but the fullscreen player is created **muted** —
  browsers only allow autoplay when a video starts muted, and nothing ever unmuted it. So there was
  simply no sound to hear.
- The **🔊 button now does both halves**: it opens the robot's microphone (opcode 102001) *and*
  unmutes the player (a real tap is required by the browser, which is exactly what this is). Tap again
  to mute and close the mic. The icon shows whether you're currently hearing the robot.


## 0.26.83 — fix: raw HTML appearing in the robot list
- After a few seconds the robot list showed the battery/Wi-Fi gauges as **raw markup** instead of the
  little bar icons. The periodic refresh was still writing that line as plain *text*, while 0.26.77
  changed it to return HTML. It now updates it as markup, so the gauges keep rendering.


## 0.26.82 — a Listen switch you can actually press
- 0.26.81 opened the robot's microphone automatically, but there was **no control for it**. Now there is:
  - **Robot page → Audio → "Listen — hear the robot\'s microphone"** toggle;
  - **Fullscreen → 🔊 / 🔇 button** next to the laser and day/night buttons.
- Turning it off sends `102001 {"open":0}` (the robot stops publishing its mic), turning it on sends
  `open:1`. The choice is remembered and re-applied whenever the robot rejoins.
- Reminder shown in the UI: the audio rides **inside the camera stream**, so the video player must be
  unmuted to actually hear it.


## 0.26.81 — LISTEN WORKS: the robot's microphone finally comes through
- **Solved the long-standing "listen" problem.** Subscribing to the robot's audio track was never
  enough — the robot only **starts publishing its microphone** when it is explicitly told to open that
  direction: opcode **`102001 {"type":1,"open":1}`**. We never sent it, so the track stayed subscribed
  and silent, which looked exactly like a muted mic (and sent us chasing codecs for weeks).
- Verified live: the mic came up **within a second** of the command — `bitrate ~73 kbps, 8000 Hz mono,
  0 loss`, matching what the official app gets — and `open:0` stops it again (bitrate 0).
- The bridge now sends it automatically when the robot joins and audio is enabled, and re-sends it once
  if no audio has arrived. The **talk** direction gets the matching `102003 {"type":1,"open":1}`.
- Along the way the audio codec option gained `0` (µ-law) and `auto`; the default `8` (G.711 A-law) is
  correct — the codec was never the problem.


## 0.26.80 — audio investigation: more codec options, quieter log
- The robot's audio subscription reaches state **SUBSCRIBED**, not "no publisher" — which suggests the
  robot *does* publish a mic track and we simply fail to DECODE it, rather than the mic being muted.
- So the **audio codec** option now also accepts **`0`** (G.711 μ-law — never tried; only 8/A-law and
  9/G.722 were selectable) and **`auto`** (force nothing and let the SDK negotiate — forcing the wrong
  payload type looks exactly like a muted mic).
- The video statistics line now logs **every 30 s instead of every 5 s**: it was flooding the add-on
  log and pushing the audio diagnostics out of the buffer within minutes.


## 0.26.79 — the sharper drive video now actually kicks in (fix for 0.26.78)
- 0.26.78 chose the quality from the **URL** (the "are we remote?" guess). If you open Home Assistant
  through your own domain — even while sitting on the same LAN — that guess says "remote", so it kept
  forcing **Low** and the badge still read `848px`. Wrong signal.
- Now it uses the **transport that actually connects**: when **WebRTC** comes up, the browser is
  talking to the add-on directly, so the robot is switched to **High** (~720p) and that fact is
  remembered — from then on High is requested *before* connecting, with no mid-stream switch. When it
  falls back to **HLS**, quality is put back to **Low** so the remote stream stays watchable.


## 0.26.78 — much sharper drive video on the LAN (720p instead of 480p)
- The drive view always forced the robot to **Low (848×480)**. That dated back to a lag problem which
  — measured again — was really the **x264 `fast` preset**, not the resolution. With `ultrafast` the
  robot's **High source (2304×1296)** downscaled to ~720p runs at **25 fps with 0 dropped frames and
  ~36% CPU on a 2-core host**.
- So quality now follows the **transport**, because they have opposite constraints:
  - **On your LAN (WebRTC)** → the robot is switched to **High** → visibly sharper ~720p, still fluid.
  - **From remote (HLS)** → stays on **Low**, so it remains watchable through the proxy.
- Your own quality setting is saved on entering the drive view and **restored when you leave** — and if
  you pick a quality by hand in the fullscreen ⚙ menu, that choice is kept instead of being overwritten.


## 0.26.77 — readable battery / Wi-Fi gauges, and clearer feedback when sending the robot to sleep
- **Battery and signal are now little bar gauges** instead of an emoji and a raw number: the battery
  is a 4-segment gauge that turns amber below 50% and red below 20%, with a **⚡ bolt while charging**;
  Wi-Fi is 4 bars (a bare "-64 dBm" meant nothing). Hovering still shows the exact value. Used in the
  robot list, the robot page and the fullscreen bar.
- **Sleep now gives immediate feedback.** The add-on can only *stop watching* the robot — the robot
  itself then closes its eyes after a few seconds to a couple of minutes (same as closing the official
  app), so the button used to look like it did nothing. It now dims the picture at once, says
  "Going to sleep…" and shows a short message explaining the delay. Waking shows a message too.


## 0.26.76 — looking at a robot no longer wakes it, and you can send it to sleep with one tap
- **Fixed: opening a robot woke it up by itself.** The panel sent `camera/set on` as soon as you opened
  the robot page, so it could never stay asleep while you just checked on it — and the new "tap to
  wake" button never had a chance to appear. Now **looking is passive**: you wake it deliberately
  (the button on the picture, **☀ Wake**, or by entering the drive view).
- **New: send it to sleep on demand** — a **😴 Sleep** button on the picture (bottom-left) while the
  robot is awake, and the row button is now clearly labelled **😴 Sleep (Zz)** instead of "Standby".


## 0.26.75 — the last frame now survives an add-on restart
- 0.26.74 kept showing the last frame while the robot sleeps, but that cache lived only in memory —
  so after an add-on update or restart the tile was blank again until the robot woke up. The last
  frame is now **also stored on disk** and reloaded at startup.


## 0.26.74 — see the last frame while the robot sleeps, and wake it with one tap
- **The picture no longer disappears when the robot is asleep (ZZ).** Both the robot list and the
  detail keep showing the **last frame we saw**, dimmed, with a **Zz** badge — so you still see where
  the robot was. (It also stops the panel from stalling for seconds on every refresh trying to grab a
  stream that isn't there.)
- **New: a big "Sleeping — tap to wake" button in the middle of the picture** on the robot page, so you
  no longer have to enter fullscreen just to wake it. It disappears as soon as the robot is streaming.
- The view now rebuilds when a robot falls asleep or wakes, so this appears/disappears on its own.


## 0.26.73 — sending the robot home now lets it sleep as soon as it arrives
- When you press **Dock / return to base**, the add-on now **releases the session the moment the robot
  reaches the charger**, so it goes to sleep (ZZ) right away instead of sitting awake on the base
  because we were still "watching". Sending it home means you're done with it.
- If you **drive again** after issuing dock, the pending sleep-on-dock is cancelled — you took control
  back. It also gives up after 10 minutes if the docking never completes.
- Follows the same option as auto-standby: with **"Let the robot sleep after (minutes)" = 0** (never
  sleep) this is disabled too.


## 0.26.72 — the robot can finally fall asleep again (auto-standby)
- **The add-on kept the robot permanently awake.** The robot only sleeps when nobody is watching, and
  the bridge stayed in its Agora session for as long as the add-on ran — so it never showed the **ZZ
  eyes**, unlike when you close the official app. That was constant surveillance nobody asked for.
- New option **"Let the robot sleep after (minutes)"** (default **5**, `0` = never): after that long
  with no commands, the add-on **leaves the session** so the robot goes to sleep, exactly like closing
  the app. Any command — or opening the camera / drive view — wakes it again (and 0.26.70's fresh
  cloud session means it comes back even from deep sleep).
- While you're driving, the fullscreen view re-asserts the camera every ~20 s, so it stays awake.
- Known limitation: only *commands* postpone standby. Passively watching the `camera.ebo` card does
  not, so the robot may doze off while you watch — drive from the panel, or set the option to 0.


## 0.26.71 — fix the reconnect loop introduced in 0.26.70
- 0.26.70 made the wake helper reconnect when no frames were flowing — but that helper is also called
  from the connect path and from the "still waiting for the first frame" retry (every 8 s), so it
  recursed: reconnect → wake → reconnect… roughly every 3 seconds, re-asking the cloud for a session
  each time. Split in two: `_wake()` only sends the opcode (safe to repeat), while the full
  cloud-backed wake runs **only** from the explicit `wake` command.
- Added a **rate limit** on the forced rejoin (at most once every 15 s) so no future change can spin
  that loop again.


## 0.26.70 — wake the robot from DEEP sleep (the "parked on the dock, ZZ eyes" one)
- There are **two different sleeps**, and we only handled one:
  * **light standby** — we left the Agora channel (Standby button, or the robot dozed while we were
    connected). A fresh viewer join wakes it. This already worked.
  * **deep sleep** — the robot drives itself home, sits on the dock and shows the **ZZ eyes**. It then
    **leaves Agora entirely** and keeps only its link to Enabot's cloud. Re-joining the channel with
    our **cached** tokens reached nobody, so the robot could only be revived from the official app.
- **Fix:** the wake paths now ask the cloud for a **fresh session first** (exactly what the app does
  every time you open a robot) and then rejoin — that cloud call is what tells a deeply-sleeping robot
  to come back online. Applies to `set_connected(on)`, the forced rejoin, and the `wake` command,
  which now also performs the full rejoin instead of only sending `isSleeping=false`.


## 0.26.69 — the key-extraction steps are now IN the add-on documentation
- 0.26.68 added the guide as a separate file, but Home Assistant's **Documentation** tab can't follow
  relative links — so it was effectively invisible from inside HA. The **full procedure is now written
  directly in DOCS.md** (get the APK → open with jadx → read the two constants in
  `ServerEncryptHelper` → paste), with clickable download links, plus an absolute link to the longer
  version on GitHub.


## 0.26.68 — guide for getting the two app keys
- Added **[docs/GET-APP-KEYS.md](docs/GET-APP-KEYS.md)**: a step-by-step guide (with download links) to
  read `payload_key` / `sign_key` from **your own copy** of the EBO HOME app — get the APK off your
  phone, open it with jadx, read the two constants in `ServerEncryptHelper`, paste them into the
  Configuration tab. Linked from the README and DOCS.
- Documented **why the keys aren't bundled** (even encrypted): whatever decrypts them would have to
  ship too, so it would protect nothing — and it would mean redistributing someone else's secrets.
  User-supplied keys are safer and cleaner: your own app's keys, for your own robot.


## 0.26.67 — drop the deprecated build.yaml (Supervisor housekeeping)
- The Supervisor now warns that **`build.yaml` is deprecated** ("move build parameters into the
  Dockerfile"). Removed it: the base image and the image labels are declared **in the Dockerfile**.
  The base stays a **Debian/glibc Python** on purpose — the Agora SDK ships glibc `.so` files and does
  not run on the Alpine/musl bases — and a test now enforces that so it can't be changed by accident.
- Fixed the stale image label (it still said "EBO Air 2" instead of the current add-on name).
- Fixed two stale tests (a value map renamed back in 0.26.50, and the shipped-files list).
- NOTE: nothing to do about the *"all_app_configs" folder mapping* rename you may see in other add-ons
  — this add-on maps `homeassistant_config`, which is unaffected.


## 0.26.66 — CRITICAL: the robot no longer stays offline forever after a bridge crash
- **Fixed a supervisor bug that made a crash permanent.** The entrypoint runs with `set -e`; when the
  bridge process died (e.g. a segfault inside the Agora SDK), `wait` returned non-zero and **killed the
  very subshell whose job was to restart it**. The add-on still looked healthy — the container stayed
  "started" and the panel kept responding — but the robot showed **offline (red dot) forever** and no
  command reached it until you restarted the add-on by hand.
- All the supervisor `wait` calls are now failure-tolerant, so a crashing bridge is **restarted** as
  intended (with the existing back-off and the A/V fallback).
- Note: this was **not** caused by the MCP server — memory was at 1.8% and the MCP runs in its own
  process. The crash came from the video/Agora side; what was broken was the recovery.


## 0.26.65 — settings no longer get wiped, and the panel works for non-admin users
- **Fixed: options could be silently reset.** When the add-on persisted its auto-generated `api_token`
  back to the Supervisor, it sent only the login fields — and the Supervisor **replaces the whole
  options block**, so everything else (video/audio settings, log level, the new MCP flag…) was wiped.
  It now merges the token into the **existing** options, so nothing else changes. This is why a
  toggled setting could appear not to "stick" on a fresh install.
- **The sidebar panel is now visible to non-admin users** (`panel_admin: false`). Home Assistant hides
  add-on panels from non-admins by default, but the panel *is* how you drive the robot — so family
  accounts can use it too. Every request is still authenticated by Ingress as that user.


## 0.26.64 — restore the low-latency remote video (undo an unnecessary downgrade)
- 0.26.63 fixed the black screen (leftover WebRTC `srcObject`) but ALSO switched off-LAN playback to
  plain HLS on a hunch that proxies/CDNs break Low-Latency HLS. That hunch was wrong — LL-HLS works
  fine through a real remote connection and is much closer to live. Reverted: **Low-Latency HLS is used
  everywhere again**, so the control-to-video lag is back to the 0.26.60 behaviour.
- Kept from 0.26.63: the **black-screen fix** (always detach the peer connection / clear `srcObject`
  before starting HLS) and **visible error reporting** instead of a silent black video.


## 0.26.63 — fix BLACK screen on the remote HLS fallback (regression from 0.26.62)
- 0.26.62 started always *trying* WebRTC first (so LAN users on a domain URL get the fluid video). But
  a failed WebRTC attempt leaves a dead MediaStream on the `<video>` element (`pc.ontrack` sets
  `srcObject`), and **`srcObject` takes precedence over the HLS/MSE source** — so the HLS fallback
  attached correctly but rendered a **black screen** over cellular. The player now always detaches the
  peer connection and clears `srcObject` before starting HLS.
- Off-LAN also switches to **plain HLS instead of Low-Latency HLS**: LL-HLS relies on blocking playlist
  reloads and chunked "parts" that reverse proxies/CDNs (Cloudflare tunnel, Nabu Casa) tend to buffer or
  break. Slightly more delay, but it actually plays.
- HLS failures are now **reported on screen** (with the reason) after a few recovery attempts, instead
  of silently looping on a black video. Recovery uses hls.js's own network/media recovery first.


## 0.26.62 — fluid video also when you open HA by domain name on your own LAN
- **Fixed:** the panel decided "you're remote" from the **URL alone**, so opening Home Assistant through
  a domain (Cloudflare / Nabu Casa / reverse proxy) forced the slower **HLS even while you were on the
  same network as the robot** — where WebRTC works perfectly.
- Now the URL is only a **hint**: WebRTC is **always attempted**. When the URL looks remote it's probed
  **briefly** (~4-6 s) and falls back to HLS if it truly can't connect. So: same LAN by any URL → fluid
  ~200 ms video; genuinely off-LAN → a few seconds, then HLS as before.
- This also helps **across subnets** (e.g. a guest/IoT VLAN): if routing allows it, WebRTC now connects
  instead of being skipped outright.


## 0.26.61 — MCP server for AI agents (opt-in, off by default)
- The add-on can now expose the robot to **MCP-capable AI assistants**, with the camera **in the loop**:
  the agent calls `ebo_look` (gets the live image), decides, then `ebo_move`. Tools: `ebo_list`,
  `ebo_state`, `ebo_wake`, `ebo_look`, `ebo_move`, `ebo_stop`, `ebo_dock`, `ebo_night_vision`,
  `ebo_laser`, `ebo_say`.
- **Off by default.** New option **"Allow AI agents (MCP)"** — when off, the server never starts, so it
  uses no resources. Turn it on only if you connect an agent.
- **Token-protected:** the endpoint (`http://<HA-host>:8100/mcp`) requires
  `Authorization: Bearer <api_token>` (the add-on's own token); unauthenticated requests get 401 —
  verified.
- **Safety by design:** `ebo_move` refuses to move unless `ebo_look` ran moments before (no blind
  driving), caps speed/duration, and refuses while the robot is on its charging base.
- Installing `fastmcp` is **non-fatal**: if it can't be installed, everything else works and the option
  simply reports as unavailable.


## 0.26.60 — reliable camera.ebo snapshots (fixes intermittent 500)
- `camera.ebo` still images no longer depend on Home Assistant extracting a keyframe from the internal
  RTSP itself — that default path returns **500** when it can't grab a frame in time (seen by tooling /
  automations that pull the snapshot). The camera now fetches the JPEG from the **add-on's own reliable
  snapshot endpoint** (the same one the panel previews use, grabbed from the local mediamtx), and falls
  back to the stream grab if the add-on can't provide one. Live streaming (WebRTC/HLS) is unchanged.
- NOTE: because this changes integration code, it takes effect after an **HA core restart**.


## 0.26.59 — slimmer, auto-dismissing HLS notice
- The HLS "video is delayed" warning was a big multi-line box covering the view. It's now a **slim
  one-line pill** that **auto-fades after ~5 s** (the amber **HLS** badge stays as the persistent
  indicator). It re-appears briefly each time you enter fullscreen on HLS.


## 0.26.58 — REVERT the HLS "improvement" (it broke HLS loading)
- The 0.26.55 remote-HLS tuning was a **regression**: mediamtx `hlsSegmentCount: 3` (a ~3 s playlist)
  together with hls.js `liveMaxLatencyDuration: 4` is an invalid combo — hls.js won't start when the
  max-latency exceeds the playlist window, so the remote video hung on "connecting". Reverted both to
  the known-good 0.26.54 values (`hlsSegmentCount: 7`, plain `lowLatencyMode` player). Remote HLS loads
  again. (A safer latency tune can come later, with the window sized to match.)


## 0.26.57 — Routes hidden on robots that don't support them (e.g. Air 2)
- **Routes (teach & repeat)** needs the robot's route/patrol firmware. The **EBO Air 2 doesn't have
  it** — its firmware silently ignores the route/patrol commands (the official app hides patrol for the
  Air 2 for the same reason). Verified live: the robot never answers the route query, the record
  start/stop, or returns a recorded path.
- The panel now **detects route support at runtime** (whether the robot answers the route query) and
  **hides the Routes section + the ⏺ record button** when unsupported — no more silently-failing UI.
  On models that do support it (e.g. SE) the Routes UI appears exactly as before.


## 0.26.56 — English-only UI strings + debug off
- All panel strings are now **English** (the add-on is English-only): the connection badge/warning, the
  "Connecting to the robot…" overlay and the detail connection hint were showing Italian.
- Turned off debug logging that had been left on for diagnostics — it was starving the video encoder,
  which left the remote HLS stuck on "connecting".


## 0.26.55 — keyboard-in-dialog fix, connection badge, better remote HLS
- **Fixed: keyboard drove the robot while typing.** When the "Save route" name box (or any dialog) was
  open, pressing `a`/`w`/`s`/`d`/arrows moved the robot instead of typing (the `a` key sits right over
  the drive stick). The keyboard now ignores driving keys while you're in a text field or a dialog is open.
- **Connection badge + warning.** The fullscreen top bar shows a **green "WebRTC"** (fluid ~200 ms) or
  **amber "HLS"** badge; on HLS a banner warns the video is delayed (~1 s) — fine to watch/steer gently,
  not for reactive driving. The robot page also shows, before you open fullscreen, whether you'll get
  fluid LAN WebRTC or remote HLS.
- **Better remote HLS.** The player now hugs the live edge (catch-up playback), and mediamtx keeps a
  shorter playlist → less lag on the remote/fallback path. **LAN WebRTC is untouched.**
- **Docs:** new "Video connection: LAN vs remote" section explaining why remote is slower and the
  relay/VPN (Tailscale/TURN) options for fluid remote video.


## 0.26.54 — fullscreen works from cellular / remote (instant HLS instead of a WebRTC hang)
- **Fixed the fullscreen "drive" view failing when you're not on the robot's LAN** (mobile data, Nabu
  Casa remote, a reverse proxy). The fluid path is **WebRTC**, whose media needs a *direct*
  browser→host:8189/UDP hop; mediamtx only advertises the host's **private LAN IPs** as ICE candidates
  and there's no STUN/TURN, so from remote WebRTC can never connect — it just hung ~15 s on
  "Connessione al robot…" before (maybe) falling back.
- Now the panel **detects when it's opened from off-LAN** (from `location.hostname`) and plays the
  **Ingress-proxied HLS straight away**, skipping the doomed WebRTC attempt. The badge shows
  **"HLS (remoto)"** and the video starts in ~1-2 s instead of hanging. On the LAN nothing changes:
  WebRTC is still tried first for the ~200 ms fluid drive video, with HLS as fallback.
- NOTE: remote video is the ~1 s HLS, not the 200 ms WebRTC — good enough to watch/steer gently, but
  the truly-fluid drive path stays LAN-only unless a STUN/TURN server is added later.

## 0.26.53 — Routes: teach & repeat (record a path, replay it)
- New **Routes** feature, reverse-engineered from the app (the Air 2 firmware supports it even though
  the current app hides it): **drive to teach a path, then have the robot repeat it.**
  - **Record** from the fullscreen ⛶ view: the ⏺ button starts recording (opcode 103201), you drive the
    path, tap ⏺ again to stop (103205); the robot hands back the recorded route (103206) and you give it
    a name to save (104003).
  - **Repeat / delete** any saved route from the robot detail's **Routes** section (patrol 103061 /
    delete 104005). Routes list comes from the robot (104001/104002).
  - Clarified that **"Motion recording"** is an activity log, *not* a path recorder.
- NOTE: recording video *during* a replay isn't in this build — the robot has no manual "record video"
  command, so that has to be done add-on-side (capturing the stream to a file); it's the next step.
- This is built to the app's protocol but the record/replay path **moves the robot**, so it needs live
  testing on a real robot.

## 0.26.52 — dual-stick turns continuously (matches the official app)
- **Fixed steering**: holding the dual-stick to turn now makes the robot **keep turning**, instead of
  jerking ~90° and then going straight. The move command (opcode 101007) carries a `buttons` flag that
  the official app uses as the control scheme — **1 = dual-stick** (independent throttle + steering →
  continuous turn), **0 = single joystick** (the vector is a heading). We were always sending 0, so the
  robot treated the dual-stick like a single joystick. Now the panel sends the right scheme per control:
  - **dual sticks → 1** (continuous turn, like the app's 双摇杆 mode)
  - **single joystick** (detail + fullscreen) **→ 0** (heading, like the app's 单摇杆 mode)
  - **keyboard / D-pad → follows the chosen control type** (dual or single), so it behaves consistently
    with whatever you drive with.

## 0.26.51 — settings grouped by function + clearer audio labels
- **Fullscreen ⚙ menu** re-tabbed to match the detail, grouped by function: **Driving** (mode · speed ·
  collision avoidance) · **Camera** (night vision · video quality) · **Audio** (speaker + call volume) ·
  **Controls** (joystick config). Consistent with the robot detail's sections.
- **Clearer audio labels** everywhere, so the two volumes aren't confusing:
  - **Speaker volume** — the robot's own voice & sounds (`playbackVolume`).
  - **Call volume** — your voice through the robot, two-way talk (`talkbackVolume`).
- **Fixed** the speaker-volume showing "—": the robot does report `playbackVolume`, it just wasn't
  published to the state. Now it shows the real value.

## 0.26.50 — day/night vision
- Added **day/night vision** control, matching the app's fullscreen day/night button. Three modes:
  **Auto / Day / Night** (the Air 2's `shootMode`, opcode 102035; confirmed 0=Auto, 1=Day, 2=Night
  from the app's own day/night layout). It's **real state** — read back from the robot's settings
  report, so the control always shows the current mode.
- **Fullscreen**: the previously-disabled 🌙 button now works — tap to cycle Auto → Day → Night, with
  an icon that reflects the current mode (🌗 Auto · ☀️ Day · 🌙 Night).
- **Detail** (Camera & display) and a **native `select` entity** *Night vision* also expose it.
- (Note: this repurposes the old, mislabelled "shoot mode" select — on the Air 2 that opcode is the
  day/night vision, not Normal/Wide/Follow.)

## 0.26.49 — tidier robot detail (one speed, clearer sections)
- **Removed the duplicate speed control.** The detail had two: the browser-side *joystick sensitivity*
  and the robot's real *Movement speed*. The detail now shows only the robot's **Movement speed**
  (real state); joystick sensitivity lives in the fullscreen ⚙ → **Controls**, where it belongs.
- **Reorganised the detail into clear sections**: **Remote control** (joystick + fullscreen),
  **Driving** (mode · movement speed · collision avoidance), **Camera & display** (video quality ·
  image style · eyes), **Audio** (speaker volume · two-way call volume), **Recording** (motion rec).
- Collision avoidance and motion recording are now clean on/off **toggles** (motion rec was two buttons).

## 0.26.48 — driving settings, just like the Enabot app's fullscreen menu
- The fullscreen ⚙ menu is now **tabbed like the app** (Settings / Controls / Auxiliary):
  - **Settings**: **Driving mode** (Smooth / Racing), **Movement speed**, **Call volume**.
  - **Controls**: our joystick config (two-sticks/single, swap, side, sensitivity) + video quality.
  - **Auxiliary**: **Collision avoidance** toggle. (The app's "Auxiliary View" is an app-only on-screen
    overlay, not a robot setting, so it's not included.)
- The same driving settings are also on the **robot detail** page (new "Driving" section) and as
  **native entities**: `select` *Driving mode* + `switch` *Collision avoidance*.
- All of these are **real state**: `moveMode` and `avoidobstacle` come back in the robot's normal
  settings report, so the controls reflect the robot's actual values. Collision avoidance uses the
  robot's **dedicated single-field setter** (opcode 103045) — no whole-bundle write, so it never
  disturbs the other motion settings.

## 0.26.13 — fluid drive video that works through Ingress (hls.js + proxy)
- The 0.26.12 fluid HLS was black in the panel because HA's Ingress blocks the nested iframe. Now the
  fullscreen 'drive' view plays the Low-Latency HLS in a **<video> via hls.js**, with the stream
  **proxied through the add-on/Ingress** (same origin) — so it plays fluidly inside the panel, no
  CSP/CORS trouble. Falls back to the snapshot preview if hls.js is unavailable.


## 0.26.12 — FLUID drive video (Low-Latency HLS) in fullscreen
- The fullscreen 'drive' view now plays the robot's **Low-Latency HLS** stream (mediamtx) instead of
  choppy snapshots → **fluid, ~0.5-1s latency** video for actually driving. Served on port 8888
  (per robot), reachable directly from your browser (HTTP, no WebRTC/ICE hassle). List/detail
  thumbnails stay on snapshots (fine for previews).


## 0.26.11 — lower-latency preview, wake-on-drive, keyboard driving fixed
- **Video latency:** reverted the persistent MJPEG feeder (it buffered → laggy). Back to fresh
  per-frame grabs with `nobuffer`/`low_delay`/tiny probe, short 0.25 s cache, and a shorter RTSP
  keyframe interval (~1 s) → fresher frames for driving.
- **Wake on drive (like the app):** opening a robot or entering the fullscreen gamepad now sends
  **camera-on + wake**, and keeps the robot awake (re-wakes every 15 s while you're driving), so it
  doesn't drop back to the sleeping (Zz) state.
- **Keyboard driving fixed:** the fullscreen view now takes keyboard focus, so the **arrow keys /
  WASD** actually drive the robot.


## 0.26.10 — low-latency panel preview (persistent MJPEG feeder)
- The panel preview used to spawn a fresh ffmpeg per frame (reconnecting to RTSP each time → ~2 fps,
  laggy). Now a **single persistent ffmpeg** keeps the latest frame in memory, so snapshots are
  instant and the preview runs ~10-15 fps at low latency (much better for driving). The feeder
  auto-stops when nobody's watching. Fullscreen refresh 120 ms, list/detail 250 ms.


## 0.26.9 — CRITICAL FIX: no commands worked in native mode (missing subscriptions)
- The bridge's **command topic subscriptions** were located AFTER the `expose_mqtt` gate inside the
  discovery method. Since native mode (the default since 0.26.0) sets `expose_mqtt: off`, that method
  returned early — so the bridge **never subscribed to the command topics** and ignored every command
  (wake / laser / move / camera / everything), though telemetry still flowed. Subscriptions now run
  **unconditionally on connect**, before the discovery gate. This is the real cause of "nothing works".


## 0.26.8 — FIX: commands stop reaching the robot (blocking sends on the receive thread)
- Root cause of "nothing responds after a while": `rtm.publish()` ran **synchronously on the MQTT
  receive thread**. When a cloud send got slow, it blocked that thread, so no further command was
  ever delivered to the robot — it looked totally dead until an add-on restart. Now **all sends go
  through a single dedicated sender thread via a queue**; the receive/control loops only enqueue and
  never block, and the SDK is never called concurrently. (Replaces the 0.26.7 lock, which serialized
  but still blocked the receive thread.)


## 0.26.7 — FIX: commands stop working after a while (RTM thread-safety)
- The Agora RTM `publish()` was called from several threads (heartbeat loop, movement loop, command
  handler) **without a lock**. The SDK is not thread-safe, so concurrent sends corrupted the
  connection — dispatch latency crept to **several seconds** and the robot dropped the control
  session (laser/move/etc. stopped responding, needing an add-on restart). All RTM sends are now
  **serialized through a lock**, keeping the link healthy.


## 0.26.6 — keyboard driving, quieter logs, log level in Configuration
- **Fullscreen keyboard driving:** arrow keys (or WASD) drive the robot while in the fullscreen
  gamepad — hold to move, release to stop.
- **Quieter logs:** the per-command timing line now only appears when the cloud link is genuinely
  slow (>2s), instead of every couple of seconds — so a healthy add-on log is calm.
- **Log level** is back in the add-on **Configuration** tab (`log_level`: info/debug/warning).


## 0.26.5 — panel driving UX: wake-on-open, feedback, fullscreen toggles, charging notice
- **Wake on open, standby on leave:** opening a robot in the panel wakes it; going back puts it in
  standby. (A robot **on the charger won't drive** — the detail page now shows a clear notice.)
- **Button press feedback:** D-pad and overlay buttons now visibly react when pressed.
- **Fullscreen:** tap the video to **show/hide the controls**; open fullscreen by **tapping the
  camera** in the detail page; smoother live refresh while driving.
- **Robot list:** each robot has **🎮 drive** (straight to fullscreen gamepad) and **⚙ open** icons.


## 0.26.4 — driving available to any user (dashboard), not just the admin panel
- The integration now exposes **movement buttons** (Forward / Back / Turn left / Turn right / Stop)
  per robot, so the robot can be driven from a **Home Assistant dashboard by non-admin users** — the
  add-on panel stays admin-only (settings + pairing). Each press drives ~1s then stops (watchdog).
- Added a ready **drive card** (`lovelace/ebo-drive-card.yaml`): the camera with a D-pad overlaid;
  restrict its view to a user to give them driving without admin access.


## 0.26.3 — camera self-heals its RTSP URL
- The integration camera now reads the **current** RTSP URL from the add-on on each update, so it
  fixes itself when the add-on's address changes — no need to remove and re-add the robot.

## 0.26.2 — drive from the panel (D-pad + fullscreen gamepad) + camera reachability fix
- **Panel driving:** each robot's page now has a **D-pad** (hold to move, release to stop; analog
  vector with a watchdog) and a **⛶ Fullscreen gamepad** mode — the live view fills the screen with
  the D-pad and quick actions (camera/wake/laser/dock/standby) overlaid. Movement commands are now
  allowed from the panel (you drive while watching the view).
- **Camera fix:** the integration's camera RTSP now uses the add-on's **internal hostname** (like
  the data API), reachable by Home Assistant core regardless of LAN/VLAN — previously it used a
  guessed LAN IP and could be unreachable (go2rtc "connection refused").

## 0.26.1 — fix integration load on Home Assistant 2026.7
- The integration's config flow failed to load on recent Home Assistant ("Invalid handler
  specified") because `is_hassio` moved to `homeassistant.helpers.hassio`. Now imported from there
  (with a fallback), and the unused Supervisor-discovery step was removed. Verified live: robots
  are added as native EBO devices.

## 0.26.0 — native-only, no MQTT, one repo, self-installing integration
- **Renamed `ebo_air2` → `ebo`** (add-on slug, integration domain, topics) so it's generic for
  future robot models; robots stay distinct via their `model`.
- **No MQTT in your Home Assistant.** The bridge↔panel bus is now a **mosquitto bound to
  127.0.0.1 inside the container** (private); `services: mqtt:need` removed. Home Assistant needs
  no broker and no MQTT integration. Entities come from the **native integration** only.
- **The add-on installs its integration itself** (no HACS): the image bundles it and copies it into
  `/homeassistant/custom_components/ebo` (`map: homeassistant_config:rw`). After one Home Assistant
  restart: **Settings → Devices & Services → + Add Integration → "EBO"** — it finds your robots via
  the add-on's API automatically (nothing to type). Robots appear as **distinct devices under EBO**.
- The integration no longer depends on MQTT; discovery is via the add-on's HTTP API. Token is
  auto-generated and persisted to the add-on options so the integration can read it via Supervisor.

## 0.25.2 — command-dispatch timing (proves the lag is the cloud, not the transport)
- The bridge logs the **local** cost of dispatching a command (the only part MQTT-vs-native could
  affect); it's normally a few ms, so it stays silent unless it exceeds 25 ms. The rest of the
  perceived lag is the Agora **cloud** round-trip, which no transport choice removes. Use the
  **native integration** (default): robots show as **distinct devices under the EBO integration**
  (not under MQTT) with the same latency.

## 0.25.1 — same engine runs standalone (HA Container/Core, x86_64)
- `run.sh` can synthesize its config from **environment variables** (`EBO_EMAIL`, `EBO_PASSWORD`,
  `EBO_PAYLOAD_KEY`, `EBO_SIGN_KEY`, `EBO_EXPOSE_MQTT`) and honor a pinned `EBO_API_TOKEN`, so the
  image runs as a plain Docker container for installs that can't use add-ons. No-op under
  Supervisor. See **STANDALONE.md** + `docker-compose.yml`. Requires x86_64 (Agora SDK is amd64).

## 0.25.0 — configure from the integration + robots as distinct devices (not MQTT)
- New add-on option **`expose_mqtt`** (default on for standalone use). The companion **EBO
  integration** now provisions the add-on for you (Supervisor): you enter the account there and it
  sets these options and starts the add-on — no need to touch the Configuration tab. It sets
  `expose_mqtt: off`, so each robot appears as a **distinct native device** (camera + entities)
  owned by the integration, instead of a set of MQTT entities. `expose_mqtt` moved out of the panel
  into the add-on options (so the integration can drive it). Pairs with integration **v0.3.0**.

## 0.24.1 — clearly-unofficial branding + document the user-supplied keys
- Renamed to **EBO for Home Assistant (unofficial)** (add-on, integration, panel, logo) to avoid
  looking official. Documented in DOCS how to supply the required app crypto keys (not shipped).

## 0.24.0 — do not ship the app crypto keys (risk reduction)
- The Enabot app's signing/encryption keys are **no longer in the public code**. They're supplied
  by the user via the new **payload_key / sign_key** config fields (password-typed). Removed the
  hardcoded key defaults, the extraction framing in comments, and a personal account id from the
  tests. Unofficial/free/community wording clarified. The add-on stops with a clear message if
  the keys aren't set.

## 0.23.1 — multi-model docs (cloud family) + EBO SE guidance
- Documented supported models: the **cloud family** (Air 2 verified; Air 2 Plus/S, Mini, EBO X,
  Max experimental — same Agora cloud, discovered automatically). **EBO SE** is LAN/TUTK and is
  NOT this add-on — pointed to the community **ebo-se-lan-bridge** (coexists; can't bundle its
  proprietary ARM libs). Panel 'Add robot' notes the SE case.

## 0.23.0 — native-integration reachability fix + remove robot + account + polish
- **Fix**: the native integration couldn't reach the add-on API on the LAN IP (VLAN-firewalled).
  The add-on now announces its API on the internal Supervisor hostname → HA reaches it regardless
  of LAN/VLAN firewalls. Existing installs self-heal on the next boot (re-discovery).
- **Remove a robot from the account** from the panel (unbind, DELETE robots/robot/<id>) with a
  confirmation. Available in the robot detail.
- The panel header shows the **connected account** (email). Settings now have readable labels
  (incl. the **Expose entities over MQTT** toggle). Port descriptions clarified (internal).
- README rewritten: Enabot manages ALL account robots, provides the panel + the integration.

## 0.22.3 — smooth previews (both views) + Standby
- Reverted MJPEG (didn't stream through HA Ingress → detail preview went blank). Previews now use
  **double-buffered snapshots** (preload off-screen, swap on load) on BOTH the list and the detail
  view → smooth, no flicker. Per-node grab lock avoids ffmpeg pile-up.
- New **Standby** action (panel + native integration) to put the robot back to sleep.

## 0.22.2 — panel fixes: phantom 'homeassistant' robot + flickering preview
- The panel no longer creates a fake robot from `homeassistant/status` (its +/status,+/state
  wildcards now ignore non-EBO nodes).
- Live preview no longer flickers: the detail view uses a smooth **MJPEG stream** (set once), and
  the panel updates values in place instead of rebuilding the page every few seconds.

## 0.22.1 — wake from standby (like the app)
- Turning the **camera on now wakes the robot** from standby (sends `isSleeping=false`, opcode
  101047), and re-sends the wake if no video arrives — mirroring the app, where opening the live
  view wakes it. New **EBO wake** button (MQTT) and Wake in the panel/native integration.

## 0.22.0 — groundwork for the native integration (expose_mqtt toggle + data API)
- New **`expose_mqtt`** setting (default on): off = don't publish HA entities over MQTT (the
  native integration will own them). Panel state/commands still use MQTT.
- The panel now also serves a **token-guarded data API** on port 8098 (host-mapped) for the
  upcoming native integration; the API URL + token are included in the discovery announce.

## 0.21.0 — pair a NEW robot from the panel (QR, no phone)
- The **+ Add robot** button now runs the real pairing: enter the Wi-Fi, the panel mints a cloud
  **bind key**, shows a **QR** the robot's camera scans to join Wi-Fi + bind to your account, and
  polls until it's bound — then restarts to bring it online. Reproduces the app's flow server-side
  (`bind_key`/`bind_status`, Base64 QR `s/p/m/k/r`). Adds the `segno` QR generator.

## 0.20.1 — Enabot branding + fix white detail page + config tab = login only
- Add-on renamed **Enabot**. Fixed the blank page on selecting a robot (a JS function was named
  `open`, shadowing the browser's `window.open`). Everything English.
- Configuration tab now holds ONLY **email + password**. Region, host, robot_id and all operational
  settings live in the panel (⚙ Settings) / `/data/panel.json`.

## 0.20.0 — panel redesign (list → detail) + operational settings out of the config tab
- Panel renamed **Enabot**, now a **list** of robots (thumbnail, name, battery, wifi) — click a
  row to open its **detail page** (big preview + controls + robot settings).
- **Operational settings moved out of the add-on Configuration tab** into a panel-managed store
  (`/data/panel.json`, read by run.sh): video/audio/talk, video quality (max height/fps/bitrate/
  preset), audio codec, log level. The config tab now holds ONLY account/connection
  (email/password/region/host/robot_id/host_ip). Existing values are migrated on first boot.

## 0.19.2 — tidy config (panel-first)
- Config tab is now just the essentials (account + connection); the panel is the place for
  operational settings. Removed the diagnostic `audio_tx_test` and the unusable `video_encoded`
  (H.265 passthrough segfaults) from the options schema.

## 0.19.1 — panel settings (robot + add-on options)
- The Ingress panel now edits settings: per-robot **video quality, image style, eyes, volume,
  speed, motion recording** (over MQTT), and **add-on options** (log level, video max height /
  fps / bitrate / preset, audio, talk) via the Supervisor (Save & restart). Added `hassio_role:
  manager` so the panel can apply its own options.

## 0.19.0 — Ingress web panel (Zigbee2MQTT-style sidebar UI)
- New **sidebar panel** (Ingress): one page to see and manage every robot the add-on bridges —
  online status, battery, wifi, a live JPEG preview, and quick controls (camera on/off, laser,
  dock). Aggregated over MQTT; no extra dependency (stdlib http.server + paho + ffmpeg).
- `panel.py` runs once for the whole add-on; movement is intentionally NOT exposed here.

## 0.18.2 — English-only (docs + remaining strings)
- Translated all docs to English and renamed GUIDA-HA→GUIDE-HA, COMANDI-APK→COMMANDS-APK;
  fixed the last two Italian strings in code (a log line + a comment). Everything is English now.

## 0.18.1 — use the robot's real name for a single robot too
- The device/camera now takes the robot's actual account name even with one robot (was the
  generic "EBO Air 2").

## 0.18.0 — auto-discovery for the companion HA integration (device + live camera per robot)
- The add-on now announces each robot on retained MQTT `ebo/discovery/<node>` with its
  name, serial, **MAC**, model and RTSP URL. The companion **Enabot EBO integration** (HACS,
  `custom_components/ebo`) turns this into a *"device detected → Add"* flow that creates a
  **device named after the robot** with a **live camera** (RTSP → HA stream/go2rtc = WebRTC).
- The MQTT device now includes the robot's **MAC as a connection**, so the integration's camera
  **merges into the same device** as the sensors/controls — one complete device per robot.
- No manual Generic Camera, no `/config` YAML. Multiple robots each auto-create their own
  device + camera named after them.

## 0.17.5 — video hardening (configurable fps + bitrate cap, sturdier encoder)
- New options **`video_fps`** (default 20) and **`video_bitrate`** kbps VBV cap (default 2500,
  0 = uncapped), alongside `video_max_height` / `video_preset` — tune resolution, frame rate and
  bandwidth from the add-on config.
- **Frame decimation**: drop source frames to hit the target fps, cutting re-encode CPU
  (720p@20 ≈ 35-40% vs ~55% at 25).
- **Bitrate cap (VBV `-maxrate/-bufsize`)**: busy scenes can't spike bandwidth/CPU.
- **Sturdier encoder**: detect an ffmpeg exit (`poll()`) and restart it; `kill()` on stop to end
  the "Error writing trailer: Broken pipe" log spam.

## 0.17.4 — audio listen A/B CLOSED + SSH-drivable test
- Live A/B on the real robot (via `audio_tx_test` = off/silence/tone/auto): publishing an audio
  track — silent OR a sustained tone — NEVER opens the robot's mic. Listen is not achievable via
  the server SDK. **Talk (→ robot speaker) is confirmed working** (the tone was heard on the
  robot). `audio_tx_test` option added for the diagnostic; default off.

## 0.17.3 — audio DIAGNOSTIC build (A/B what opens the robot's mic)
- Live finding: with 0.17.1 publishing a silent track for 1h+, the robot's mic never opened — yet
  our silence looped back through the mix, proving our track WAS published/active. So "an audio
  publisher exists" is NOT the trigger.
- Adds a runtime A/B switch over MQTT `ebo/audio_tx/set` = `off` | `silence` | `tone`, and a
  clear `*** ROBOT MIC OPENED *** (tx=…, N.s after TX start / TX was OFF)` log, to test whether the
  robot opens its mic only when it hears real audio energy (VAD). `tone` = faint ~400 Hz.
- Diagnostic only; no behaviour change for normal use.

## 0.17.2 — honest audio + stop the false "audio works"
- **0.17.1's silent-track trick did NOT work.** Live test: publishing a silent audio track on
  camera-on did **not** make the robot open its mic (no `before-mix from 200001609`, no
  `reason=6`, no stats in a 17-min run). Worse, the post-mix observer began catching **our own
  silence** and logged a **false** `PCM flowing — audio works`, while feeding silence to the
  RTSP audio. Reverted.
- **Listen is now pure subscribe** (mirrors the app's speaker icon). Only the robot's
  **before-mixing** frame (its uid) feeds the HA audio; post-mix paths are ignored so 'talk'
  is never echoed into the listen feed and there are no false positives.
- Honest watchdog: if the robot's mic stays muted it now says so plainly — the mic opens on its
  own, unpredictably, and the reliable trigger is an **RTM command the phone app sends that we
  haven't captured yet** (next step, when the phone can go on the test network).
- `talk` (speak to the robot) is unchanged and still available via `ebo/talk`; the TX track
  is published only while a clip plays.
- **Net effect for now:** video, movement, sensors, snapshots, patrol, eyes and TTS all work;
  **listen** works only when the robot opens its mic on its own (best-effort, honestly reported).

## 0.17.1 — audio listen: open the two-way channel so the robot's mic turns on (REVERTED in 0.17.2)
- **Correction to 0.16.12's "audio works":** the decode pipeline is correct, but listening was
  NOT reliable — the robot keeps its mic **muted** and only opens it when a **two-way audio
  channel** is active. Subscribing alone left it silent for 40+ minutes in testing. (The earlier
  one-off success was a coincidence: the robot's mic had been opened by a prior app "talk".)
- Fix: when the camera is on and `audio: true`, the bridge now **publishes a silent audio track**
  (`[audio-tx] publishing audio track …`). That opens the two-way channel — exactly what the
  app's mic does — so the robot turns its microphone on and we finally receive audio. The track
  is published only while the camera is on and unpublished when it goes off.
- `talk` clips now flow through the same channel (queued), so talking works whenever listening is
  on too, not just with a separate `talk: true`.
- Trade-off to be aware of: while you're watching with audio on, the robot is in a two-way
  "call" (it may show a call indicator / use a bit more battery). Turn the camera off to end it.

## 0.17.0 — talk: speak TO the robot (two-way audio)
- New **`talk: true`** option. When on, the bridge publishes an audio track to the robot (the
  server-SDK equivalent of the app's mic/"talk" button — `publishMicrophoneTrack`, audio
  scenario 3), so you can play audio through the robot's speaker.
- New command topic **`ebo/talk`** (and a text entity **"EBO talk (audio URL)"** when
  `talk` is on): the payload is anything ffmpeg can read — an **http(s) URL** (e.g. a Home
  Assistant **TTS media URL**) or a file path. It's decoded to 8 kHz mono and streamed to the
  robot in real time; the track is published only while playing and unpublished after.
  - Example (HA automation): generate TTS to a media URL, then `mqtt.publish` it to
    `ebo/talk`. Or send any sound-effect URL.
  - Note: this makes the robot **emit sound** — it's user-initiated, but be mindful it's a
    device in your home. One utterance plays at a time.
- This is distinct from `ebo/say` (which makes the robot speak text in *its own* TTS voice
  via the cloud) — `talk` plays *your* audio through its speaker.

## 0.16.12 — audio WORKS 🎉 (and honest watchdog)
- Confirmed live on the robot: `first PCM frame (before-mix)` + `first remote audio DECODED —
  codec OK!`, steady `stats: bitrate=73 bytes=… sr=8000 ch=1 loss=0`. The 0.16.9–0.16.11 chain
  (subscribe explicitly + 8 kHz + `enable_audio_recording_or_playout=1` + drop `pcm_data_only`)
  is the full, verified fix. Audio is forwarded to the RTSP stream.
- **Note on timing:** the robot's mic starts *muted* and unmutes on its own (audio track
  `reason=6` = remote-unmuted), sometimes a few minutes after connect, and can go quiet again
  (`reason=7` = remote-offline). This is robot-side behaviour, not a bug.
- Rewrote the misleading "no PCM after 8 s → change the codec" watchdog: it now waits longer and
  says plainly that we're subscribed and waiting for the robot to open its mic — audio will play
  automatically when it does.

## 0.16.11 — audio: drop pcm_data_only, mirror the working video path
- Playout enabled still gave no PCM. The one thing the audio path did that the (working) video
  path didn't was set `AudioSubscriptionOptions(pcm_data_only=1)` — a raw-track-PCM mode that
  bypasses the playout frame observer. Removed it: audio now uses plain `auto_subscribe_audio`
  + `enable_audio_recording_or_playout=1` + frame observer, exactly like video.
- Observer now also catches `on_mixed_audio_frame` (a third possible delivery path) so whichever
  callback the SDK actually uses, we forward it — and the `first PCM frame (<source>)` log tells
  us which one fired.

## 0.16.10 — audio: enable the decode/playout pipeline + post-mix path
- 8 kHz alone still gave no PCM despite `subscribe state 3`. Root cause: `RTCConnConfig` was
  missing **`enable_audio_recording_or_playout=1`** — without it the SDK subscribes but never
  runs the audio decode/playout pipeline, so the PCM observers never fire. Now enabled.
- Also register the **post-mix `on_playback_audio_frame`** observer (the mixed remote output)
  in addition to before-mixing, and set both frame formats to 8 kHz mono — whichever the SDK
  delivers, we forward it to ffmpeg.
- If this still yields no `[audio] first PCM frame`, the `[audio-diag]` subscribe-state (3) plus
  the absence of PCM points at the SDK build, and we'll dump raw track stats next.

## 0.16.9 — audio: the robot streams 8 kHz, we were asking for 16 kHz (likely THE fix)
- Instrumented the **real app's audio-receive** path with Frida and pressed "listen" ONLY (no
  talk): `onFirstRemoteAudioDecoded — AUDIO IS FLOWING`, `onRemoteAudioStats: bitrate=90,
  **sr=8000, ch=1**`. So the robot streams its mic on a bare subscribe (no two-way call needed),
  8 kHz mono G.711 — and it decodes fine.
- Our bridge asked the SDK for **16 kHz** PCM (`AudioSubscriptionOptions` + before-mixing params
  + ffmpeg), a mismatch with the 8 kHz source that stopped the PCM observer from ever firing.
  Now everything uses **8 kHz mono** (`AUDIO_RATE`, env `EBO_AUDIO_RATE`). This is the concrete,
  evidence-based cause of "subscribed (state 3) but no PCM".
- Net of 0.16.7–0.16.9: subscribe explicitly (like the app's listen icon) + correct 8 kHz rate.

## 0.16.8 — audio: subscribe harder + subscribe-state diagnostics
- 0.16.7's `subscribe_audio` returned rc=0 but the track still didn't appear. Now: on join we
  call **both** `subscribe_audio(uid)` and `subscribe_all_audio()`, **and retry once after 2.5 s**
  (the robot may publish its audio track a moment after joining).
- New diagnostics: `on_audio_subscribe_state_changed` (state 3 = subscribed, 1 = **robot has no
  audio publisher**) and `on_user_audio_track_state_changed` — these say definitively whether the
  robot is publishing audio at all, or we're failing to subscribe.
- Also set the codec on the **connection** handle after connect (in addition to the global
  pre-join set), matching the app which sets `custom_payload_type` post-join.
- **Set `audio_codec: 8`** (not 9): the app uses payload type **8** (G.711 A-law) for this
  monitor stream — confirmed by Frida. 9 is the wrong codec for listening.

## 0.16.7 — audio: subscribe to the robot's track explicitly (VERIFIED against the app)
- Instrumented the real app on the emulator with Frida and captured exactly what the audio
  buttons do:
  - **"Listen" (speaker)** → `muteRemoteAudioStream(robotUid, false)` — it just **subscribes to
    the robot's audio track**. No RTM command; it does NOT publish the phone's mic. So the robot
    publishes audio all along — the app simply doesn't subscribe until you tap listen.
  - **"Talk" (mic)** → `enableLocalAudio(true)` + `updateChannelMediaOptions(publishMicrophoneTrack
    =true)` — publishes the phone's mic (the other, outbound direction; not needed to listen).
- Our `auto_subscribe_audio=1` wasn't engaging (no track ever appeared). Fix: on robot-join we
  now call `local_user.subscribe_audio(robotUid)` explicitly — the exact server-SDK equivalent of
  the app's listen button. Combined with the codec params from 0.16.5 (payload 8 = G.711 A-law),
  the PCM observer should finally receive audio.
- Kept `[audio-diag]` so we can confirm the track now subscribes and `received_bytes` climbs.

## 0.16.6 — audio: find the mic-enable trigger by sniffing the app's RTM
- The 0.16.4 diagnostic proved it live: the robot publishes **video** on join but **no audio
  track at all** (zero `[audio-diag]` subscribe/stats events, both codecs). So audio isn't a
  codec problem — the robot simply isn't sending its mic during passive monitoring. It needs a
  trigger, which the app sends when you tap its audio/listen icon.
- The app and the bridge publish to the **same robot RTM channel** the bridge is subscribed to,
  so added an `[rtm-raw]` debug log of every non-telemetry RTM message. With `log_level: debug`,
  open the official app on live view and tap the audio icon — the exact opcode it sends shows up
  in our log, and we replicate it to enable the mic. (Confirmed the codec is **G.711 A-law**,
  `.g711a`, in the app — payload type 8, as expected.)

## 0.16.5 — audio: set the codec on the ENGINE, before join (the real fix candidate)
- **Root-cause correction:** the codec params (`che.audio.codec_unfallback` +
  `custom_payload_type`) were applied on the *per-connection* handle *after* `connect()`.
  Agora's guidance for this exact case (payload 8 = G.711) is that they must be set on the
  **global engine parameter handle before joining** — after-join never takes effect, which is
  why the PCM observer got 0 frames. Now set on `service.get_agora_parameter()` right after
  `svc.initialize()`, before the RTC connection is even created. **This is the single change
  most likely to finally make the mic produce PCM.**
- Removed the runtime 8↔9 flip — impossible now that the codec is fixed before join. To test
  the other codec, set `audio_codec: 9` and restart (the watchdog says so, and pairs with the
  `[audio-diag] received_bytes` line to tell "no stream" from "can't decode").
- Kept the `[audio-diag]` observer from 0.16.4. Tests (68) + ruff clean.
- **Honest caveat:** not verified against the robot — a well-grounded hypothesis (SDK source +
  Agora codec-8 guidance). If it still logs no PCM *and* `received_bytes>0`, this SDK genuinely
  can't decode the stream and the path forward is a different SDK/transport, not more tweaks.

## 0.16.4 — audio: decisive diagnostic (does the robot even send audio bytes?)
- Confirmed via the actual SDK source: `agora-python-server-sdk` 2.4.9 (the latest) has **no
  working encoded-audio / media-packet receive path** (`register_audio_encoded_frame_observer`
  is a `#todo` stub), so we can't grab undecoded audio to decode ourselves. The only audio path
  is the PCM observer, which needs the SDK to decode — and it won't decode the robot's codec.
- So the whole question is: **does the robot actually publish mic audio in monitor mode?**
  Added a local-user observer that logs `[audio-diag] stats: bitrate=… bytes=…` plus
  `first remote audio FRAME/DECODED`. On the next run the log tells us definitively:
  - `bytes` grows > 0 but never `DECODED` → bytes arrive, SDK can't decode the custom codec.
  - `bytes` stays 0 → the robot isn't sending mic audio here; it needs a trigger command.
- No behaviour change otherwise — pure diagnostics.

## 0.16.3 — fix image-style / auto-record-calls read-back (the two ❌ in your test)
- The live `[settings]` dump proved the robot **never reports `imageStyle` or
  `callAutoRecording`** — that's why those two entities stayed null/false even though the
  command worked. Now the bridge **reflects the value you set optimistically** (write-only
  settings), so the entity updates immediately → both tests go ✅.
- Settings reports are now **merged** into state instead of replacing it, so those optimistic
  values survive the robot's periodic reports (which omit them).

## 0.16.2 — audio: auto-try both codecs + kill the false "stale image" alarm
- **Auto-fallback 8→9:** if payload type 8 yields no PCM within 6 s, the bridge now flips to
  9 at runtime and tries again — **one restart tests both codecs** and logs a definitive
  verdict (`decoding OK with payload_type=N`, or `NO PCM with 8 or 9` → needs self-decode).
- **Fix false "version MISMATCH / stale image" warning:** `VERSION.txt` is now **derived from
  `config.yaml` at build**, so it can't drift behind the released version (that mismatch was
  cosmetic — the new code *was* running, the banner just read a stale baked version string).
- Verified live: 0.16.1's fix (codec params after `connect()`) runs correctly, but payload
  type 8 alone does **not** decode this robot's mic — hence the auto-fallback.

## 0.16.1 — audio: correct codec timing + flip switch + watchdog
- **Fix ordering:** the app sets the codec params *after joining the channel*, per-connection —
  we were setting them *before* `connect()`, which likely never took effect. Now set right
  after connect, matching the app exactly.
- **New option `audio_codec` (8 or 9)** in the add-on UI: 8 = monitor (default), 9 = two-way
  call. The app uses payload type 8 for the watch flow and 9 for calls — if 8 stays silent,
  switch to 9 from the UI (no rebuild) and restart.
- **Watchdog:** if no PCM arrives within 8 s of enabling audio, the log now says so explicitly
  and tells you which value to try next — so we can tell "wrong codec" from "silent playback".

## 0.16.0 — audio: decode the robot's mic codec
- The robot streams its microphone with a **custom telephony codec** (Agora audio payload
  type 8/9), not the default. The bridge now tells the Agora engine to decode it
  (`che.audio.codec_unfallback:[0,8,9]` + `custom_payload_type`) exactly like the app does —
  without this the PCM observer received **0 frames** (why audio never worked).
- Enable with `audio: true` (+ `video: true`). Watch the log for `[audio] first PCM frame`.
- If you still hear nothing, tell me: I'll switch the payload type 8↔9 (the app uses both).

## 0.15.2 — diagnose image-style / call-recording read-back
- Added a **debug log of the raw settings report** (`log_level: debug` → line `[settings] {...}`)
  to see exactly which fields the robot echoes. `image style` and `auto-record calls` set the
  correct command (verified) but didn't reflect in state — this pins down whether it's a
  read-back gap or a condition (e.g. image style may need the camera stream active).
- No entity/behaviour change.

## 0.15.1 — hardening + multi-robot fix
- Fix: the RTSP config file used a fixed `/tmp/mediamtx.yml` path that **collided** when the
  add-on runs one bridge per robot (multi-robot). Now per-instance (keyed by RTSP port).
- Security/quality: added an automated **security + lint pipeline** (ruff, bandit SAST,
  pip-audit dependency CVE scan) — all clean; nonce generation moved to a CSPRNG.
- No functional/entity changes vs 0.15.0.

## 0.15.0 — full control catalog + rich telemetry
Mapped the **entire command set** from the app (112 commands, see `docs/COMMANDS-APK.md`) and
exposed the useful ones as first-class Home Assistant entities.

- **New controls**: rotate by angle, video quality (Low/Medium/High), image style, shoot mode,
  move mode, eyes/emoji mode, autonomous roaming, AI subject tracking, play a preset
  motion/voice by id, and **ask the built-in AI** a question (Air 2 has an LLM agent).
- **New sensors** (from the robot's status report): SD card present + free/total, internal
  storage free, docked, guard/safe mode, current **activity** (moving / charging / AI-tracking /
  on a call / upgrading…), plus diagnostics: camera & MCU **firmware versions**, robot **IP**
  and **WiFi SSID**.
- The camera-setting selects (quality/style/mode) show and set the robot's real current value.
- Everything still routes over Agora RTM, the same channel as before. The raw `ebo/cmd`
  escape hatch remains for any opcode not given its own entity.
- A few controls with complex payloads (eyes, AI track, ask-AI, roaming) are best-effort from
  the decompiled builder; if one misbehaves, the raw `cmd` channel gives exact control.

## 0.14.0 — multi-robot (experimental)
- If your Enabot account has **more than one robot**, the add-on now runs **one bridge per
  robot** automatically: each gets its own device/entities and its own camera on its own RTSP
  port (8554, 8555, …). Set `robot_id: 0` to run all; a specific id runs just that one.
- **Single-robot behaviour is unchanged** (same entities, same `rtsp://…:8554/ebo`).
- Note: the multi-robot path is validated only in design (we can't test 2+ robots here) — if
  you have several EBO and try it, feedback is very welcome.

## 0.13.5 — finer driving + joystick channel
- **Gentler move buttons** (A): each tap is a shorter, smaller nudge — turns no longer spin
  ~90° per press, forward/back are softer.
- **Joystick channel** (B): new MQTT topic `ebo/joystick` accepting `{"x":-1..1,"y":-1..1}`
  for smooth, continuous driving from a joystick card (x = turn, y = forward). Pair it with the
  EBO joystick Lovelace card. (Cloud latency still applies.)

## 0.13.4 — much lower video latency
- The stream lagged because ffmpeg was forced to 15 fps (`-r`/`-vsync cfr`) while the robot
  sends ~25 fps → it buffered and dropped frames. Now it passes the real frame rate through
  with arrival timestamps + low-delay flags: **latency should drop a lot** (clean DTS kept).
- For the lowest latency/CPU use `video_preset: ultrafast` and a lower `video_max_height`.

## 0.13.3 — audio no longer breaks video
- With `audio: true`, ffmpeg had a second (audio) input; if the robot's PCM didn't arrive it
  **stalled the whole mux and froze the video**. Fixed: (1) the audio observer is now kept
  referenced (it was garbage-collected, so it never fired), and (2) the pipeline feeds
  **silence** when no real audio arrives, so ffmpeg never blocks — **video always flows**,
  with audio overlaid when the robot sends it.

## 0.13.2 — quieter log + log level
- New **`log_level`** option: `info` (default) shows key events only — no more `N frames
  received` spam; `debug` for the chatty lines; `warning` for problems only. Video keeps a
  light "still streaming" heartbeat every few minutes at info level.

## 0.13.1 — audio fix + diagnostics
- Audio didn't work because a required SDK call was missing:
  `set_playback_audio_frame_before_mixing_parameters(1, 16000)` — without it the PCM callback
  never fires. Added it (+ `audio_recv_media_packet=0`). The log now shows
  `[audio] first PCM frame from …` when audio is flowing.
- Silenced transient template warnings on the new entities (defaults).

## 0.13.0 — audio (listen), experimental
- Optional **audio**: the robot's microphone (16 kHz mono PCM from the SDK) is muxed into the
  camera stream as AAC, so the Generic Camera has **sound**. Enable with `audio: true` (needs
  `video: true`). Off by default; if it ever misbehaves the safety net falls back to
  control-only. Two-way *talk* is a separate future step.

## 0.12.1 — camera stream: fix timestamps ("No dts")
- The re-encoded stream could produce timestamps HA's stream backend rejected ("No dts in N
  consecutive packets"). ffmpeg now timestamps incoming frames by arrival
  (`use_wallclock_as_timestamps`) and forces a constant output rate (`-r`, CFR), giving clean
  monotonic DTS/PTS. (The `Connection refused`/`404` errors were just the add-on being down
  during the update — transient.)

## 0.12.0 — "connected" switch + CI
- **EBO connected** switch (default on): turn it **off** to fully leave the cloud session so
  the robot can **sleep** (no control/telemetry while off); turn it back on to reconnect. MQTT
  entities stay available throughout.
- **CI:** a GitHub Actions workflow builds the add-on image on every push/PR, so build breaks
  are caught before release.

## 0.11.0 — more entities
- New controls (verified against the app): **motion recording** (switch), **auto-record calls**
  (switch), **cloud upload** (switch, privacy), **talkback volume** (number). The recording/
  volume ones show real state from the robot's settings report.
- Eyes/emoji, DND and other complex settings stay on the raw `ebo/cmd` channel (they need
  structured payloads) — see COMANDI.md.

## 0.10.0 — video CPU: resolution/quality options
- The robot streams ~2304×1296 (2K); re-encoding that is CPU-heavy on a NUC. New options:
  `video_max_height` (default **720** — big CPU saving; set `0` for native 2K) and
  `video_preset` (libx264 speed/quality). The log shows the chosen resolution/preset.

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
  base**, plus a raw `ebo/cmd` channel to send any opcode (for automations / AI).
- **Clean shutdown** (no more "error" on stop; logs stay readable).
- Renamed add-on to **Enabot integration**; repository is now the multi-add-on
  **Playcolors.co** collection.
- Fixed **return to base** (correct opcode) and removed the invalid patrol/AI-tracking buttons
  (they need structured payloads — documented in COMANDI.md).

## 0.3.0
- Video off by default (Agora Python SDK can't receive the robot's H.265 at the time).

## 0.2.x
- Initial control + telemetry over the Enabot cloud (Agora RTM/RTC) with MQTT Discovery.
