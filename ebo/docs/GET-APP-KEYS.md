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
| **jadx** (jadx-gui) | opens the app and shows its code — finds the `payload_key` | <https://github.com/skylot/jadx/releases> |
| **A way to get the APK** | pull the app file off your phone | see step 1 |
| *(optional)* **adb / platform-tools** | pull the APK over USB | <https://developer.android.com/tools/releases/platform-tools> |
| *(recent app builds)* **Frida** | read the `sign_key` from the app's **native** library at runtime | <https://frida.re> |

Java is required by jadx; if it complains, install a JDK (e.g. <https://adoptium.net>).

> **Heads-up on app versions.** On older EBO HOME builds **both** keys sat in the Java code and jadx
> alone was enough. On **recent builds the signing key was moved into a native library**
> (`libeboSignature.so`), so jadx shows you only the `payload_key` — that's why a plain jadx search now
> comes up short. Step 3 covers both cases.

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

### 3a — `payload_key` (with jadx)

The class that encrypts the cloud request body is:

```
com.enabot.lib_ebo.netWork.ServerEncryptHelper
```

- In **jadx-gui**: press **Ctrl/Cmd + Shift + F** (search in code), search for `ServerEncryptHelper`,
  and open the class.
- With the **CLI**: `grep -rn "class ServerEncryptHelper" ebo-src/sources/`

Near the top you'll find a **16-character string** passed to the AES/cipher code — that's your
**`payload_key`**. (Field names are obfuscated, e.g. `f24161b`.) On older builds you'll also see a
second 16-char constant right next to it used to build the signature — on those builds that's your
`sign_key` and you're done. On recent builds it isn't in the Java code any more → do **3b**.

### 3b — `sign_key` (recent builds: it's in the native library)

On current EBO HOME versions the signing key lives in the native library **`libeboSignature.so`**, not
in the Java code, so jadx won't show it. Two ways to get it, in order of ease:

- **Frida (easiest).** With Frida attached to the running app, hook the app's signing routine and read
  the value it uses (the app builds the `x-ebo-sign` header from it). This reads the key from **your
  own** running app; nothing leaves your device.
- **Reverse the `.so`.** Unzip the APK, take `lib/arm64-v8a/libeboSignature.so`, and open it in a
  disassembler (Ghidra/IDA) to recover the constant.

> Not sure you've got the right one? The `sign_key` is what builds the `x-ebo-sign` request signature
> (an HMAC-SHA256). If the add-on logs a **signature error** on login, the `sign_key` is the one to
> re-check.

> Tip: both keys are exactly **16 characters** and contain punctuation. Copy them **exactly**, with no
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
