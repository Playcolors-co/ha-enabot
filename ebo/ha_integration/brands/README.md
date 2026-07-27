# Brand icon for Home Assistant

Home Assistant shows a custom integration's icon **only if it is in the official
[home-assistant/brands](https://github.com/home-assistant/brands) repository** — HA/HACS do not
read an icon from this repo directly. Until then the integration shows a generic icon (it still
works fully).

The ready-made assets are here: `icon.png` (256×256), `icon@2x.png` (512×512), `logo.png`.

## To get the icon into Home Assistant (one-time PR)
1. Fork **https://github.com/home-assistant/brands**.
2. Add the files under **`custom_integrations/ebo/`**:
   - `icon.png`  ← this folder's `icon.png` (256×256)
   - `icon@2x.png` ← this folder's `icon@2x.png` (512×512)
   - `logo.png` (optional) ← this folder's `logo.png`
3. Open a pull request. Once merged, the Enabot icon appears on the integration everywhere in HA.
