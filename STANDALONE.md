# Running without the add-on (HA Container / HA Core)

Home Assistant **Container** and **Core** installs have no Supervisor, so they can't run add-ons.
You can still use EBO by running the **engine** (the same code as the add-on) as a plain Docker
container, and connecting the **EBO integration** to it manually.

> ⚠️ **You need an x86_64 (amd64) machine for the engine.** The Agora SDK it depends on is
> x86_64-only — there is no ARM build, and emulating it on a Raspberry Pi is far too slow. If your
> Home Assistant runs on a Raspberry Pi (ARM), keep HA there and run this engine on a **separate
> small x86_64 box** (a NUC, mini-PC, an old laptop, or a VM). On HA OS / Supervised you don't need
> any of this — just install the add-on.

## 1) Run the engine

Grab [`docker-compose.yml`](docker-compose.yml), fill in the environment (your Enabot
email/password, the two app keys, a token you choose, and your LAN IP), then:

```bash
docker compose up -d
docker compose logs -f      # watch it log in and start
```

The engine exposes **RTSP** on `8554` and the **data API** on `8098`.

- **The two app keys** (`EBO_PAYLOAD_KEY` / `EBO_SIGN_KEY`) are constants from the EBO HOME app,
  not shipped here — see the add-on docs → "App crypto keys".
- **`EBO_API_TOKEN`** is any long random string you pick; you'll paste it into HA below.
- **Robots are native EBO devices — no MQTT.**

## 2) Add the integration in Home Assistant

The integration is **bundled in the image** — no HACS. If you mount your Home Assistant config dir
(uncomment the `/homeassistant` volume in the compose), the engine installs it for you; otherwise
copy `ha_integration/custom_components/ebo` into your HA `custom_components/`. Then:

1. **Restart Home Assistant** (to load the integration).
2. **Settings → Devices & Services → + Add Integration → EBO**. With no Supervisor it opens the
   **manual** step — fill in:
   - **RTSP URL**: `rtsp://<engine-ip>:8554/ebo`
   - **API URL**: `http://<engine-ip>:8098`
   - **API token**: the `EBO_API_TOKEN` you set
   - (name, and optionally serial/MAC/model)
3. Submit → you get the robot's device with a **live camera** + native entities.

Add one integration entry per robot (extra robots stream on `8555`, `8556`, `8557`).

## Notes

- **No MQTT anywhere.** The integration talks straight to the engine's API.
- **Command lag** is the Agora **cloud** round-trip, the same as the app — not something the engine
  or the transport adds. Local control isn't possible on these models.
- **Build from source** instead of the published image: clone this repo and set `build: ./ebo`
  in the compose file (drop the `image:` line).
