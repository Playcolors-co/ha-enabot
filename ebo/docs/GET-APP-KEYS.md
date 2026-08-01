# How to get the two app keys (`payload_key` and `sign_key`)

The add-on talks to Enabot's cloud the same way the official **EBO HOME** app does. That API signs and
encrypts every request with two 16-character keys that live inside the app.

**These keys are not shipped with this add-on** — they're Enabot's, not ours, so we don't redistribute
them. You extract them **once**, from **your own copy of the app**, to make your own robot work with
your own Home Assistant. Then you paste them into the add-on's Configuration tab.

> Shipping them "encrypted" inside the add-on wouldn't help: whatever decrypts them would have to ship
> too, so anyone could read them anyway. Keeping them user-supplied is both safer and cleaner.

---

## What you need

| Tool | What for | Link |
|---|---|---|
| **jadx** (jadx-gui) | opens the app and shows its code | <https://github.com/skylot/jadx/releases> |
| **A way to get the APK** | pull the app file off your phone | see step 1 |
| *(optional)* **adb / platform-tools** | pull the APK over USB | <https://developer.android.com/tools/releases/platform-tools> |

Java is required by jadx; if it complains, install a JDK (e.g. <https://adoptium.net>).

---

## Step 1 — Get the EBO HOME APK from your phone

The app's package name is **`com.enabot.ebox.intl`**. Pick whichever is easiest:

**A. With an extractor app (no PC needed to export)**
- **App Manager** — <https://github.com/MuntashirAkon/AppManager/releases> (also on F-Droid:
  <https://f-droid.org/packages/io.github.muntashirakon.AppManager/>)
- Open it → find **EBO HOME** → *Export/Save APK* → copy the file to your computer.

**B. With adb over USB** (developer options + USB debugging enabled)
```bash
adb shell pm path com.enabot.ebox.intl
# prints e.g. package:/data/app/~~xxxx/com.enabot.ebox.intl-yyyy/base.apk
adb pull /data/app/.../base.apk ebo.apk
```

If you get several files (`base.apk`, `split_*.apk`), the one you want is **`base.apk`**.

---

## Step 2 — Open it with jadx

- **GUI:** launch `jadx-gui`, open `ebo.apk`, and wait for it to finish decompiling (a few minutes).
- **CLI:** `jadx -d ebo-src ebo.apk` → sources land in `ebo-src/sources/`.

---

## Step 3 — Find the keys

The class that signs and encrypts the cloud requests is:

```
com.enabot.lib_ebo.netWork.ServerEncryptHelper
```

- In **jadx-gui**: press **Ctrl/Cmd + Shift + F** (search in code) and search for
  `ServerEncryptHelper`, then open the class.
- With the **CLI**: `grep -rn "class ServerEncryptHelper" ebo-src/sources/`

Inside that class you'll find two constants near the top — the field names are obfuscated (something
like `f24161b`, `f24162c`), but their **values are two 16-character strings**:

- the one used as the **AES key for the request body** → this is your **`payload_key`**
- the one used to build the **request signature** (the `x-ebo-sign` header) → this is your **`sign_key`**

If you're unsure which is which, look at how each constant is used in the same class: the one passed to
the AES/cipher code is the payload key; the one concatenated into the string that gets hashed
(SHA-256) is the sign key. Worst case, try one order — if login fails, swap them.

> Tip: both are exactly **16 characters** and contain punctuation. Copy them **exactly**, with no
> added spaces, and don't let your editor "smart-quote" any character.

---

## Step 4 — Put them in the add-on

Home Assistant → **Settings → Add-ons → EBO → Configuration**:

- `payload_key` → the AES key from step 3
- `sign_key` → the signing key from step 3
- also fill in your **Enabot account email/password**, and the `region`/`host` that match your account

Save and (re)start the add-on. The log should show it logging in and finding your robot.
If login fails with a signature error, you most likely swapped the two keys.

---

## Keep them to yourself

- They're stored only in your own Home Assistant (masked in the UI, never written to the add-on log).
- **Don't post them** in issues, forums, screenshots or public repos — that's exactly what this
  project avoids doing.
- This procedure is for making **your own device** work with **your own** Home Assistant.
