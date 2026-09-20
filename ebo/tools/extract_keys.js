/*
 * extract_keys.js — read YOUR OWN EBO HOME app's two signing keys at runtime, with Frida.
 *
 * WHY: on current EBO HOME builds the two 16-char constants are no longer in the Java/DEX. The
 * native library libeboSignature.so resolves them at runtime (loadSignatureResources) and writes
 * the DEOBFUSCATED values back into Java string fields:
 *     bodyEncryptKeyS2      -> payload_key   (AES-128-GCM request body)
 *     headerAccessKeySecret -> sign_key       (HMAC x-ebo-sign header)
 * So instead of reversing the .so transform, we just read those fields AFTER the loader has run.
 * This reads the keys from YOUR OWN running app; nothing is shipped, nothing leaves your device.
 *
 * USAGE (see tools/README.md for details):
 *     # spawn the app under Frida, then log in in the app when it opens:
 *     frida -U -f com.enabot.ebox.intl -l extract_keys.js
 *     # or attach to the already-running app:
 *     frida -U -n "EBO HOME" -l extract_keys.js
 *
 * The script prints each key as soon as it sees a non-empty value. Trigger a signed request
 * (just log in, or open your robot) to make the app populate the fields.
 *
 * This is for making YOUR OWN device work with YOUR OWN Home Assistant. Do NOT share the printed
 * values — they are Enabot's app-level constants, the same reason this project never ships them.
 */
'use strict';

// Field names the native loader fills. The first two are the ones the add-on needs.
var WANT = {
  bodyEncryptKeyS2:      'payload_key   (AES body key)',
  headerAccessKeySecret: 'sign_key      (x-ebo-sign HMAC key)',
  verificationCaptchaId: 'captcha id    (not needed by the add-on)'
};
// Only inspect classes whose name hints at signing/crypto, so we don't Java.use() the whole app.
var CLASS_HINT = /(signature|sign|encrypt|crypto|ebo)/i;

var seen = {};          // dedupe: field=value already printed
var hooked = {};        // classes whose methods we've already hooked (don't stack wrappers on re-scan)

function printKey(fieldName, value, where) {
  if (value === null || value === undefined) return;
  var v = '' + value;
  if (v.length === 0) return;
  var dedupe = fieldName + '=' + v;
  if (seen[dedupe]) return;
  seen[dedupe] = true;
  console.log('');
  console.log('  ========================================================');
  console.log('  FOUND  ' + (WANT[fieldName] || fieldName));
  console.log('         ' + fieldName + ' = "' + v + '"   (length ' + v.length + ')');
  console.log('         source: ' + where);
  console.log('  ========================================================');
  console.log('');
}

// Read the WANT fields off a class wrapper (statics) or an instance wrapper.
function readWant(wrapper, where) {
  if (!wrapper) return;
  Object.keys(WANT).forEach(function (name) {
    try {
      var holder = wrapper[name];               // Frida exposes fields (incl. private) with .value
      if (holder && holder.value !== undefined) {
        printKey(name, holder.value, where);
      }
    } catch (e) { /* not this class / not accessible */ }
  });
}

// Does this class declare any of the fields we're after?
function declaresWant(Cls) {
  try {
    var fields = Cls.class.getDeclaredFields();
    for (var i = 0; i < fields.length; i++) {
      var n = fields[i].getName();
      if (WANT.hasOwnProperty(n)) return true;
    }
  } catch (e) { }
  return false;
}

function instrument(name) {
  var Cls;
  try { Cls = Java.use(name); } catch (e) { return false; }
  if (!declaresWant(Cls)) return false;

  // 1) maybe they're static and already filled — read now (every scan, values may appear late).
  readWant(Cls, name + ' (static, at scan time)');

  // Hook only once per class — re-scans must not stack wrappers on the same method.
  if (hooked[name]) return true;
  hooked[name] = true;
  console.log('[*] candidate class holding the key fields: ' + name);

  // 2) hook the native-backed loader so we read the fields right after it fills them.
  ['loadSignatureResources', 'init', 'initSignature', 'load'].forEach(function (m) {
    try {
      if (!Cls[m]) return;
      Cls[m].overloads.forEach(function (ov) {
        ov.implementation = function () {
          var r = ov.apply(this, arguments);
          try { readWant(this, name + '.' + m + '() -> instance'); } catch (e) { }
          try { readWant(Cls,  name + '.' + m + '() -> static'); } catch (e) { }
          return r;
        };
      });
      console.log('    hooked ' + name + '.' + m + '()');
    } catch (e) { }
  });

  // 3) capture instances via the constructor, in case the fields are per-instance and filled later.
  try {
    Cls.$init.overloads.forEach(function (ov) {
      ov.implementation = function () {
        var r = ov.apply(this, arguments);
        try { readWant(this, name + ' (new instance)'); } catch (e) { }
        return r;
      };
    });
  } catch (e) { }

  return true;
}

Java.perform(function () {
  console.log('[*] EBO key extractor running. Log in / open your robot in the app to trigger it.');
  var hits = 0;

  // Scan currently-loaded classes for the ones that declare the key fields.
  Java.enumerateLoadedClasses({
    onMatch: function (name) {
      if (!CLASS_HINT.test(name)) return;
      try { if (instrument(name)) hits++; } catch (e) { }
    },
    onComplete: function () {
      if (hits === 0) {
        console.log('[!] No key-holding class loaded yet — it may load on demand.');
        console.log('    Leave this running and LOG IN in the app; re-scanning every 2s…');
      }
    }
  });

  // Re-scan + re-read periodically: the class/fields often appear only after you log in.
  var elapsed = 0;
  var timer = setInterval(function () {
    elapsed += 2;
    Java.perform(function () {
      Java.enumerateLoadedClasses({
        onMatch: function (name) { if (CLASS_HINT.test(name)) { try { instrument(name); } catch (e) { } } },
        onComplete: function () { }
      });
    });
    if (elapsed >= 120) { clearInterval(timer); console.log('[*] stopping the re-scan (2 min elapsed).'); }
  }, 2000);
});
