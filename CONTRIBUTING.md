# Contributing

Thanks for helping bring Enabot robots into Home Assistant! Contributions are welcome —
bug fixes, camera/perf improvements, and especially **support for other EBO models**.

## Repository layout

```
ha-enabot/
├── repository.yaml        # HA add-on repository descriptor
├── ebo_air2/              # one add-on per model (own config.yaml, Dockerfile, docs)
│   ├── config.yaml        # add-on manifest (name, slug, options, ports…)
│   ├── Dockerfile         # image (python:3.11-slim + Agora SDK + ffmpeg + mediamtx)
│   ├── run.sh             # entrypoint: reads /data/options.json, MQTT + host IP from Supervisor
│   ├── ebo_bridge.py      # main bridge: RTM control/telemetry, RTC video, MQTT Discovery
│   ├── ebo_cloud.py       # Enabot cloud login + session (Agora tokens)
│   ├── ebo_sign.py        # request signing
│   ├── ebo_video.py       # decoded YUV → ffmpeg (H.264) → RTSP
│   ├── COMANDI.md         # full opcode catalog
│   └── VERSION.txt        # baked version (logged at startup to detect stale builds)
└── <ebo_se>/ , <ebo_max>/ …   # future models
```

## Adding a new model

1. Copy `ebo_air2/` to `<model>/` and set a unique `slug` + `name` in `config.yaml`.
2. Reuse the cloud/crypto helpers where possible; each model has its **own opcodes** and RTM
   data shapes — reverse them from the app and document in the add-on's `COMANDI.md`.
3. Keep `version` and `VERSION.txt` in sync when you release (the startup log compares the
   baked version with what the Supervisor installed, to catch stale rebuilds).
4. Bump the version so Home Assistant offers **Update** (not Rebuild).

## How the EBO Air 2 add-on works (reference)

- **Control needs RTC presence:** the robot only accepts RTM commands while you are also
  joined to the Agora RTC channel (join with `publishCameraTrack=false`).
- **Video:** the Agora *encoded* frame observer segfaults for H.265, but the SDK **decodes**
  H.265. So we set `AgoraServiceConfig.enable_video = 1`, register a decoded
  `register_video_frame_observer`, take the I420 YUV and re-encode to H.264 with ffmpeg →
  RTSP (mediamtx). Keyframes every ~2 s so clients attach fast.
- **Entities** are exposed via **MQTT Discovery**; a raw `ebo_air2/cmd` topic accepts any
  opcode for automations / AI.

## Guidelines

- Don't commit credentials, tokens, or captures.
- Keep the code style of the surrounding files.
- Open an issue first for larger changes so we can align.
