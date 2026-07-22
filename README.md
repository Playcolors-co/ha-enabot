# Enabot for Home Assistant

Home Assistant **add-ons for Enabot EBO robots** — control, telemetry and camera, without the
phone app. Community project; contributions for other EBO models are welcome.

> ⚠️ Independent, unofficial project. Not affiliated with Enabot or ThroughTek/Agora. It
> interoperates using **your own** credentials and device, through reverse engineering. Use at
> your own risk; it may break if Enabot changes their API/firmware.

## Add-ons

| add-on | model | control | telemetry | camera | transport |
|--------|-------|:------:|:---------:|:------:|-----------|
| **[EBO Air 2](ebo_air2/README.md)** | EBO Air 2 | ✅ | ✅ | ✅ (H.265→H.264 RTSP) | Enabot cloud (Agora) |
| _EBO SE_ | EBO SE | — | — | — | see [ebo-se-lan-bridge](https://github.com/lilium360/ebo-se-lan-bridge) (LAN/Kalay) |
| _EBO Max / EBO X / SE 2_ | — | — | — | — | wanted — contributions welcome |

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories**
2. Add this URL:
   ```
   https://github.com/Playcolors-co/ha-enabot
   ```
3. Install the add-on for your model (e.g. **EBO Air 2**) and follow its
   [docs](ebo_air2/DOCS.md).

## Why this exists

The Home Assistant community has asked for an Enabot integration
[since 2021](https://community.home-assistant.io/t/enabot-ebo-integration-camera-with-wheels/328355).
The EBO robots are cloud-locked (or LAN via Kalay on some models), so there is no official
integration. This repo collects working, per-model add-ons:

- **EBO Air 2** is **cloud-only for real-time** (live video + control go over Agora), so this
  add-on replicates the app's cloud flow: encrypted login → Agora RTM (control/telemetry) +
  RTC (video). The robot streams **H.265**; the add-on decodes it with the Agora SDK and
  re-encodes to **H.264 / RTSP** for a Generic Camera.
- The **EBO SE** speaks **Kalay/TUTK over the LAN** and is covered by the excellent
  [ebo-se-lan-bridge](https://github.com/lilium360/ebo-se-lan-bridge) by **lilium360** — a
  different (local) transport. Complementary to this repo.

## Contributing

Adding another EBO model, fixing bugs, improving the camera — all welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md). Each model lives in its own folder with its own
`config.yaml`; the shared cloud/crypto helpers can be reused.

## Support

Free, independent work. If it's useful to you:

[![Buy me a coffee](https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20coffee&emoji=%E2%98%95&slug=scattolacom&button_colour=FFDD00&font_colour=000000&font_family=Lato&outline_colour=000000&coffee_colour=ffffff)](https://www.buymeacoffee.com/scattolacom)

## License

MIT (see [LICENSE](LICENSE)). No proprietary Enabot/ThroughTek/Agora component is included or
redistributed.
