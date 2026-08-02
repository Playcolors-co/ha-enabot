"""
panel.py — the add-on's Ingress web UI (a Zigbee2MQTT-style sidebar panel, "Enabot").

ONE instance for the whole add-on. It subscribes to MQTT to aggregate every robot's state, shows a
LIST of robots (click one → its detail page with preview + controls + settings), forwards safe
control/settings commands over MQTT, and edits operational add-on settings stored in
/data/panel.json (read by run.sh at boot). Live preview = on-demand JPEG from each robot's RTSP.

No extra dependencies: stdlib http.server + paho-mqtt + ffmpeg.
"""
import base64
import io
import json
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paho.mqtt.client as mqtt
import segno

import ebo_cloud
from ebo_log import log

EMAIL = os.environ.get("EBO_EMAIL", "")
PASSWORD = os.environ.get("EBO_PASSWORD", "")
REGION = os.environ.get("EBO_REGION", "GB")
HOST = os.environ.get("EBO_HOST", "ebox-eu.enabotserverintl.com")

PORT = int(os.environ.get("EBO_PANEL_PORT", "8099"))
MQTT_HOST = os.environ.get("EBO_MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.environ.get("EBO_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("EBO_MQTT_USER", "") or None
MQTT_PASS = os.environ.get("EBO_MQTT_PASS", "") or None
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
API_PORT = int(os.environ.get("EBO_API_PORT", "8098"))
API_TOKEN = os.environ.get("EBO_API_TOKEN", "")

# Command suffixes the panel may publish (allow-list). Movement is excluded on purpose.
ALLOWED_CMDS = {
    "camera/set", "laser/set", "dock", "sleep/set", "wake", "connected/set",
    "say", "talk",
    "video_quality/set", "image_style/set", "volume/set", "talkback_volume/set",
    "speed/set", "sports_record/set", "call_rec/set", "eyes/set",
    "move_mode/set", "avoid_obstacle/set", "night_vision/set", "listen/set",
    "patrol/start", "patrol/stop", "patrol/route/set",
    "route/record/start", "route/record/stop", "route/save", "route/delete", "talk/stop",
    # raw opcode escape hatch for AI/automation (and the eyes protocol): {"id":<op>,"data":{...}}
    "cmd",
}

# All add-on settings (account/connection + audio/video processing) now live in the add-on
# Configuration tab (/data/options.json), read by run.sh — the panel no longer has a Settings dialog.
# Per-robot settings (video quality, eyes, volume, speed…) are on the robot's detail page.

_robots = {}
_lock = threading.Lock()
_snap_cache = {}
_snap_fail = {}      # node -> ts of the last failed grab (backoff while the robot sleeps)
# Last good frame, also kept on disk: the panel restarts with the add-on (updates, crashes), and an
# in-memory-only cache would leave you staring at a blank tile until the robot wakes up again.
_SNAP_DIR = os.environ.get("EBO_SNAP_DIR", "/data")


def _snap_path(node):
    safe = "".join(c for c in str(node) if c.isalnum() or c in "-_")
    return os.path.join(_SNAP_DIR, "last_frame_%s.jpg" % (safe or "ebo"))


def _snap_load(node):
    """Last frame from a previous run (so a restart doesn't blank the thumbnails)."""
    try:
        with open(_snap_path(node), "rb") as f:
            data = f.read()
        if data:
            _snap_cache[node] = (0, data)      # ts=0: stale, so a live grab is always preferred
            return data
    except Exception:
        pass
    return None


def _snap_store(node, data):
    try:
        tmp = _snap_path(node) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, _snap_path(node))
    except Exception:
        pass
_snap_lock = {}
_client = None


# --------------------------- MQTT: aggregate every robot's state ---------------------------
def _robot(node):
    return _robots.setdefault(node, {"node": node, "online": False, "state": {}})


def _on_connect(client, userdata, flags, rc, properties=None):
    log("[panel] MQTT connected rc=%s" % rc)
    for t in ("ebo/discovery/#", "+/status", "+/state", "+/camera/state", "+/camera/url"):
        client.subscribe(t)


def _on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace")
        if topic.startswith("ebo/discovery/"):
            data = json.loads(payload) if payload else {}
            node = data.get("node") or topic.rsplit("/", 1)[-1]
            with _lock:
                _robot(node).update({k: data.get(k) for k in
                                     ("name", "sn", "mac", "model", "rtsp")})
            return
        node = topic.split("/", 1)[0]
        # the +/status, +/state wildcards also catch non-EBO topics (e.g. homeassistant/status) —
        # only track real EBO nodes (from discovery, or the ebo prefix).
        if node not in _robots and not node.startswith("ebo"):
            return
        leaf = topic[len(node) + 1:]
        with _lock:
            r = _robot(node)
            if leaf == "status":
                r["online"] = (payload == "online")
            elif leaf == "state":
                try:
                    r["state"] = json.loads(payload)
                except ValueError:
                    pass
            elif leaf == "camera/state":
                r["camera"] = payload
            elif leaf == "camera/url":
                r["url"] = payload
    except Exception as e:
        log("[panel] message error:", e)


def _start_mqtt():
    global _client
    c = mqtt.Client(client_id="ebo_panel")
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.on_connect = _on_connect
    c.on_message = _on_message
    c.connect(MQTT_HOST, MQTT_PORT, 30)
    c.loop_start()
    _client = c


# --------------------------- operational settings (/data/panel.json) ---------------------------
def _restart_self():
    time.sleep(1)
    try:
        req = urllib.request.Request("http://supervisor/addons/self/restart",
                                     data=b"", method="POST")
        req.add_header("Authorization", "Bearer " + SUPERVISOR_TOKEN)
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        log("[panel] self-restart failed:", e)


# --------------------------- pair a NEW robot (QR provisioning) ---------------------------
_pair = {}          # {key, qr, account, client}


def _b64(s):
    return base64.b64encode((s or "").encode()).decode()


def _pair_start(ssid, password):
    """Log in, mint a bind key, and build the WiFi QR string the robot's camera will scan."""
    c = ebo_cloud.EboCloud(host=HOST)
    c.login(EMAIL, PASSWORD, region=REGION)
    acc = c.account_id
    if not acc:
        raise RuntimeError("could not read the account id from login")
    resp = c.bind_key(acc)
    key = (resp.get("data") or {}).get("bind_key") or resp.get("bind_key")
    if not key:
        raise RuntimeError("no bind_key in response: %s" % resp)
    qr = "s=%s&p=%s&m=2&k=%s&r=%s" % (_b64(ssid), _b64(password), _b64(key),
                                      _b64(ebo_cloud.region_code(HOST)))
    _pair.clear()
    _pair.update({"key": key, "qr": qr, "account": acc, "client": c})
    log("[pair] bind key obtained — showing QR (region %s)" % ebo_cloud.region_code(HOST))
    return {"ok": True}


def _pair_status():
    if not _pair:
        return {"status": "idle"}
    try:
        resp = _pair["client"].bind_status(_pair["account"], _pair["key"])
    except Exception as e:
        return {"status": "error", "error": str(e)}
    data = resp.get("data") or {}
    st = data.get("bind_status")
    if st == 200:
        log("[pair] robot bound — robot_id=%s" % data.get("robot_id"))
    return {"status": st, "robot_id": data.get("robot_id")}


def _qr_png():
    if not _pair:
        return None
    buf = io.BytesIO()
    segno.make(_pair["qr"], error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


def _find_rid_by_sn(obj, sn):
    """Find a robot_id in the account's robot list by matching serial number."""
    if isinstance(obj, dict):
        if sn and sn in {str(v) for v in obj.values() if isinstance(v, (str, int))}:
            for k in ("robot_id", "robotId", "id"):
                if obj.get(k) is not None:
                    return obj[k]
        for v in obj.values():
            r = _find_rid_by_sn(v, sn)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_rid_by_sn(v, sn)
            if r is not None:
                return r
    return None


def _remove_robot(node):
    """Unbind a robot from the account (DESTRUCTIVE). Prefer the known robot_id; else look up by SN."""
    with _lock:
        r = _robots.get(node)
    if not r:
        raise RuntimeError("unknown robot")
    rid = r.get("robot_id")
    c = ebo_cloud.EboCloud(host=HOST)
    c.login(EMAIL, PASSWORD, region=REGION)
    if not rid:
        rid = _find_rid_by_sn(c.robots(), str(r.get("sn") or ""))
    if not rid:
        raise RuntimeError("could not resolve the robot's id on the account")
    resp = c.unbind(rid)
    log("[remove] unbound robot_id=%s (%s) -> code=%s" % (rid, r.get("name"), resp.get("code")))
    threading.Thread(target=_restart_self, daemon=True).start()
    return {"ok": True, "robot_id": rid}


# --------------------------- live preview: one JPEG from RTSP ---------------------------
def _snapshot(node):
    with _lock:
        r = _robots.get(node)
        url = r and r.get("rtsp")
        asleep = bool(r) and r.get("camera") != "on"
    now = time.time()
    ts, cached = _snap_cache.get(node, (0, None))
    # Robot asleep (ZZ) or no stream: there's nothing to grab, and trying costs a multi-second
    # ffmpeg timeout on every refresh — which made the thumbnails go blank and the panel sluggish.
    # Serve the LAST frame we saw instead, so you still see where the robot is.
    if not url or asleep:
        return cached or _snap_load(node)
    # Same when a grab just failed (stream still coming up): don't retry in a tight loop.
    if cached and now - _snap_fail.get(node, 0) < 5:
        return cached
    if cached and now - ts < 0.25:          # short cache: keep frames FRESH (low latency > fps)
        return cached
    lock = _snap_lock.setdefault(node, threading.Lock())
    if not lock.acquire(blocking=False):    # one grab per node at a time (no ffmpeg pile-up)
        return cached
    try:
        ts, cached = _snap_cache.get(node, (0, None))
        if cached and time.time() - ts < 0.25:
            return cached
        p = urlparse(url)
        internal = "rtsp://127.0.0.1:%s%s" % (p.port or 8554, p.path)
        # grab the FRESHEST frame with minimal buffering: no probe/analyze delay, no jitter buffer.
        out = subprocess.run(
            # NOTE: probesize used to be 32 bytes for minimum latency. That silently broke every
            # grab as soon as the stream also carried an Opus audio track — ffmpeg could no longer
            # identify the streams, so the panel kept showing one frozen frame (and it looked like
            # the robot had stopped responding). Probe a little more, and take video only.
            ["ffmpeg", "-nostdin", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "200k", "-analyzeduration", "300000", "-rtsp_transport", "tcp",
             "-i", internal, "-an", "-map", "0:v:0",
             "-frames:v", "1", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
            capture_output=True, timeout=5).stdout
        if out:
            _snap_cache[node] = (time.time(), out)
            _snap_fail.pop(node, None)
            _snap_store(node, out)
            return out
        _snap_fail[node] = time.time()
    except Exception:
        _snap_fail[node] = time.time()
    finally:
        lock.release()
    return cached or _snap_load(node)


# --------------------------- HTTP: dashboard + tiny API ---------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        # the ingress server (8099) is authenticated by HA; the API port (8098) needs the token
        return (not getattr(self.server, "require_token", False)
                or self.headers.get("X-Enabot-Token") == API_TOKEN)

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return self._send(403, json.dumps({"error": "forbidden"}))
        # HLS proxy: play the fluid Low-Latency HLS through Ingress (same origin → no CSP/CORS
        # trouble with a cross-origin port). /hlsp/<port>/<rest> -> 127.0.0.1:<port>/<rest>.
        if "/hlsp/" in self.path:
            rest = self.path.split("/hlsp/", 1)[1]
            port, _, sub = rest.partition("/")
            return self._proxy_hls(port, sub)
        path = urlparse(self.path).path.rstrip("/")
        if path.endswith("/hls.min.js"):
            try:
                with open("/app/hls.min.js", "rb") as f:
                    return self._send(200, f.read(), "application/javascript")
            except Exception:
                return self._send(404, b"", "text/plain")
        if path.endswith("/api/robots"):
            with _lock:
                return self._send(200, json.dumps(list(_robots.values())))
        if path.endswith("/api/account"):
            return self._send(200, json.dumps({"email": EMAIL}))
        if path.endswith("/api/snapshot"):
            q = parse_qs(urlparse(self.path).query)
            jpg = _snapshot((q.get("node") or [""])[0])
            return self._send(200 if jpg else 404, jpg or b"", "image/jpeg")
        if path.endswith("/api/pair/qr"):
            png = _qr_png()
            return self._send(200 if png else 404, png or b"", "image/png")
        if path.endswith("/api/mjpeg"):
            q = parse_qs(urlparse(self.path).query)
            return self._mjpeg((q.get("node") or [""])[0])
        return self._send(200, PAGE, "text/html; charset=utf-8")

    def _proxy_hls(self, port, sub):
        """Forward an HLS request to the local mediamtx (127.0.0.1:<port>). Restricted to the HLS
        ports so it can't be used as an open proxy."""
        if not port.isdigit() or not (8888 <= int(port) <= 8891):
            return self._send(400, b"", "text/plain")
        url = "http://127.0.0.1:%s/%s" % (port, sub)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self._send(502, b"", "text/plain")

    def _proxy_whep(self, raw_path, kind="whep"):
        """Forward a WHEP/WHIP SDP offer to the local mediamtx WebRTC endpoint and return its SDP
        answer. WHEP = we receive the robot's video; WHIP = we publish your microphone.
        Restricted to the WebRTC ports (8189-8192) so it can't be used as an open proxy."""
        rest = raw_path.split("/whipp/" if kind == "whip" else "/whepp/", 1)[1]
        port, _, sub = rest.partition("/")
        if not port.isdigit() or not (8189 <= int(port) <= 8192) or not sub:
            return self._send(400, b"", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            offer = self.rfile.read(n)
        except Exception:
            return self._send(400, b"", "text/plain")
        url = "http://127.0.0.1:%s/%s/%s" % (port, sub, kind)
        req = urllib.request.Request(url, data=offer, method="POST")
        req.add_header("Content-Type", "application/sdp")
        try:
            r = urllib.request.urlopen(req, timeout=15)
            status, answer, ctype = r.getcode(), r.read(), \
                r.headers.get("Content-Type", "application/sdp")
        except urllib.error.HTTPError as e:
            # mediamtx returns 4xx for a bad/rejected offer — relay its real status+body, not a 502,
            # so the browser can see the actual error instead of a generic proxy failure.
            status, answer = e.code, e.read()
            ctype = e.headers.get("Content-Type", "text/plain")
        except Exception as e:
            log("[%s] proxy failed: %s" % (kind, e))
            return self._send(502, b"", "text/plain")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(answer)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")   # harmless; same-origin in the panel
        self.end_headers()
        self.wfile.write(answer)

    def _mjpeg(self, node):
        """Stream a live MJPEG preview (multipart) from the robot's RTSP — smooth, no flicker."""
        with _lock:
            r = _robots.get(node)
            url = r and r.get("rtsp")
        if not url:
            return self._send(404, b"", "image/jpeg")
        p = urlparse(url)
        internal = "rtsp://127.0.0.1:%s%s" % (p.port or 8554, p.path)
        proc = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            proc = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-rtsp_transport", "tcp", "-i", internal,
                 "-r", "6", "-q:v", "7", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            buf = b""
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        break
                    jpg = buf[s:e + 2]
                    buf = buf[e + 2:]
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     b"Content-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
        except Exception:
            pass
        finally:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass

    def do_POST(self):
        if not self._authed():
            return self._send(403, json.dumps({"error": "forbidden"}))
        # WHEP (WebRTC signalling) proxy: the body is SDP, not JSON — handle before the JSON parse.
        # /whepp/<port>/<path> -> POST http://127.0.0.1:<port>/<path>/whep. Same-origin so the panel
        # page (behind Ingress, possibly https) never does a cross-origin/mixed-content POST.
        raw_path = urlparse(self.path).path
        if "/whepp/" in raw_path:
            return self._proxy_whep(raw_path)
        # WHIP: the same thing in the other direction — the browser PUBLISHES your microphone to
        # mediamtx, and the bridge picks that stream up and plays it through the robot's speaker.
        if "/whipp/" in raw_path:
            return self._proxy_whep(raw_path, kind="whip")
        path = urlparse(self.path).path.rstrip("/")
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad body"}))
        if path.endswith("/api/cmd"):
            node, suffix = str(body.get("node", "")), str(body.get("suffix", ""))
            # movement (move/vector, move/forward…) is allowed from the panel: you drive it while
            # watching the live view, so the "can't see the robot" reason to block it doesn't apply.
            ok_cmd = suffix in ALLOWED_CMDS or suffix.startswith("move/")
            if not node or not ok_cmd or _client is None:
                return self._send(400, json.dumps({"error": "bad command"}))
            _client.publish("%s/%s" % (node, suffix), str(body.get("payload", "")))
            log("[panel] cmd %s/%s = %s" % (node, suffix, body.get("payload", "")))
            return self._send(200, json.dumps({"ok": True}))
        if path.endswith("/api/pair/start"):
            try:
                return self._send(200, json.dumps(
                    _pair_start(str(body.get("ssid", "")), str(body.get("password", "")))))
            except Exception as e:
                log("[pair] start failed:", e)
                return self._send(500, json.dumps({"error": str(e)}))
        if path.endswith("/api/pair/status"):
            return self._send(200, json.dumps(_pair_status()))
        if path.endswith("/api/robot/remove"):
            try:
                return self._send(200, json.dumps(_remove_robot(str(body.get("node", "")))))
            except Exception as e:
                log("[remove] failed:", e)
                return self._send(500, json.dumps({"error": str(e)}))
        if path.endswith("/api/restart"):
            threading.Thread(target=_restart_self, daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))
        return self._send(404, "{}")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enabot</title><style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#111}
@media(prefers-color-scheme:dark){body{background:#111417;color:#e9ecef}}
header{padding:14px 18px;font-size:20px;font-weight:600;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;background:inherit;border-bottom:1px solid #0001}
.btn{border:0;border-radius:9px;padding:8px 12px;font-size:13px;cursor:pointer;background:#e6e8eb;color:inherit}
@media(prefers-color-scheme:dark){.btn{background:#2a3138;color:#e9ecef}}
.btn:hover{filter:brightness(.95)}.btn.pri{background:#2b6cff;color:#fff}.btn.danger{background:#c0392b;color:#fff}
.list{padding:10px 14px 24px;max-width:760px;margin:0 auto}
.rowitem{display:flex;gap:12px;align-items:center;background:#fff;border-radius:12px;padding:10px;margin-bottom:10px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.1)}
@media(prefers-color-scheme:dark){.rowitem{background:#1c2126}}
.rowitem:hover{filter:brightness(.98)}
.thumb{width:104px;height:60px;border-radius:8px;object-fit:cover;background:#000;flex:none}
.ri-name{font-weight:600;font-size:16px;display:flex;align-items:center;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;background:#c33;flex:none}.on{background:#2ea44f}
.ri-meta{color:#7a828a;font-size:13px;margin-top:3px}
.chev{margin-left:auto;color:#9aa2aa;font-size:22px;padding-right:6px}
.empty{padding:40px 18px;color:#8a929a;text-align:center}
/* detail */
.detail{max-width:760px;margin:0 auto;padding:0 14px 30px}
.big{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000;border-radius:12px;display:block;margin-top:12px}
.dname{font-size:22px;font-weight:700;margin:14px 0 2px;display:flex;align-items:center;gap:9px}
.dmeta{color:#7a828a;font-size:14px;margin-bottom:8px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}
.sec{background:#fff;border-radius:12px;padding:14px;margin-top:12px}
@media(prefers-color-scheme:dark){.sec{background:#1c2126}}
.sec h4{margin:0 0 8px;font-size:14px;color:#8a929a;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
label{font-size:12px;color:#8a929a;display:block;margin:10px 0 3px}
label.tgl{display:flex;align-items:center;justify-content:space-between;gap:12px;color:inherit;font-size:14px;margin:14px 0 3px}
label.tgl input[type=checkbox]{width:44px;height:26px;flex:none;-webkit-appearance:none;appearance:none;background:#c8ccd0;border-radius:13px;position:relative;cursor:pointer;transition:background .15s;margin:0}
label.tgl input[type=checkbox]:checked{background:#12b886}
label.tgl input[type=checkbox]::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .15s}
label.tgl input[type=checkbox]:checked::after{left:21px}
select,input[type=number]{width:100%;padding:8px;border-radius:8px;border:1px solid #0002;background:transparent;color:inherit}
input[type=range]{width:100%}
.url{font-size:11px;color:#8a929a;word-break:break-all;margin-top:10px}
/* driving D-pad */
.drive{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.dpad{display:grid;grid-template-columns:repeat(3,58px);grid-template-rows:repeat(3,58px);gap:6px;flex:none}
.db{border:0;border-radius:13px;background:#e6e8eb;color:inherit;font-size:20px;font-weight:700;cursor:pointer;touch-action:none;user-select:none;-webkit-user-select:none;display:flex;align-items:center;justify-content:center}
@media(prefers-color-scheme:dark){.db{background:#2a3138;color:#e9ecef}}
.db:active{background:#2b6cff;color:#fff}
.db.up{grid-area:1/2}.db.left{grid-area:2/1}.db.stop{grid-area:2/2;background:#c0392b;color:#fff}.db.right{grid-area:2/3}.db.down{grid-area:3/2}
.drive .sp{flex:1;min-width:150px}
/* analog joystick (drag the knob to drive; up=forward, sides=turn, diagonal=curve) */
.joy{width:170px;height:170px;border-radius:50%;position:relative;flex:none;touch-action:none;user-select:none;-webkit-user-select:none;
  background:radial-gradient(circle at 50% 42%,#3a4048,#1b2025);border:1px solid #ffffff14;box-shadow:inset 0 2px 10px #0007}
.joy::before{content:'';position:absolute;inset:20px;border-radius:50%;border:1px dashed #ffffff1f}
.joy-knob{position:absolute;left:50%;top:50%;width:66px;height:66px;margin:-33px 0 0 -33px;border-radius:50%;
  background:radial-gradient(circle at 40% 33%,#5a97ff,#2b6cff);box-shadow:0 3px 12px #0009;transition:transform .06s ease-out;pointer-events:none}
.joy.drag .joy-knob{transition:none}
/* fullscreen gamepad */
#fs{position:fixed;inset:0;background:#000;z-index:9999;display:none}
#fsvid{position:absolute;inset:0;width:100%;height:100%;border:0;background:#000;object-fit:contain}
.fs-tap{position:absolute;inset:0;z-index:1}   /* tap the video to show/hide controls */
/* fullscreen top bar: info (battery/signal/video) on the left, actions on the right (like the app) */
.fs-top{position:absolute;top:0;left:0;right:0;z-index:3;display:flex;justify-content:space-between;align-items:center;
  gap:10px;padding:10px 14px;color:#fff;font-size:13px;background:linear-gradient(#000a,#0000)}
.fs-info{display:flex;align-items:center;gap:14px;flex-wrap:wrap;min-width:0;overflow:hidden}
.fs-info .b{background:#0006;padding:3px 8px;border-radius:8px;backdrop-filter:blur(3px)}
.fs-info .b.rtc{background:rgba(18,184,134,.55);color:#eafff5}
.fs-info .b.hls{background:rgba(214,138,0,.6);color:#fff5e0}
/* tiny level meters so you can SEE that audio is flowing, both ways */
.vu{display:none;align-items:center;gap:5px;background:#0007;border-radius:9px;padding:3px 7px;
  backdrop-filter:blur(3px);vertical-align:middle;font-size:11px;line-height:1}
.vu.on{display:inline-flex}
.vu .bar{position:relative;width:64px;height:10px;background:#ffffff26;border-radius:5px;overflow:hidden}
.vu .bar i{position:absolute;left:0;top:0;bottom:0;width:0%;border-radius:5px;
  transition:width .06s linear}
.vu .pk{position:absolute;top:0;bottom:0;width:2px;background:#fff;opacity:.85;left:0;
  transition:left .12s linear}
.vu.spk .bar i{background:linear-gradient(90deg,#12b886,#2ee6a8);box-shadow:0 0 6px #2ee6a880}
.vu.mic .bar i{background:linear-gradient(90deg,#2b6cff,#59a7ff);box-shadow:0 0 6px #59a7ff80}
.fs-hlswarn{position:absolute;top:50px;left:50%;transform:translateX(-50%);z-index:3;max-width:calc(100% - 24px);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  background:rgba(176,90,0,.85);color:#fff;font-size:11px;padding:3px 12px;border-radius:12px;
  text-align:center;backdrop-filter:blur(3px);transition:opacity .5s;opacity:1}
.fs-hlswarn.fade{opacity:0}
.connhint{font-size:12px;margin-top:5px;color:#8a929a}
.connhint.hls{color:#c77d00}
.fs-actions{display:flex;align-items:center;gap:8px;flex:none}
.fs-ic{width:46px;height:46px;border-radius:50%;background:#0007;color:#fff;border:0;font-size:19px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;backdrop-filter:blur(3px);flex:none}
/* A phone held sideways has very little height: shrink the bar so the buttons stop crowding the
   picture (and each other), and keep everything on one line. */
@media (max-height: 500px){
  .fs-top{padding:6px 10px;gap:6px;font-size:12px}
  .fs-ic{width:36px;height:36px;font-size:16px}
  .fs-ic[style]{width:32px!important;height:32px!important;font-size:19px!important}
  .fs-info{gap:8px}
  .fs-info .b{padding:2px 6px;font-size:11px}
  .vu{padding:2px 5px}
  .vu .bar{width:44px;height:8px}
}
@media (max-width: 430px){       /* narrow phones, portrait */
  .fs-ic{width:38px;height:38px;font-size:17px}
  .fs-actions{gap:5px}
}
.fs-ic:active{transform:scale(.92)}.fs-ic.on{background:#2b6cff}
.fs-ic.disabled{opacity:.35;pointer-events:none}
.fs-ic.fail{background:#c0392b;animation:failshake .4s}
@keyframes failshake{25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
/* driving controls container (dual sticks or a single joystick, chosen in fullscreen Settings) */
#fs-drive{position:absolute;inset:0;z-index:2;pointer-events:none}
#fs-drive .stick,#fs-drive .joy{pointer-events:auto}
/* one-axis sticks are slim tracks (no need for a full circle): vertical = tall+narrow, horiz = wide+short */
.stick{position:absolute;bottom:30px;touch-action:none;user-select:none;-webkit-user-select:none;
  background:linear-gradient(#ffffff1e,#00000055);border:1px solid #ffffff1f;backdrop-filter:blur(3px)}
.stick[data-axis=v]{width:88px;height:210px;border-radius:44px}
.stick[data-axis=h]{width:210px;height:88px;border-radius:44px}
.fs-lstick{left:28px}.fs-rstick{right:28px}
.fs-single{position:absolute;bottom:26px;width:200px;height:200px;border-radius:50%;
  background:radial-gradient(circle at 50% 42%,#ffffff26,#00000055);backdrop-filter:blur(3px)}
.fs-single.left{left:28px}.fs-single.center{left:50%;transform:translateX(-50%)}.fs-single.right{right:28px}
.fs-single .joy-knob{width:76px;height:76px;margin:-38px 0 0 -38px}
.stick .joy-knob{position:absolute;left:50%;top:50%;width:64px;height:64px;margin:-32px 0 0 -32px;border-radius:50%;
  background:radial-gradient(circle at 40% 33%,#5a97ff,#2b6cff);box-shadow:0 3px 12px #0009;pointer-events:none;transition:transform .06s ease-out}
.stick.drag .joy-knob{transition:none}
.stick .ax{position:absolute;color:#ffffff88;font-size:13px}
.stick[data-axis=v] .ax.a1{top:7px;left:50%;transform:translateX(-50%)}
.stick[data-axis=v] .ax.a2{bottom:7px;left:50%;transform:translateX(-50%)}
.stick[data-axis=h] .ax.a1{left:7px;top:50%;transform:translateY(-50%)}
.stick[data-axis=h] .ax.a2{right:7px;top:50%;transform:translateY(-50%)}
#fs.hidectl .fs-top,#fs.hidectl #fs-drive{display:none}
/* press feedback */
.btn:active{transform:scale(.96);filter:brightness(1.3)}
.db.on,.db:active{background:#2b6cff !important;color:#fff !important;transform:scale(.9)}
/* list action icons + detail camera hint + charging warning */
.ic{border:0;background:transparent;font-size:20px;cursor:pointer;padding:6px 8px;border-radius:9px;line-height:1;flex:none}
.ic:hover{background:#0001}
@media(prefers-color-scheme:dark){.ic:hover{background:#ffffff14}}
.bigwrap{position:relative;cursor:pointer}
.fshint{position:absolute;right:10px;bottom:20px;background:#0008;color:#fff;font-size:11px;padding:3px 8px;border-radius:8px;pointer-events:none}
/* sleeping (ZZ): keep showing the last frame, dimmed, with a big wake button over it */
.bigwrap.asleep .big{filter:grayscale(.6) brightness(.45)}
.wakebtn{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2;display:flex;
  flex-direction:column;align-items:center;gap:6px;border:0;cursor:pointer;color:#fff;
  background:rgba(20,24,28,.72);backdrop-filter:blur(4px);padding:16px 22px;border-radius:16px}
.wakebtn .ic{font-size:34px;line-height:1}
.wakebtn .tx{font-size:12px;opacity:.9}
.wakebtn:active{transform:translate(-50%,-50%) scale(.95)}
.wakebtn.busy{opacity:.6;pointer-events:none}
.sleepbtn{position:absolute;left:10px;bottom:20px;z-index:2;border:0;cursor:pointer;color:#fff;
  background:rgba(20,24,28,.72);backdrop-filter:blur(4px);font-size:12px;padding:5px 10px;border-radius:10px}
.sleepbtn:active{transform:scale(.95)}
.sleepbtn.busy{opacity:.6;pointer-events:none}
/* battery + wifi as little bar gauges — a raw "-64" means nothing to most people */
.ind{display:inline-flex;align-items:center;gap:4px;vertical-align:-2px;margin-right:5px;white-space:nowrap}
.bat{position:relative;width:26px;height:13px;border:1.5px solid currentColor;border-radius:3px;
  display:inline-flex;gap:1px;padding:1.5px;box-sizing:border-box}
.bat{margin-right:3px}
.bat::after{content:"";position:absolute;right:-4px;top:3.5px;width:2.5px;height:5px;
  background:currentColor;border-radius:0 2px 2px 0}
.bat i{flex:1;background:currentColor;opacity:.2;border-radius:1px}
.bat i.on{opacity:1}
.bat.ok{color:#2ea36a}.bat.warn{color:#d68a00}.bat.low{color:#c0392b}.bat.none{color:#8a929a}
.bat .bolt{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-size:10px;line-height:1;color:#fff;text-shadow:0 0 3px #000,0 0 2px #000}
.sig{display:inline-flex;align-items:flex-end;gap:2px;height:13px}
.sig i{width:3px;background:currentColor;opacity:.2;border-radius:1px}
.sig i.on{opacity:1}
.sig i:nth-child(1){height:4px}.sig i:nth-child(2){height:7px}
.sig i:nth-child(3){height:10px}.sig i:nth-child(4){height:13px}
.sig.good{color:#2ea36a}.sig.fair{color:#d68a00}.sig.weak{color:#c0392b}.sig.none{color:#8a929a}
.thumbwrap{position:relative}
#toast{position:fixed;left:50%;bottom:18px;transform:translate(-50%,20px);z-index:60;opacity:0;
  background:rgba(20,24,28,.92);color:#fff;font-size:13px;padding:9px 14px;border-radius:12px;
  pointer-events:none;transition:opacity .25s,transform .25s;max-width:90%;text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}
.zzbadge{position:absolute;top:6px;right:6px;background:#0009;color:#cfe;font-size:11px;
  padding:2px 6px;border-radius:8px;pointer-events:none}
.warn{background:#e67e22;color:#fff;border-radius:10px;padding:9px 12px;font-size:13px;margin:10px 0;font-weight:600}
dialog{border:0;border-radius:14px;padding:0;max-width:440px;width:92%;background:#fff;color:#111}
@media(prefers-color-scheme:dark){dialog{background:#1c2126;color:#e9ecef}}
dialog .in{padding:18px}h3{margin:0 0 10px}.note{font-size:12px;color:#8a929a;margin-top:10px}
.tabs{display:flex;gap:6px;margin:0 0 4px;border-bottom:1px solid #0001;padding-bottom:2px}
.tab{flex:1;border:0;background:transparent;color:#8a929a;font-size:13px;font-weight:600;padding:8px 4px;cursor:pointer;border-bottom:2px solid transparent}
.tab.on{color:inherit;border-bottom-color:#2b6cff}
/* routes (teach & repeat) */
.routes{display:flex;flex-direction:column;gap:8px}
.rrow{display:flex;align-items:center;gap:8px}
.rrow .rn{flex:1;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rrow .btn{padding:6px 12px}
/* fullscreen record button: red + pulsing while recording */
.fs-ic.rec{background:#c0392b;color:#fff;animation:recpulse 1.2s ease-in-out infinite}
@keyframes recpulse{0%,100%{opacity:1}50%{opacity:.45}}
</style></head><body>
<header>
  <span><span id="title" onclick="goBack()" style="cursor:pointer">🤖 EBO</span>
        <span id="acct" style="font-size:12px;color:#8a929a;font-weight:400"></span></span>
  <span><button class="btn" id="addbtn" onclick="openAdd()">+ Add robot</button></span>
</header>
<div id="view"></div>

<div id="fs" tabindex="0">
  <video id="fsvid" autoplay muted playsinline></video>
  <div class="fs-tap" onclick="toggleFsControls()"></div>
  <div class="fs-top" id="fs-top"></div>
  <div id="fs-hlswarn" class="fs-hlswarn" style="display:none">⚠ HLS — video delayed ~1&nbsp;s, not for reactive driving</div>
  <div id="fs-drive"></div>
</div>

<dialog id="fsopts"><div class="in">
  <h3>Drive settings</h3>
  <div class="tabs">
    <button class="tab on" data-tab="drv" onclick="fsTab('drv')">Driving</button>
    <button class="tab" data-tab="cam" onclick="fsTab('cam')">Camera</button>
    <button class="tab" data-tab="aud" onclick="fsTab('aud')">Audio</button>
    <button class="tab" data-tab="ctl" onclick="fsTab('ctl')">Controls</button>
  </div>
  <div class="tabp" data-tab="drv">
    <label>Driving mode</label>
    <select id="fs-dm" onchange="if(fsNode)cmd(fsNode,'move_mode/set',this.value)">${''}</select>
    <label>Movement speed (<span id="fs-mspd-v">—</span>)</label>
    <input id="fs-mspd" type="range" min="1" max="100" value="50" onchange="if(fsNode)cmd(fsNode,'speed/set',this.value)" oninput="document.getElementById('fs-mspd-v').textContent=this.value">
    <label class="tgl"><span>Collision avoidance</span>
      <input type="checkbox" id="fs-avoid" onchange="if(fsNode)cmd(fsNode,'avoid_obstacle/set',this.checked?'on':'off')"></label>
  </div>
  <div class="tabp" data-tab="cam" style="display:none">
    <label>Night vision</label>
    <select id="fs-nv" onchange="if(fsNode)cmd(fsNode,'night_vision/set',this.value)">${''}</select>
    <label>Video quality</label><select id="fs-vq" onchange="if(fsNode){_driveVQ=null;cmd(fsNode,'video_quality/set',this.value);}">${''}</select>
  </div>
  <div class="tabp" data-tab="aud" style="display:none">
    <label>Speaker volume — the robot's own voice &amp; sounds (<span id="fs-svol-v">—</span>)</label>
    <input id="fs-svol" type="range" min="0" max="100" value="50" onchange="if(fsNode)cmd(fsNode,'volume/set',this.value)" oninput="document.getElementById('fs-svol-v').textContent=this.value">
    <label>Call volume — your voice through the robot (<span id="fs-cvol-v">—</span>)</label>
    <input id="fs-cvol" type="range" min="0" max="100" value="50" onchange="if(fsNode)cmd(fsNode,'talkback_volume/set',this.value)" oninput="document.getElementById('fs-cvol-v').textContent=this.value">
  </div>
  <div class="tabp" data-tab="ctl" style="display:none">
    <label>Controls</label>
    <select id="fs-ctrl" onchange="setFsCtrl(this.value)">
      <option value="dual">Two sticks (drive + steer)</option>
      <option value="joy">Single joystick</option>
    </select>
    <label id="fs-swaprow" style="display:flex;align-items:center;gap:8px;margin-top:10px">
      <input type="checkbox" id="fs-swap" style="width:auto" onchange="setFsSwap(this.checked)"> Swap the two sticks (steer on the left, drive on the right)
    </label>
    <label id="fs-joyrow" style="display:none">Joystick side
      <select id="fs-joyside" onchange="setFsJoySide(this.value)">
        <option value="left">Left</option><option value="center">Center</option><option value="right">Right</option>
      </select>
    </label>
    <label>Joystick sensitivity (<span id="fs-spd-v">60</span>)</label>
    <input id="fs-spd" type="range" min="1" max="100" value="60" oninput="driveSpeed=+this.value;document.getElementById('fs-spd-v').textContent=this.value">
  </div>
  <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn pri" onclick="document.getElementById('fsopts').close()">Done</button></div>
  <div class="note">More actions (talk, listen, snapshot) coming soon.</div>
</div></dialog>

<dialog id="routesave"><div class="in">
  <h3>Save route</h3>
  <label>Route name</label>
  <input id="rs-name" type="text" placeholder="e.g. Living-room loop">
  <div class="row" style="justify-content:flex-end;margin-top:16px;gap:8px">
    <button class="btn" onclick="document.getElementById('routesave').close()">Discard</button>
    <button class="btn pri" onclick="saveRoute()">Save</button>
  </div>
  <div class="note">Saves the path you just drove, so you can repeat it later from the robot's Routes list.</div>
</div></dialog>

<dialog id="add"><div class="in">
  <h3>Add a robot</h3>
  <div id="addform">
    <label>Wi-Fi network (2.4 GHz) the robot should join</label>
    <input id="a-ssid" type="text" placeholder="SSID">
    <label>Wi-Fi password</label>
    <input id="a-pass" type="text" placeholder="password">
    <div class="row" style="justify-content:flex-end;margin-top:16px">
      <button class="btn" onclick="document.getElementById('add').close()">Cancel</button>
      <button class="btn pri" onclick="pairStart()">Generate QR</button>
    </div>
    <div class="note">The robot joins this Wi-Fi by scanning a QR — no phone needed.<br>
      This is for cloud models (Air 2, X, Max…). An <b>EBO SE</b> uses local LAN — use the
      <b>ebo-se-lan-bridge</b> project instead.</div>
  </div>
  <div id="addqr" style="display:none;text-align:center">
    <p>Turn the robot on, then <b>hold its camera up to this QR code</b>:</p>
    <img id="qrimg" style="width:260px;height:260px;image-rendering:pixelated;background:#fff;border-radius:10px">
    <p id="pairmsg" class="note">Waiting for the robot to scan…</p>
    <div class="row" style="justify-content:flex-end">
      <button class="btn" onclick="stopPair()">Close</button>
    </div>
  </div>
</div></dialog>

<script>
const B = window.location.pathname.replace(/\/$/,'');
(function(){ const s=document.createElement('script'); s.src=B+'/hls.min.js'; s.async=true; document.head.appendChild(s); })();  // fluid HLS player
const VQ=["Low","Medium","High"], IS=["Standard","Vivid","Soft"],
      EY=["Dynamic 1","Dynamic 2","Dynamic 3","Dynamic 4","Dynamic 5","Dynamic 6","Clock 1","Clock 2","Custom"],
      DM=["Smooth","Racing"],   // driving mode (app: Driving Mode Smooth/Racing)
      NV=["Auto","Day","Night"], NV_ICON={Auto:'🌗',Day:'☀️',Night:'🌙'};  // day/night vision (app btnDayNight)
let ROBOTS=[], SEL=null;
async function cmd(node,suffix,payload){
  await fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({node,suffix,payload})}); setTimeout(refresh,400);
}
function esc(s){return (s==null?'':(''+s))}
function opt(list,cur){return list.map(v=>`<option ${v==cur?'selected':''}>${v}</option>`).join('')}
// Battery as a 4-segment gauge (green/amber/red), with a bolt while charging.
function batHtml(pct, charging){
  const p = (pct==null||pct==='') ? null : Math.max(0, Math.min(100, +pct));
  const n = (p==null) ? 0 : Math.max(1, Math.ceil(p/25));
  const cls = (p==null) ? 'none' : (p<=20 ? 'low' : (p<=50 ? 'warn' : 'ok'));
  let bars=''; for(let i=1;i<=4;i++) bars += '<i class="'+(i<=n?'on':'')+'"></i>';
  const on = (charging===true || charging==='true');
  return '<span class="ind" title="Battery '+(p==null?'unknown':p+'%')+(on?' · charging':'')+'">'
       + '<span class="bat '+cls+'">'+bars+(on?'<span class="bolt">⚡</span>':'')+'</span>'
       + '<span>'+(p==null?'—':p+'%')+'</span></span>';
}
// Wi-Fi as 4 bars. The robot reports dBm (e.g. -64); some report 0-100 instead — handle both.
function sigHtml(v){
  const raw = (v==null||v==='') ? null : +v;
  let n=0, label='unknown';
  if(raw!=null && !isNaN(raw)){
    if(raw>0){ n = Math.max(1, Math.ceil(raw/25)); }                       // percentage
    else { n = raw>=-55?4 : raw>=-65?3 : raw>=-75?2 : 1; }                 // dBm
    label = ['weak','fair','good','excellent'][n-1] || 'unknown';
  }
  const cls = n===0?'none' : (n>=3?'good' : (n===2?'fair':'weak'));
  let bars=''; for(let i=1;i<=4;i++) bars += '<i class="'+(i<=n?'on':'')+'"></i>';
  return '<span class="ind" title="Wi-Fi: '+label+(raw!=null?' ('+raw+(raw>0?'%':' dBm')+')':'')+'">'
       + '<span class="sig '+cls+'">'+bars+'</span></span>';
}
function meta(r){const st=r.state||{};
  const wifi=(st.wifi!=null?st.wifi:(st.rssi!=null?st.rssi:null));
  return `${r.model||'EBO'} · ${batHtml(st.battery, st.charging)} · ${sigHtml(wifi)}`;}
function thumb(n){return `${B}/api/snapshot?node=${encodeURIComponent(n)}&t=${Math.floor(Date.now()/4000)}`}
function bg(node,suffix,payload){ fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({node,suffix,payload})}).catch(()=>{}); }
// Laser is a TOGGLE: the robot reports its state (state.laser), so read it at click time and send
// the opposite. Sending 'on' unconditionally (the old behaviour) could never turn it off.
// The robot reports laserStatus with a few seconds' lag, and refresh() overwrites ROBOTS wholesale —
// so an optimistic toggle would flicker back when a stale poll lands. We hold the optimistic value
// until the real state catches up (or a short timeout), which kills the flicker.
let laserPending={};
function laserOn(node){
  const r=ROBOTS.find(x=>x.node===node);
  const real=!!(r&&r.state&&(r.state.laser==='true'||r.state.laser===true));
  const p=laserPending[node];
  if(p){ if(real===p.val || Date.now()>=p.until){ delete laserPending[node]; return real; } return p.val; }
  return real;
}
function updateLaserUI(node){
  const on=laserOn(node);
  const db=document.getElementById('d-laser'); if(db){ db.className='btn '+(on?'pri':''); db.textContent='Laser '+(on?'ON':'OFF'); }
  const fb=document.getElementById('fs-laser'); if(fb) fb.className='fs-ic '+(on?'on':'');
}
function toggleLaser(node){
  const nv=!laserOn(node);
  laserPending[node]={val:nv, until:Date.now()+6000};   // optimistic feedback, held until telemetry agrees
  updateLaserUI(node);
  cmd(node,'laser/set', nv?'on':'off');
}
// Day/night vision (app's fullscreen day/night button = shootMode 0 Auto / 1 Day / 2 Night).
let nightPending={};
function nightMode(node){
  const r=ROBOTS.find(x=>x.node===node)||{}, st=r.state||{};
  const real=NV.includes(st.night_vision)?st.night_vision:'Auto';
  const p=nightPending[node];
  if(p){ if(real===p.val || Date.now()>=p.until){ delete nightPending[node]; return real; } return p.val; }
  return real;
}
function updateNightUI(node){
  const m=nightMode(node);
  const fb=document.getElementById('fs-night');
  if(fb){ fb.textContent=NV_ICON[m]||'🌗'; fb.title='Day/Night vision: '+m+' (tap to change)'; fb.className='fs-ic'+(m==='Night'?' on':''); }
}
function cycleNight(node){
  const next=NV[(NV.indexOf(nightMode(node))+1)%NV.length];
  nightPending[node]={val:next, until:Date.now()+8000};   // optimistic; released when telemetry agrees
  updateNightUI(node);
  cmd(node,'night_vision/set', next);
}
// --- Routes: teach-and-repeat. The list + replay/delete live in the detail; recording (drive to teach
// a path) starts from the fullscreen ⏺ button. ---
function routesHtml(r){
  const st=r.state||{}, routes=st.routes||[];
  if(!routes.length) return '<div class="note" style="color:#8a929a">No saved routes yet.</div>';
  return '<div class="routes">'+routes.map(rt=>{
    const nm=esc(rt.name).replace(/'/g,"\\'");
    return `<div class="rrow"><span class="rn">${esc(rt.name)}</span>
      <button class="btn pri" onclick="replayRoute('${r.node}','${nm}')">▶ Repeat</button>
      <button class="btn" onclick="delRoute('${r.node}',${rt.id})" title="Delete route">🗑</button></div>`;
  }).join('')+'</div>';
}
// Wake the robot straight from the detail view (no need to enter fullscreen just to wake it).
// camera/set on is the reliable wake: it re-joins the robot's session with a fresh cloud session.
async function wakeRobot(node, btn){
  if(btn){ btn.classList.add('busy'); const t=btn.querySelector('.tx'); if(t) t.textContent='Waking…'; }
  toast('Waking the robot — this takes a few seconds');
  await cmd(node,'camera/set','on');
  setTimeout(refresh, 2500);      // the robot needs a moment to come back and start streaming
}
// Hearing the robot needs BOTH halves: the robot must publish its mic (opcode 102001 — subscribing
// alone gets you a silent track) AND the <video> must be unmuted. Browsers force muted autoplay, so
// the player always starts silent; only a real tap may unmute it. This one button does both.
function hearingRobot(){ const v=document.getElementById('fsvid'); return !!(v && !v.muted); }
function updateListenUI(){
  const b=document.getElementById('fs-listen'); if(!b) return;
  const on=hearingRobot();
  b.textContent = on?'🔊':'🔇';
  b.className = 'fs-ic'+(on?' on':'');
  b.title = on ? 'Listening to the robot — tap to mute' : 'Listen to the robot';
}
async function toggleListen(node){
  const want = !hearingRobot();
  const v=document.getElementById('fsvid');
  if(v){ v.muted = !want; if(want){ v.volume = 1; v.play().catch(()=>{}); } }
  if(want) startSpeakerMeter(); else stopSpeakerMeter();
  updateListenUI();
  toast(want ? 'Listening to the robot' : 'Muted');
  await cmd(node,'listen/set', want?'on':'off');
}

// ---- Talk (your phone/PC microphone -> the robot's speaker) --------------------------------
// The browser PUBLISHES the mic to mediamtx over WebRTC (WHIP) on a "talk" path; the bridge then
// reads that live stream and pushes it into the robot's Agora channel. Same plumbing as the video,
// just in the opposite direction.
let _talkPc=null, _talkStream=null;
function _rtspPortOf(node){
  const r=ROBOTS.find(x=>x.node===node);
  try{ return parseInt(new URL((r.rtsp||'').replace(/^rtsp:/,'http:')).port||'8554',10); }
  catch(e){ return 8554; }
}
function talking(){ return !!_talkPc; }
function _talkFail(){            // flash the button so a failure is never silent
  const b=document.getElementById('fs-talk'); if(!b) return;
  b.classList.add('fail'); setTimeout(()=>b.classList.remove('fail'), 1600);
}
function updateTalkUI(){
  const b=document.getElementById('fs-talk'); if(!b) return;
  const on=talking();
  b.className='fs-ic'+(on?' on':'');
  b.title=on?'Talking — tap to stop':'Talk to the robot (your microphone)';
  const m=document.getElementById('vu-mic'); if(m) m.className='vu mic'+(on?' on':'');
}
async function toggleTalk(node){
  if(talking()){ return stopTalk(node); }
  // Browsers only expose the microphone in a SECURE context: https:// (or localhost). Opening Home
  // Assistant over plain http://<ip>:8123 makes navigator.mediaDevices simply not exist — no prompt,
  // no permission to grant. Say so plainly instead of a vague "blocked".
  if(!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    toast('Talk needs HTTPS — open Home Assistant over https:// (on plain http the browser hides the microphone entirely)', 7000);
    _talkFail();
    return;
  }
  let stream;
  try{
    stream = await navigator.mediaDevices.getUserMedia(
      {audio:{echoCancellation:true, noiseSuppression:true, autoGainControl:true}});
  }catch(e){
    const n=(e&&e.name)||'';
    toast(n==='NotAllowedError' ? 'Microphone permission denied — allow it for this site'
        : n==='NotFoundError'  ? 'No microphone found on this device'
        : 'Microphone unavailable: '+(e&&e.message||n), 6000);
    _talkFail();
    return;
  }
  _talkStream = stream;
  const pc = new RTCPeerConnection({iceServers:[]});
  _talkPc = pc;
  stream.getAudioTracks().forEach(t=>pc.addTrack(t, stream));
  const offer = await pc.createOffer(); await pc.setLocalDescription(offer);
  await new Promise(res=>{ if(pc.iceGatheringState==='complete') return res();
    const t=setTimeout(res,1200);
    pc.addEventListener('icegatheringstatechange',()=>{ if(pc.iceGatheringState==='complete'){clearTimeout(t);res();} }); });
  const rp=_rtspPortOf(node), wp=8189+(rp-8554);
  let ok=false;
  try{
    const r=await fetch(B+'/whipp/'+wp+'/talk',{method:'POST',
      headers:{'Content-Type':'application/sdp'}, body:pc.localDescription.sdp});
    if(r.ok){ await pc.setRemoteDescription({type:'answer', sdp:await r.text()}); ok=true; }
    else toast('Talk failed ('+r.status+')');
  }catch(e){ toast('Talk failed: '+e.message); }
  if(!ok){ return stopTalk(node); }
  startMicMeter(stream);
  updateTalkUI();
  toast('Talking — the robot is playing your voice');
  // give mediamtx a moment to accept the publisher, then have the robot play it
  setTimeout(()=>cmd(node,'talk','rtsp://127.0.0.1:'+rp+'/talk'), 700);
}
async function stopTalk(node){
  try{ await cmd(node,'talk/stop',''); }catch(e){}
  if(_talkPc){ try{_talkPc.close();}catch(e){} _talkPc=null; }
  if(_talkStream){ try{_talkStream.getTracks().forEach(t=>t.stop());}catch(e){} _talkStream=null; }
  stopMicMeter();
  updateTalkUI();
}

// ---- Level meters: show that audio is actually flowing, both ways --------------------------
let _ac=null, _micAn=null, _spkAn=null, _vuRaf=null, _spkSrcEl=null;
function _audioCtx(){
  if(!_ac){ const C=window.AudioContext||window.webkitAudioContext; if(!C) return null; _ac=new C(); }
  if(_ac.state==='suspended'){ _ac.resume().catch(()=>{}); }
  return _ac;
}
function _analyser(node){
  const ac=_audioCtx(); if(!ac) return null;
  const an=ac.createAnalyser(); an.fftSize=256; an.smoothingTimeConstant=0.6;
  node.connect(an); return an;
}
function _level(an){
  if(!an) return 0;
  const buf=new Uint8Array(an.fftSize); an.getByteTimeDomainData(buf);
  let sum=0; for(let i=0;i<buf.length;i++){ const d=(buf[i]-128)/128; sum+=d*d; }
  const rms=Math.sqrt(sum/buf.length);
  // Perceptual: quiet speech is a very small RMS, so take a root and add gain — otherwise the bar
  // barely twitches even when the audio is perfectly audible.
  return Math.max(0, Math.min(1, Math.pow(rms, 0.45) * 2.2));
}
const _pk={};      // per-meter peak hold, decays slowly so short peaks stay visible
function _vuTick(){
  const set=(id,v)=>{
    const e=document.getElementById(id); if(!e) return;
    const b=e.querySelector('.bar i'), p=e.querySelector('.pk');
    if(b) b.style.width=Math.round(v*100)+'%';
    const prev=_pk[id]||0;
    const peak=Math.max(v, prev-0.012);          // hold, then fall back gently
    _pk[id]=peak;
    if(p) p.style.left=Math.max(0, Math.round(peak*100)-2)+'%';
  };
  set('vu-mic', _level(_micAn));
  set('vu-spk', _level(_spkAn));
  _vuRaf=requestAnimationFrame(_vuTick);
}
function _vuStart(){ if(!_vuRaf) _vuTick(); }
function _vuStopIfIdle(){ if(!_micAn && !_spkAn && _vuRaf){ cancelAnimationFrame(_vuRaf); _vuRaf=null; } }
function startMicMeter(stream){
  const ac=_audioCtx(); if(!ac) return;
  try{ _micAn=_analyser(ac.createMediaStreamSource(stream)); _vuStart(); }catch(e){}
}
function stopMicMeter(){ _micAn=null; _pk['vu-mic']=0; const m=document.getElementById('vu-mic');
  if(m){ m.className='vu mic'; const b=m.querySelector('.bar i'); if(b) b.style.width='0%'; } _vuStopIfIdle(); }
function startSpeakerMeter(){
  const v=document.getElementById('fsvid'); const ac=_audioCtx(); if(!v||!ac) return;
  try{
    let src;
    if(v.srcObject && v.srcObject.getAudioTracks && v.srcObject.getAudioTracks().length){
      src=ac.createMediaStreamSource(v.srcObject);        // WebRTC path
    }else{
      if(_spkSrcEl!==v){ _spkSrcEl=v; _spkSrcEl._node=ac.createMediaElementSource(v);
        _spkSrcEl._node.connect(ac.destination); }        // HLS path: keep it audible
      src=_spkSrcEl._node;
    }
    _spkAn=_analyser(src);
    const e=document.getElementById('vu-spk'); if(e) e.className='vu spk on';
    _vuStart();
  }catch(e){}
}
function stopSpeakerMeter(){ _spkAn=null; _pk['vu-spk']=0; const e=document.getElementById('vu-spk');
  if(e){ e.className='vu spk'; const b=e.querySelector('.bar i'); if(b) b.style.width='0%'; } _vuStopIfIdle(); }
// Put the robot to sleep on demand (ZZ): leaving the session is exactly what makes it doze off,
// same as closing the official app.
async function sleepRobot(node, btn){
  // We can only STOP WATCHING (leave the robot's session) — the robot itself then decides to doze
  // off, which takes a few seconds to a couple of minutes, exactly like closing the official app.
  // So give immediate feedback (dim the picture, say what's happening) instead of looking broken.
  const wrap=document.querySelector('.bigwrap');
  if(wrap) wrap.classList.add('asleep');
  if(btn){ btn.classList.add('busy'); btn.textContent='😴 Going to sleep…'; }
  toast('Sleep requested — the robot closes its eyes in a moment');
  await cmd(node,'connected/set','off');
  setTimeout(refresh, 2500);
}
// small transient message at the bottom of the panel
function toast(msg, ms){
  // In native fullscreen the browser paints ONLY the fullscreen element's subtree, so a toast on
  // <body> is invisible — which is exactly why the mic button looked like it "did nothing".
  const fs = document.getElementById('fs');
  const inFs = document.fullscreenElement || (fs && fs.style.display === 'block');
  const host = document.fullscreenElement || (inFs ? fs : document.body);
  let t=document.getElementById('toast');
  if(!t){ t=document.createElement('div'); t.id='toast'; }
  if(t.parentNode!==host){ host.appendChild(t); }
  t.textContent=msg; t.className='show';
  clearTimeout(t._h); t._h=setTimeout(()=>{ t.className=''; }, ms||4500);
}
function replayRoute(node,name){ cmd(node,'patrol/route/set',name); setTimeout(()=>cmd(node,'patrol/start',''),350); }
function delRoute(node,id){ if(confirm('Delete this route?')){ cmd(node,'route/delete',''+id); } }
// recording state (optimistic, like the laser)
let _recOptim={};
function recState(node){ const r=ROBOTS.find(x=>x.node===node)||{}, st=r.state||{}, real=(st.route_recording==='true');
  const o=_recOptim[node];
  if(o){ if(real===o.val || Date.now()>o.until){ delete _recOptim[node]; return real; } return o.val; }
  return real; }
function updateRecUI(node){ const on=recState(node); const b=document.getElementById('fs-rec');
  if(b){ b.className='fs-ic'+(on?' rec':''); b.title=on?'Stop recording — then name & save':'Record a route (drive to teach a path)'; } }
function recordRoute(node){
  if(recState(node)){
    _recOptim[node]={val:false,until:Date.now()+8000}; updateRecUI(node);
    cmd(node,'route/record/stop','');
    setTimeout(()=>openRouteSave(node), 1500);   // give the robot a moment to hand back the path
  } else {
    _recOptim[node]={val:true,until:Date.now()+8000}; updateRecUI(node);
    cmd(node,'route/record/start','');
  }
}
let _rsNode=null;
function openRouteSave(node){ _rsNode=node; const i=document.getElementById('rs-name'); if(i) i.value='';
  const d=document.getElementById('routesave'); if(d){ d.showModal(); if(i) i.focus(); } }
function saveRoute(){ const i=document.getElementById('rs-name'); const name=(i&&i.value.trim())||'';
  if(_rsNode) cmd(_rsNode,'route/save', name);
  const d=document.getElementById('routesave'); if(d) d.close(); }
// Enter detail/drive → camera/set on. Bridge-side this JOINS the Agora RTC channel, which WAKES
// the robot exactly like opening the app (real viewer present). goBack → connected/set off leaves
// the channel so the robot goes back to standby (ZZ). No unreliable isSleeping opcode dance.
// Just LOOKING at a robot must not wake it — otherwise it can never stay asleep while you check on
// it, and the "tap to wake" button would never appear. You wake it deliberately (that button, the
// Wake button, or by entering the drive view).
function openRobot(n){ SEL=n; render(true); }
function goBack(){ const p=SEL; SEL=null; render(true); if(p) bg(p,'connected/set','off'); }  // leave = standby
function driveNow(n){ SEL=n; render(true); bg(n,'camera/set','on'); setTimeout(()=>enterFS(n),60); }

// --- driving: hold direction(s) to move, release to stop. MULTIPLE directions COMBINE into one
// analog vector (move/vector carries ly=forward/back AND rx=turn together), so forward+right drives
// a smooth diagonal instead of only the last key winning. A watchdog re-sends while held. ---
let driveSpeed=60, moveNode=null, moveTimer=null;
const pressed=new Set();          // currently-held directions (keyboard and/or D-pad)
function sendVec(node,ly,rx,hold,buttons){
  fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({node,suffix:'move/vector',payload:JSON.stringify({ly,rx,hold,buttons:buttons||0})})}).catch(()=>{});
}
// Control scheme flag, exactly like the official app: buttons=1 = dual-stick (independent throttle +
// steering → the robot keeps TURNING while held); buttons=0 = single joystick (the vector is a heading,
// so a turn is a one-shot ~heading change). Sending 0 for a dual-stick turn is what made the robot jerk
// ~90° then go straight. The keyboard/D-pad follows the chosen control type (fsCtrlMode).
function dualFlag(){ return fsCtrlMode==='dual' ? 1 : 0; }
function _driveTick(){
  if(!moveNode) return;
  let ly=0, rx=0;
  if(pressed.has('fwd')) ly-=1;
  if(pressed.has('back')) ly+=1;
  if(pressed.has('left')) rx-=1;
  if(pressed.has('right')) rx+=1;
  if(ly===0 && rx===0){ sendVec(moveNode,0,0,0); return; }
  // Normalize the diagonal so a two-axis press doesn't OVERDRIVE one wheel (ly+rx would sum to
  // ~2×speed, making the robot pivot then shoot off at double speed). With normalization,
  // forward+right becomes a smooth forward ARC (each axis ~0.7×speed) instead of a spin.
  const mag=Math.hypot(ly,rx);
  if(mag>1){ ly/=mag; rx/=mag; }
  sendVec(moveNode, Math.round(ly*driveSpeed), Math.round(rx*driveSpeed), 0.7, dualFlag());
}
function startMove(node,dir){       // press a direction — add it to the combined vector
  moveNode=node;
  if(!pressed.has(dir)){ pressed.add(dir); _driveTick(); }   // (also ignores keyboard auto-repeat)
  if(!moveTimer) moveTimer=setInterval(_driveTick,300);      // keep-alive while any key is held
}
function stopMove(dir){             // release one direction; no arg = release ALL (cleanup)
  if(dir===undefined) pressed.clear(); else pressed.delete(dir);
  if(pressed.size===0){
    if(moveTimer){clearInterval(moveTimer);moveTimer=null;}
    if(moveNode) sendVec(moveNode,0,0,0);                    // stop, but keep moveNode for next press
  } else { _driveTick(); }
}
function dpad(node){
  const h=d=>`onpointerdown="event.preventDefault();this.classList.add('on');startMove('${node}','${d}')" onpointerup="this.classList.remove('on');stopMove('${d}')" onpointerleave="this.classList.remove('on');stopMove('${d}')" onpointercancel="this.classList.remove('on');stopMove('${d}')"`;
  return `<div class="dpad">
    <button class="db up" ${h('fwd')}>▲</button>
    <button class="db left" ${h('left')}>◀</button>
    <button class="db stop" onpointerdown="this.classList.add('on')" onpointerup="this.classList.remove('on')" onpointerleave="this.classList.remove('on')" onclick="stopMove()">■</button>
    <button class="db right" ${h('right')}>▶</button>
    <button class="db down" ${h('back')}>▼</button>
  </div>`;
}
// --- analog joystick: drag the knob; vertical = forward/back, horizontal = turn, diagonal = a
// smooth curve (both axes together). Sends move/vector at ~8 Hz while held, zero on release. ---
function joystick(node){
  return `<div class="joy" data-node="${node}"><div class="joy-knob"></div></div>`;
}
function initJoysticks(){
  document.querySelectorAll('.joy').forEach(j=>{
    if(j._init) return; j._init=true;
    const knob=j.querySelector('.joy-knob'), node=j.getAttribute('data-node');
    let cx=0,cy=0,R=1,vx=0,vy=0,timer=null;
    const tick=()=>{ if(vx||vy) sendVec(node, Math.round(vy*driveSpeed), Math.round(vx*driveSpeed), 0.6, 0); };   // single joystick = heading scheme
    const aim=(px,py)=>{ let dx=(px-cx)/R, dy=(py-cy)/R; const m=Math.hypot(dx,dy); if(m>1){dx/=m;dy/=m;}
      vx=dx; vy=dy; knob.style.transform='translate('+(dx*R*0.6)+'px,'+(dy*R*0.6)+'px)'; };
    const down=e=>{ const b=j.getBoundingClientRect(); cx=b.left+b.width/2; cy=b.top+b.height/2; R=b.width/2;
      j.classList.add('drag'); try{j.setPointerCapture(e.pointerId);}catch(x){} aim(e.clientX,e.clientY);
      if(!timer) timer=setInterval(tick,120); tick(); e.preventDefault(); };
    const move=e=>{ if(!timer) return; aim(e.clientX,e.clientY); e.preventDefault(); };
    const up=()=>{ vx=vy=0; knob.style.transform='translate(0,0)'; j.classList.remove('drag');
      if(timer){clearInterval(timer);timer=null;} sendVec(node,0,0,0); };
    j.addEventListener('pointerdown',down); j.addEventListener('pointermove',move);
    j.addEventListener('pointerup',up); j.addEventListener('pointercancel',up);
  });
}
// --- fullscreen gamepad: live view fills the screen, controls overlaid ---
let fsTimer=null, fsNode=null, fsDX=0, fsDY=0, fsDriveTimer=null;
// driving-control preference (persisted): 'dual' = two sticks, 'joy' = one analog joystick.
// fsDualSwap flips which side drives vs steers. Chosen in the fullscreen Settings menu.
let fsCtrlMode = localStorage.getItem('ebo_fsctrl') || 'dual';
let fsDualSwap = localStorage.getItem('ebo_fsswap') === '1';
let fsJoySide  = localStorage.getItem('ebo_fsjoyside') || 'left';   // single-joystick side
// fullscreen top bar: battery/signal/video on the LEFT (like the video overlay), minimal actions on
// the RIGHT — Laser, Night vision (soon), Return to base, Settings. Talk/listen/record/snapshot/
// patrol will join the action row later.
function fsTop(node){
  const r=ROBOTS.find(x=>x.node===node)||{}, st=r.state||{};
  const laserOn=(st.laser==='true');
  return `<div class="fs-info">
      <button class="fs-ic" onclick="exitFS()" title="Back" style="width:40px;height:40px;font-size:24px">‹</button>
      <span class="b" id="fs-badge2">···</span>
      <span class="b" id="fs-bat">${batHtml(st.battery, st.charging)}</span>
      <span class="b" id="fs-wifi">${sigHtml(st.wifi)}</span>
      <span class="vu spk" id="vu-spk" title="Robot audio (what you hear)">🔊<span class="bar"><i></i><span class="pk"></span></span></span>
      <span class="vu mic" id="vu-mic" title="Your microphone (what the robot hears)">🎤<span class="bar"><i></i><span class="pk"></span></span></span>
    </div>
    <div class="fs-actions">
      <button class="fs-ic ${laserOn?'on':''}" id="fs-laser" onclick="toggleLaser('${node}')" title="Laser pointer (play with the cat)">🎯</button>
      <button class="fs-ic" id="fs-night" onclick="cycleNight('${node}')" title="Day/Night vision">${NV_ICON[st.night_vision]||'🌗'}</button>
      <button class="fs-ic" id="fs-listen" onclick="toggleListen('${node}')" title="Listen to the robot">🔇</button>
      <button class="fs-ic" id="fs-talk" onclick="toggleTalk('${node}')" title="Talk to the robot (hold a conversation)">🎤</button>
      ${st.routes_supported==='true' ? `<button class="fs-ic ${st.route_recording==='true'?'rec':''}" id="fs-rec" onclick="recordRoute('${node}')" title="Record a route (drive to teach a path)">⏺</button>` : ''}
      <button class="fs-ic" onclick="cmd('${node}','dock','')" title="Send it back to the charging base">🔌</button>
      <button class="fs-ic" onclick="openFsSettings()" title="Settings">⚙</button>
    </div>`;
}
// dual-stick driving: LEFT = forward/back (vertical), RIGHT = turn (horizontal). Both can be held at
// once (one thumb each) to drive a smooth curve. Sends the combined move/vector at ~8 Hz.
function _fsDriveTick(){ if(fsNode && (fsDX||fsDY)) sendVec(fsNode, Math.round(fsDY*driveSpeed), Math.round(fsDX*driveSpeed), 0.6, 1); }   // dual sticks = continuous-turn scheme
function initStick(el){
  if(el._init) return; el._init=true;
  const knob=el.querySelector('.joy-knob'), axis=el.getAttribute('data-axis');
  let cx=0,cy=0,R=1;
  const aim=(px,py)=>{ let dx=(px-cx)/R, dy=(py-cy)/R;
    if(axis==='v'){ dx=0; dy=Math.max(-1,Math.min(1,dy)); fsDY=dy; }
    else { dy=0; dx=Math.max(-1,Math.min(1,dx)); fsDX=dx; }
    knob.style.transform='translate('+(dx*R*0.6)+'px,'+(dy*R*0.6)+'px)'; };
  const down=e=>{ const b=el.getBoundingClientRect(); cx=b.left+b.width/2; cy=b.top+b.height/2;
    R = (axis==='v') ? b.height/2 : b.width/2;      // range along the stick's own (single) axis
    el.classList.add('drag'); try{el.setPointerCapture(e.pointerId);}catch(x){} aim(e.clientX,e.clientY);
    if(!fsDriveTimer) fsDriveTimer=setInterval(_fsDriveTick,120); _fsDriveTick(); e.preventDefault(); };
  const move=e=>{ if(!el.classList.contains('drag')) return; aim(e.clientX,e.clientY); e.preventDefault(); };
  const up=()=>{ el.classList.remove('drag'); if(axis==='v')fsDY=0; else fsDX=0; knob.style.transform='translate(0,0)';
    if(!fsDX && !fsDY){ if(fsDriveTimer){clearInterval(fsDriveTimer);fsDriveTimer=null;} if(fsNode) sendVec(fsNode,0,0,0); } };
  el.addEventListener('pointerdown',down); el.addEventListener('pointermove',move);
  el.addEventListener('pointerup',up); el.addEventListener('pointercancel',up);
}
function _fsStickEl(sideCls, axis){
  const a = axis==='v' ? ['▲','▼'] : ['◀','▶'];
  return `<div class="stick ${sideCls}" data-axis="${axis}"><span class="ax a1">${a[0]}</span><span class="ax a2">${a[1]}</span><div class="joy-knob"></div></div>`;
}
// Build the driving controls into #fs-drive according to the chosen mode. Dual = two one-axis sticks
// (side of drive/steer flips with fsDualSwap); Joy = one two-axis analog joystick (centred).
function renderFsControls(node){
  const d=document.getElementById('fs-drive'); if(!d) return;
  fsDX=0; fsDY=0; if(fsDriveTimer){clearInterval(fsDriveTimer);fsDriveTimer=null;}
  if(fsCtrlMode==='joy'){
    d.innerHTML=`<div class="joy fs-single ${fsJoySide}" data-node="${node}"><div class="joy-knob"></div></div>`;
    initJoysticks();
  } else {
    const leftAxis = fsDualSwap?'h':'v', rightAxis = fsDualSwap?'v':'h';
    d.innerHTML = _fsStickEl('fs-lstick',leftAxis) + _fsStickEl('fs-rstick',rightAxis);
    d.querySelectorAll('.stick').forEach(initStick);
  }
}
function setFsCtrl(mode){ fsCtrlMode=mode; localStorage.setItem('ebo_fsctrl',mode); if(fsNode) renderFsControls(fsNode); syncFsOpts(); }
function setFsSwap(on){ fsDualSwap=!!on; localStorage.setItem('ebo_fsswap',on?'1':'0'); if(fsNode) renderFsControls(fsNode); }
function setFsJoySide(side){ fsJoySide=side; localStorage.setItem('ebo_fsjoyside',side); if(fsNode) renderFsControls(fsNode); }
function syncFsOpts(){
  const sw=document.getElementById('fs-swaprow'); if(sw) sw.style.display = fsCtrlMode==='dual'?'flex':'none';
  const jr=document.getElementById('fs-joyrow'); if(jr) jr.style.display = fsCtrlMode==='joy'?'':'none';
}
function fsTab(name){
  document.querySelectorAll('#fsopts .tab').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  document.querySelectorAll('#fsopts .tabp').forEach(p=>p.style.display=(p.dataset.tab===name)?'':'none');
}
function openFsSettings(){
  const d=document.getElementById('fsopts'); const r=ROBOTS.find(x=>x.node===fsNode)||{}, st=r.state||{};
  // Driving tab: driving mode, movement speed, collision avoidance
  document.getElementById('fs-dm').innerHTML=opt(DM, st.move_mode);
  document.getElementById('fs-mspd').value=st.speed??50;
  document.getElementById('fs-mspd-v').textContent=st.speed??'—';
  document.getElementById('fs-avoid').checked = st.avoid_obstacle==='true';
  // Camera tab: night vision, video quality
  document.getElementById('fs-nv').innerHTML=opt(NV, st.night_vision);
  document.getElementById('fs-vq').innerHTML=opt(VQ, st.video_quality);
  // Audio tab: speaker volume (robot's own voice/sounds) + call volume (your voice through the robot)
  const sv=st.volume??st.playback_volume;
  document.getElementById('fs-svol').value=sv??50;
  document.getElementById('fs-svol-v').textContent=sv??'—';
  document.getElementById('fs-cvol').value=st.talkback_volume??50;
  document.getElementById('fs-cvol-v').textContent=st.talkback_volume??'—';
  // Controls tab: our joystick config
  document.getElementById('fs-spd-v').textContent=driveSpeed;
  d.querySelector('#fs-spd').value=driveSpeed;
  document.getElementById('fs-ctrl').value=fsCtrlMode;
  document.getElementById('fs-swap').checked=fsDualSwap;
  document.getElementById('fs-joyside').value=fsJoySide;
  syncFsOpts();
  fsTab('drv');
  d.showModal();
}
let wakeTimer=null;
// Fluid video: the add-on's Low-Latency HLS, played in a <video> via hls.js, PROXIED through
// Ingress (same origin) so no cross-origin/CSP trouble. Port 8888 = robot 1.
function hlsSrc(node){
  const r=ROBOTS.find(x=>x.node===node);
  if(!r||!r.rtsp) return '';
  try{
    const u=new URL(r.rtsp.replace(/^rtsp:/,'http:'));
    const port=8888+(parseInt(u.port||'8554',10)-8554);
    const path=u.pathname.replace(/^\//,'');
    return B+'/hlsp/'+port+'/'+path+'/index.m3u8';
  }catch(e){ return ''; }
}
// Are we likely OFF the robot's LAN (opened via Nabu Casa remote, a reverse proxy, a public domain,
// from cellular…)? WebRTC's media needs a DIRECT browser->host:8189/UDP hop, and mediamtx only offers
// the host's PRIVATE LAN IPs as ICE candidates (no STUN/TURN) — from remote those are unreachable, so
// WebRTC can never connect and we'd just hang ~15 s before falling back. Heuristic on the panel's own
// hostname: a private/LAN address (or a bare local name) = same network; anything else = remote. HLS
// (Ingress-proxied) works either way, so a wrong guess only costs the fluid path, never playback.
// Connection hint for the detail page: tells you, before you open fullscreen, which video path you'll
// get — fluid WebRTC on the LAN, or the slower HLS from remote. (Detected from the panel's hostname.)
function connHint(){
  return isLikelyRemote()
    ? '🔗 Remote · video will use <b>HLS</b> (~1&nbsp;s, less fluid). Fluid only on the LAN or with a relay/VPN.'
    : '🔗 On the LAN · fluid <b>WebRTC</b> video (~200&nbsp;ms)';
}
function connHintClass(){ return 'connhint'+(isLikelyRemote()?' hls':''); }
function isLikelyRemote(){
  const h=(location.hostname||'').toLowerCase();
  if(!h||h==='localhost') return false;
  if(h.startsWith('homeassistant')) return false;                 // homeassistant / homeassistant.local
  if(/\.(local|lan|internal|home|home\.arpa)$/.test(h)) return false;
  const m=h.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if(m){ const a=+m[1], b=+m[2];                                  // IPv4: RFC1918 / loopback / link-local = LAN
    if(a===10||a===127||(a===169&&b===254)) return false;
    if(a===192&&b===168) return false;
    if(a===172&&b>=16&&b<=31) return false;
    return true; }                                                // any other IPv4 → remote
  if(h.indexOf(':')>=0) return !(h==='::1'||/^\[?f[cd]/.test(h)); // IPv6: loopback / ULA (fc/fd) = LAN
  if(h.indexOf('.')<0) return false;                              // bare single-label host (mDNS/local DNS) → LAN
  return true;                                                    // FQDN (nabu.casa, duckdns, custom) → remote
}
// WebRTC (WHEP): the robot's H.265 is re-encoded to H.264 by the add-on and served by mediamtx as
// WebRTC. The browser CAN decode H.264 over WebRTC, giving ~200 ms FLUID video — the only path good
// enough to actually drive. Signalling is proxied through Ingress (same origin); the media flows
// browser<->host:8189 (UDP) directly. If ICE/WebRTC can't connect (odd network), we fall back to HLS.
function _cleanupVid(v){
  if(v._statTimer){ clearInterval(v._statTimer); v._statTimer=null; }
  if(v._pc){ try{v._pc.close();}catch(e){} v._pc=null; }
  if(v._hls){ try{v._hls.destroy();}catch(e){} v._hls=null; }
  try{ v.srcObject=null; }catch(e){}
  try{ v.removeAttribute('src'); v.load(); }catch(e){}
}
// video-mode indicator shown in the top bar (WebRTC·fps, or HLS fallback, or connecting)
let _hlsWarnTimer=null;
function _fsBadge(txt, kind){
  const el=document.getElementById('fs-badge2');   // lives inside the top info bar (fsTop)
  if(el){ el.textContent=txt; el.className='b'+(kind==='hls'?' hls':(kind==='webrtc'?' rtc':'')); }
  // Slim one-line "HLS is slower" notice: show briefly, then auto-fade (the amber HLS badge stays as
  // the persistent indicator). Only re-arm when we (re)enter HLS, not on every stats tick.
  const w=document.getElementById('fs-hlswarn');
  if(!w) return;
  if(kind==='hls'){
    if(w.dataset.shown!=='1'){                     // first time we go HLS this session
      w.dataset.shown='1'; w.style.display=''; w.classList.remove('fade');
      clearTimeout(_hlsWarnTimer);
      _hlsWarnTimer=setTimeout(()=>{ w.classList.add('fade'); setTimeout(()=>{ w.style.display='none'; },600); }, 5000);
    }
  } else {
    w.style.display='none'; w.classList.remove('fade'); w.dataset.shown='';
  }
}
function _fsWatchStats(v, pc){
  if(v._statTimer) clearInterval(v._statTimer);
  v._statTimer=setInterval(async()=>{
    if(v._pc!==pc){ return; }
    try{ const st=await pc.getStats(); let fps=null,w=0;
      st.forEach(s=>{ if(s.type==='inbound-rtp'&&s.kind==='video'){ fps=s.framesPerSecond; w=s.frameWidth||w; } });
      _fsBadge('WebRTC · '+(fps==null?'…':Math.round(fps))+'fps'+(w?' · '+w+'px':''), 'webrtc');
    }catch(e){}
  },1000);
}
function _fsStatus(msg){
  const fs=document.getElementById('fs'); if(!fs) return;
  let el=document.getElementById('fs-status');
  if(!el){ el=document.createElement('div'); el.id='fs-status';
    el.style.cssText='position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:3;color:#fff;font-size:17px;background:#000a;pointer-events:none;text-align:center;padding:20px';
    fs.appendChild(el); }
  if(msg===null){ el.style.display='none'; } else { el.textContent=msg; el.style.display='flex'; }
}
// ONE WHEP attempt: returns the connected-pending pc on an accepted offer, throws otherwise. A 404
// means the stream isn't published yet (the robot is still waking) — the caller retries.
async function whepAttempt(node){
  const r=ROBOTS.find(x=>x.node===node);
  if(!r||!r.rtsp) throw new Error('no-rtsp-entry');
  const u=new URL(r.rtsp.replace(/^rtsp:/,'http:'));
  const port=8189+(parseInt(u.port||'8554',10)-8554);
  const path=u.pathname.replace(/^\//,'');
  const v=document.getElementById('fsvid');
  const pc=new RTCPeerConnection({iceServers:[]});
  pc.addTransceiver('video',{direction:'recvonly'});
  pc.addTransceiver('audio',{direction:'recvonly'});
  pc.ontrack=(e)=>{ if(e.streams&&e.streams[0]&&v.srcObject!==e.streams[0]){ v.srcObject=e.streams[0]; v.play().catch(()=>{});} };
  const offer=await pc.createOffer(); await pc.setLocalDescription(offer);
  await new Promise(res=>{ if(pc.iceGatheringState==='complete')return res();
    const t=setTimeout(res,1200);
    pc.addEventListener('icegatheringstatechange',()=>{ if(pc.iceGatheringState==='complete'){clearTimeout(t);res();} }); });
  let resp;
  try{ resp=await fetch(B+'/whepp/'+port+'/'+path,{method:'POST',
        headers:{'Content-Type':'application/sdp'}, body:pc.localDescription.sdp}); }
  catch(e){ try{pc.close();}catch(x){} throw new Error('fetch:'+e.message); }
  if(!resp.ok){ try{pc.close();}catch(x){} throw new Error(resp.status===404?'no-publisher':('http'+resp.status)); }
  await pc.setRemoteDescription({type:'answer',sdp:await resp.text()});
  return pc;
}
function _waitConn(pc, ms){
  return new Promise(res=>{
    if(pc.connectionState==='connected') return res('connected');
    const t=setTimeout(()=>res('timeout'), ms);
    pc.addEventListener('connectionstatechange',()=>{
      if(pc.connectionState==='connected'){clearTimeout(t);res('connected');}
      else if(pc.connectionState==='failed'){clearTimeout(t);res('failed');} });
  });
}
function hlsPlay(node, attempt){
  const v=document.getElementById('fsvid'); const src=hlsSrc(node);
  if(!src) return;
  attempt=attempt||0;
  const open=()=>document.getElementById('fs').style.display==='block';
  // CRITICAL: a previous WebRTC attempt may have left a (dead) MediaStream on the element via
  // pc.ontrack. srcObject takes PRECEDENCE over MSE/src, so HLS would attach and show a BLACK
  // screen. Always detach the peer connection and clear srcObject before playing HLS.
  if(v._pc){ try{v._pc.close();}catch(e){} v._pc=null; }
  if(v._statTimer){ clearInterval(v._statTimer); v._statTimer=null; }
  if(v._hls){ try{v._hls.destroy();}catch(e){} v._hls=null; }
  try{ v.srcObject=null; }catch(e){}
  if(window.Hls && Hls.isSupported()){
    // Low-Latency HLS everywhere: measured on a real remote connection (through a Cloudflare tunnel)
    // it works and is noticeably closer to live than plain HLS. Don't "downgrade" it off-LAN — the
    // black screen people saw was the leftover WebRTC srcObject (cleared above), not LL-HLS.
    const hls=new Hls({lowLatencyMode:true, backBufferLength:4});
    v._hls=hls;
    hls.on(Hls.Events.ERROR,(e,d)=>{
      if(!d.fatal) return;
      // Try the built-in recoveries first (they keep the same session), then re-create, and finally
      // TELL THE USER instead of looping on a black screen forever.
      if(d.type===Hls.ErrorTypes.NETWORK_ERROR && attempt<3){
        try{ hls.startLoad(); return; }catch(x){}
      }
      if(d.type===Hls.ErrorTypes.MEDIA_ERROR && attempt<3){
        try{ hls.recoverMediaError(); return; }catch(x){}
      }
      try{hls.destroy();}catch(x){}
      if(!open()) return;
      if(attempt<3){ setTimeout(()=>{ if(open()) hlsPlay(node, attempt+1); }, 1500); }
      else { _fsStatus('Video unavailable over this connection ('+(d.details||d.type)+
                       '). Try again, or use the LAN/VPN for the fluid stream.'); }
    });
    hls.on(Hls.Events.MANIFEST_PARSED,()=>{ v.play().catch(()=>{}); });
    hls.loadSource(src); hls.attachMedia(v);
    v.play().catch(()=>{});
  } else if(v.canPlayType('application/vnd.apple.mpegurl')){
    // Safari / iOS webview: native HLS.
    v.src=src;
    v.addEventListener('error',()=>{ if(open()) _fsStatus('Video unavailable over this connection.'); },{once:true});
    v.play().catch(()=>{});
  }
}
// Play the fluid WebRTC. The stream appears only a few seconds AFTER camera/set on (the robot has to
// wake and produce the first frame), so we RETRY the WHEP offer until the publisher is up instead of
// giving up on the first 404 (that was the bug: it fell straight back to the ~5 s HLS). Only if the
// offer is accepted but ICE genuinely can't connect do we fall back to HLS.
async function fsPlay(node){
  const v=document.getElementById('fsvid');
  _cleanupVid(v);
  const gen=(v._gen=(v._gen||0)+1);
  const open=()=>document.getElementById('fs').style.display==='block' && v._gen===gen;
  _fsStatus('Connecting to the robot…');
  // The URL only HINTS at where you are: opening HA through a domain (Cloudflare/Nabu Casa/reverse
  // proxy) looks "remote" even when you're sitting on the same LAN as the robot — and then WebRTC
  // would work fine. So we NEVER skip WebRTC: when the URL looks remote we just probe it BRIEFLY
  // (a few seconds) and fall back to HLS if it can't connect. On the LAN (any URL) you still get the
  // fluid ~200 ms video; truly off-LAN you lose only a few seconds before HLS takes over.
  const maybeRemote=isLikelyRemote();
  const deadline=Date.now()+(maybeRemote?6000:20000);
  const connWait=maybeRemote?3500:6000;   // how long to wait for ICE per attempt
  const maxIceFails=maybeRemote?1:2;
  let iceFails=0;
  while(open() && Date.now()<deadline){
    let pc;
    try{ pc=await whepAttempt(node); }
    catch(e){ if(!open()) return; await new Promise(r=>setTimeout(r,900)); continue; }  // not ready → retry
    if(!open()){ try{pc.close();}catch(e){} return; }
    v._pc=pc;
    const st=await _waitConn(pc, connWait);
    if(!open()){ try{pc.close();}catch(e){} return; }
    if(st==='connected'){
      _fsStatus(null);                 // FLUID WebRTC is playing
      _fsWatchStats(v, pc);            // badge: WebRTC · Nfps
      // WebRTC means the browser talks to the add-on directly: it can carry the robot's High source
      // (~720p). Remember it, so next time we ask for High *before* connecting (no mid-stream switch).
      localStorage.setItem('ebo_transport','webrtc');
      const cur=(ROBOTS.find(x=>x.node===node)||{}).state||{};
      if(cur.video_quality!=='High'){ bg(node,'video_quality/set','High'); }
      pc.addEventListener('connectionstatechange',()=>{   // self-heal if the stream drops
        if((pc.connectionState==='failed'||pc.connectionState==='disconnected') && open() && v._pc===pc){
          bg(node,'camera/set','on'); setTimeout(()=>{ if(open()&&v._pc===pc) fsPlay(node); },800);
        } });
      return;
    }
    try{pc.close();}catch(e){} v._pc=null;
    if(++iceFails>=maxIceFails) break;   // answer OK but ICE won't connect → network issue → HLS
    await new Promise(r=>setTimeout(r,600));
  }
  if(open()){
    localStorage.setItem('ebo_transport','hls');
    const _st=(ROBOTS.find(x=>x.node===node)||{}).state||{};
    if(_st.video_quality!=='Low'){ bg(node,'video_quality/set','Low'); }   // keep remote watchable
    _fsBadge(maybeRemote?'HLS · remote':'HLS · fallback', 'hls');
    console.log('[ebo] WebRTC unavailable → HLS');
    _fsStatus('Starting video…');                       // cleared when it actually plays
    v.addEventListener('playing',()=>{ if(open()) _fsStatus(null); },{once:true});
    hlsPlay(node);
  }
}
let _driveVQ=null;   // video quality saved on entering drive, restored on exit
function enterFS(node){
  fsNode=node; fsDX=0; fsDY=0;
  const hw=document.getElementById('fs-hlswarn');   // re-arm the brief HLS notice for this session
  if(hw){ hw.dataset.shown=''; hw.style.display='none'; hw.classList.remove('fade'); }
  document.getElementById('fs-top').innerHTML=fsTop(node);
  updateListenUI();
  renderFsControls(node);          // dual sticks or single joystick, per the saved preference
  const v=document.getElementById('fsvid');
  v.setAttribute('data-node',node);                 // keyboard driving reads the node from here
  const fs=document.getElementById('fs'); fs.classList.remove('hidectl'); fs.style.display='block';
  fs.focus();                                       // keyboard focus so the arrow keys reach us
  bg(node,'camera/set','on');                       // join RTC + feed = wake (like opening the app)
  // FLUID DRIVING: force a low resolution while driving. The robot's High mode is 2304×1296 (3 MP),
  // which our real-time H.265→H.264 re-encode can't keep up with — frames pile up and the video lags
  // by SECONDS. At Low (848×480) the encoder keeps up → smooth ~20 fps at ~200 ms. We save the
  // previous quality and restore it on exit (so still-viewing keeps your chosen quality).
  // Quality follows the TRANSPORT, because they have opposite constraints:
  //   * LAN → WebRTC: the browser gets the stream directly, so we can afford the robot's HIGH
  //     source (2304×1296) downscaled to ~720p — measured on a 2-core host: 25 fps, 0 frames
  //     dropped, ~36% CPU. Much sharper than 480p, still fluid.
  //   * remote → HLS: everything squeezes through the proxy, so stay on LOW (848×480) to keep it
  //     watchable.
  // (The old blanket "always Low" came from a lag problem that was really the *fast* x264 preset,
  // not the resolution.)
  const r=ROBOTS.find(x=>x.node===node);
  _driveVQ=(r&&r.state&&r.state.video_quality)||null;
  // Which quality we can afford depends on the transport that will actually be used — and the URL
  // is a bad predictor (opening HA through your own domain looks "remote" even on the LAN). So we
  // remember what worked LAST time and confirm it below once the connection is really up.
  const wantVQ = (localStorage.getItem('ebo_transport')==='webrtc') ? 'High' : 'Low';
  if(_driveVQ !== wantVQ) bg(node,'video_quality/set',wantVQ);
  setTimeout(()=>fsPlay(node),400);                 // give the camera a moment, then play
  if(fs.requestFullscreen) fs.requestFullscreen().then(()=>fs.focus()).catch(()=>{});
  if(wakeTimer) clearInterval(wakeTimer);
  // keep-alive while driving: re-assert the camera/RTC session so the robot can't drift to standby
  wakeTimer=setInterval(()=>bg(node,'camera/set','on'),20000);
}
function toggleFsControls(){ document.getElementById('fs').classList.toggle('hidectl'); }
function exitFS(){
  if(talking()) stopTalk(fsNode);
  stopSpeakerMeter();
  stopMove(); if(fsTimer){clearInterval(fsTimer);fsTimer=null;}
  if(fsDriveTimer){clearInterval(fsDriveTimer);fsDriveTimer=null;} fsDX=0; fsDY=0;
  if(fsNode) sendVec(fsNode,0,0,0); fsNode=null;
  if(wakeTimer){clearInterval(wakeTimer);wakeTimer=null;}
  const v=document.getElementById('fsvid');
  const node=v.getAttribute('data-node');
  if(_driveVQ && node) bg(node,'video_quality/set',_driveVQ);   // restore the quality you had
  _driveVQ=null;
  _cleanupVid(v);                                      // stop WebRTC + HLS
  document.getElementById('fs').style.display='none';
  if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
}
// keyboard driving in fullscreen: arrow keys (or WASD) hold-to-move, Esc exits. Multiple keys held
// at once combine (e.g. Up+Right = forward-right diagonal) — each key adds/removes its own direction.
const KEYDIR={ArrowUp:'fwd',ArrowDown:'back',ArrowLeft:'left',ArrowRight:'right',w:'fwd',s:'back',a:'left',d:'right'};
// When you're typing in a field (e.g. the "Save route" name box) or ANY modal dialog is open, the
// keyboard must NOT drive the robot — otherwise 'a'/'w'/'s'/'d' move it instead of typing. (The dialog
// backdrop blocks clicks on the sticks, but key events still reach the document, hence this guard.)
function _typingOrDialog(e){
  const t=e.target, tag=t&&t.tagName;
  if(tag==='INPUT'||tag==='TEXTAREA'||tag==='SELECT'||(t&&t.isContentEditable)) return true;
  return !!document.querySelector('dialog[open]');
}
document.addEventListener('keydown',e=>{
  if(_typingOrDialog(e)) return;                 // typing / a dialog is open → let the keys type, don't drive
  const open=document.getElementById('fs').style.display==='block';
  if(e.key==='Escape'&&open){ exitFS(); return; }
  if(!open) return;
  const dir=KEYDIR[e.key]; if(!dir) return;
  e.preventDefault();
  startMove(document.getElementById('fsvid').getAttribute('data-node'),dir);   // auto-repeat ignored inside
});
document.addEventListener('keyup',e=>{
  if(_typingOrDialog(e)) return;
  const dir=KEYDIR[e.key]; if(dir){ e.preventDefault(); stopMove(dir); }
});

function listView(){
  if(!ROBOTS.length) return `<div class="empty">Waiting for robots… make sure the add-on is running.</div>`;
  return `<div class="list">`+ROBOTS.map(r=>`
    <div class="rowitem" onclick="openRobot('${r.node}')">
      <div class="thumbwrap">
        <img class="thumb prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25" style="${r.camera==='on'?'':'filter:grayscale(.6) brightness(.55)'}">
        ${r.camera==='on'?'':'<span class="zzbadge">Zz</span>'}
      </div>
      <div style="flex:1">
        <div class="ri-name"><span id="dot-${r.node}" class="dot ${r.online?'on':''}"></span>${esc(r.name||r.node)}</div>
        <div id="meta-${r.node}" class="ri-meta">${meta(r)}</div>
      </div>
      <button class="ic" title="Drive (fullscreen)" onclick="event.stopPropagation();driveNow('${r.node}')">🎮</button>
      <button class="ic" title="Open / settings" onclick="event.stopPropagation();openRobot('${r.node}')">⚙</button>
    </div>`).join('')+`</div>`;
}
function detailView(r){
  const st=r.state||{}, cam=(r.camera==='on');
  const charging = (st.charging===true || st.charging==='true');
  return `<div class="detail">
    <div class="bigwrap ${cam?'':'asleep'}" onclick="enterFS('${r.node}')" title="Tap for fullscreen">
      <img class="big prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25">
      ${cam ? `<span class="fshint">⛶ tap for fullscreen</span>
      <button class="sleepbtn" title="Send the robot to sleep (Zz)" onclick="event.stopPropagation();sleepRobot('${r.node}',this)">😴 Sleep</button>` : `
      <button class="wakebtn" title="Wake the robot" onclick="event.stopPropagation();wakeRobot('${r.node}',this)">
        <span class="ic">☀</span><span class="tx">Sleeping — tap to wake</span></button>`}
    </div>
    ${charging? '<div class="warn">🔌 On the charger — take the robot off the base to drive it.</div>':''}
    <div class="dname"><span id="d-dot" class="dot ${r.online?'on':''}"></span>${esc(r.name||r.node)}</div>
    <div id="d-meta" class="dmeta">${r.model||'EBO'} · SN ${esc(r.sn)||'—'} · ${batHtml(st.battery, st.charging)} · ${sigHtml(st.wifi)}</div>
    <div id="d-conn" class="${connHintClass()}">${connHint()}</div>
    <div class="row">
      <button id="d-cam" class="btn ${cam?'pri':''}" onclick="cmd('${r.node}','camera/set','${cam?'off':'on'}')">${cam?'Camera ON':'Camera OFF'}</button>
      <button class="btn" onclick="wakeRobot('${r.node}')">☀ Wake</button>
      <button class="btn" onclick="sleepRobot('${r.node}')">😴 Sleep (Zz)</button>
      <button id="d-laser" class="btn ${st.laser==='true'?'pri':''}" onclick="toggleLaser('${r.node}')">Laser ${st.laser==='true'?'ON':'OFF'}</button>
      <button class="btn" onclick="cmd('${r.node}','dock','')">Dock</button>
    </div>
    <div class="sec"><h4>Remote control</h4>
      <div class="drive">
        ${joystick(r.node)}
        <div class="sp">
          <button class="btn pri" style="width:100%" onclick="enterFS('${r.node}')">⛶ Fullscreen</button>
          <div class="note" style="font-size:11px;color:#8a929a;margin-top:10px">Joystick sensitivity is in the fullscreen ⚙ menu → Controls.</div>
        </div>
      </div>
      <div class="note" style="font-size:11px;color:#8a929a;margin-top:8px">Drag the joystick to drive: up = forward, sides = turn, diagonal = curve. The camera must be on to see the live view.</div>
    </div>
    <div class="sec"><h4>Driving</h4>
      <label>Driving mode</label><select onchange="cmd('${r.node}','move_mode/set',this.value)">${opt(DM,st.move_mode)}</select>
      <label>Movement speed (${st.speed??'—'})</label>
      <input type="range" min="1" max="100" value="${st.speed??50}" onchange="cmd('${r.node}','speed/set',this.value)">
      <label class="tgl"><span>Collision avoidance</span>
        <input type="checkbox" ${st.avoid_obstacle==='true'?'checked':''} onchange="cmd('${r.node}','avoid_obstacle/set',this.checked?'on':'off')"></label>
    </div>
    <div class="sec"><h4>Camera &amp; display</h4>
      <label>Night vision</label><select onchange="cmd('${r.node}','night_vision/set',this.value)">${opt(NV,st.night_vision)}</select>
      <label>Video quality</label><select onchange="cmd('${r.node}','video_quality/set',this.value)">${opt(VQ,st.video_quality)}</select>
      <label>Image style</label><select onchange="cmd('${r.node}','image_style/set',this.value)">${opt(IS,st.image_style)}</select>
      <label>Eyes</label><select onchange="cmd('${r.node}','eyes/set',this.value)">${opt(EY,st.eyes)}</select>
    </div>
    <div class="sec"><h4>Audio</h4>
      <label class="tgl"><span>Listen — hear the robot's microphone</span>
        <input type="checkbox" ${st.listen!=='false'?'checked':''} onchange="cmd('${r.node}','listen/set',this.checked?'on':'off')"></label>
      <div class="note" style="font-size:11px;color:#8a929a;margin-top:2px">The audio comes through the camera stream — unmute the player to hear it.</div>
      <label>Speaker volume — the robot's own voice &amp; sounds (${st.volume??st.playback_volume??'—'})</label>
      <input type="range" min="0" max="100" value="${st.volume??st.playback_volume??50}" onchange="cmd('${r.node}','volume/set',this.value)">
      <label>Call volume — your voice through the robot, two-way talk (${st.talkback_volume??'—'})</label>
      <input type="range" min="0" max="100" value="${st.talkback_volume??50}" onchange="cmd('${r.node}','talkback_volume/set',this.value)">
    </div>
    <div class="sec"><h4>Recording</h4>
      <label class="tgl"><span>Motion recording — logs the robot's activity (not a path)</span>
        <input type="checkbox" ${st.sports_record==='true'?'checked':''} onchange="cmd('${r.node}','sports_record/set',this.checked?'on':'off')"></label>
    </div>
    ${st.routes_supported==='true' ? `<div class="sec"><h4>Routes — teach &amp; repeat</h4>
      <div id="d-routes">${routesHtml(r)}</div>
      <div class="note" style="font-size:11px;color:#8a929a;margin-top:8px">Record a new route from the ⛶ fullscreen view: tap ⏺ to start, drive the path, tap ⏺ again to stop, then name &amp; save it. Repeat any saved route here.</div>
    </div>` : ''}
    <div class="row" style="margin-top:14px"><button class="btn danger" onclick="removeRobot('${r.node}')">🗑 Remove from account</button></div>
  </div>`;
}
let lastSig=null;
// The signature decides when to REBUILD the view (vs just refreshing values). It includes each
// robot's camera state so the "sleeping" look — dimmed frame, Zz badge, the big wake button —
// appears and disappears as the robot falls asleep or wakes up.
function camOf(n){ const r=ROBOTS.find(x=>x.node===n); return (r&&r.camera)||''; }
function sig(){ return SEL ? 'd:'+SEL+':'+camOf(SEL)
                           : 'l:'+ROBOTS.map(r=>r.node+':'+(r.camera||'')).join(','); }
function render(force){
  const s=sig();
  if(!force && s===lastSig){ updateValues(); return; }   // same structure: update in place, don't rebuild (keeps the live preview from flickering)
  lastSig=s;
  document.getElementById('addbtn').style.display = SEL?'none':'';
  document.getElementById('title').innerHTML = SEL? '‹ EBO' : '🤖 EBO';
  const r = SEL && ROBOTS.find(x=>x.node===SEL);
  document.getElementById('view').innerHTML = r? detailView(r) : listView();
  initJoysticks();      // wire the analog joystick(s) in the freshly-rendered detail view
}
function updateValues(){
  if(SEL){
    const r=ROBOTS.find(x=>x.node===SEL); if(!r) return;
    const st=r.state||{}, cam=(r.camera==='on');
    const dot=document.getElementById('d-dot'); if(dot) dot.className='dot '+(r.online?'on':'');
    const m=document.getElementById('d-meta'); if(m) m.innerHTML=`${r.model||'EBO'} · SN ${esc(r.sn)||'—'} · ${batHtml(st.battery, st.charging)} · ${sigHtml(st.wifi)}`;
    const cb=document.getElementById('d-cam'); if(cb){ cb.className='btn '+(cam?'pri':''); cb.textContent=cam?'Camera ON':'Camera OFF'; cb.setAttribute('onclick',`cmd('${r.node}','camera/set','${cam?'off':'on'}')`); }
    updateLaserUI(SEL);                                    // keep the laser toggle (detail + fullscreen) in sync
    updateNightUI(SEL);                                    // keep the day/night button icon in sync
    updateRecUI(SEL);                                      // keep the route-record button in sync
    const rc=document.getElementById('d-routes'); if(rc) rc.innerHTML=routesHtml(r);   // live routes list
    if(document.getElementById('fs').style.display==='block'){   // fullscreen open: refresh its top-bar info
      const fb=document.getElementById('fs-bat'); if(fb) fb.innerHTML=batHtml(st.battery, st.charging);
      const fw=document.getElementById('fs-wifi'); if(fw) fw.innerHTML=sigHtml(st.wifi);
    }
  }else{
    ROBOTS.forEach(r=>{
      const dot=document.getElementById('dot-'+r.node); if(dot) dot.className='dot '+(r.online?'on':'');
      const m=document.getElementById('meta-'+r.node); if(m) m.innerHTML=meta(r);   // meta() returns markup (battery/wifi gauges)
    });
  }
}
async function refresh(){
  try{ ROBOTS = await (await fetch(B+'/api/robots')).json(); render(); }catch(e){}
}
let pairTimer=null;
function openAdd(){
  const ssid=(ROBOTS[0]&&ROBOTS[0].state&&ROBOTS[0].state.ssid)||'';
  document.getElementById('a-ssid').value=ssid;
  document.getElementById('a-pass').value='';
  document.getElementById('addform').style.display='';
  document.getElementById('addqr').style.display='none';
  document.getElementById('add').showModal();
}
async function pairStart(){
  const ssid=document.getElementById('a-ssid').value.trim();
  const password=document.getElementById('a-pass').value;
  if(!ssid){alert('Enter the Wi-Fi name');return;}
  const r=await fetch(B+'/api/pair/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ssid,password})});
  const j=await r.json();
  if(!r.ok||j.error){alert('Could not start pairing: '+(j.error||r.status));return;}
  document.getElementById('addform').style.display='none';
  document.getElementById('addqr').style.display='';
  document.getElementById('qrimg').src=B+'/api/pair/qr?t='+Date.now();
  document.getElementById('pairmsg').textContent='Waiting for the robot to scan…';
  pairTimer=setInterval(pairPoll,3000);
}
async function pairPoll(){
  try{
    const j=await (await fetch(B+'/api/pair/status',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
    if(j.status===200){
      clearInterval(pairTimer); pairTimer=null;
      document.getElementById('pairmsg').innerHTML='✅ Robot paired! Restarting to bring it online…';
      await fetch(B+'/api/restart',{method:'POST'}); setTimeout(stopPair,2500);
    }else{
      document.getElementById('pairmsg').textContent='Waiting for the robot to scan… (status '+(j.status??'…')+')';
    }
  }catch(e){}
}
function stopPair(){ if(pairTimer){clearInterval(pairTimer);pairTimer=null;} document.getElementById('add').close(); }
async function removeRobot(node){
  const r=ROBOTS.find(x=>x.node===node); const name=r?(r.name||node):node;
  if(!confirm('Remove "'+name+'" from your Enabot account?\n\nThis UNBINDS the robot (you will need to pair it again to use it). This cannot be undone.')) return;
  const res=await fetch(B+'/api/robot/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({node})});
  const j=await res.json();
  if(res.ok&&j.ok){ alert('Removed. The add-on is restarting…'); goBack(); }
  else alert('Could not remove: '+(j.error||res.status));
}
// smooth preview: preload the next snapshot off-screen, then swap it in on load (no blank/flicker)
function previewLoop(){
  document.querySelectorAll('img.prev').forEach(el=>{
    const n=el.getAttribute('data-node'); if(!n) return;
    const im=new Image();
    im.onload=()=>{ el.src=im.src; el.style.opacity=1; };
    im.src=B+'/api/snapshot?node='+encodeURIComponent(n)+'&t='+Date.now();
  });
}
fetch(B+'/api/account').then(r=>r.json()).then(a=>{ if(a.email) document.getElementById('acct').textContent=' · '+a.email; }).catch(()=>{});
refresh(); setInterval(refresh, 4000);
previewLoop(); setInterval(previewLoop, 300);
</script></body></html>"""


def main():
    try:
        _start_mqtt()
    except Exception as e:
        log("[panel] MQTT connect failed:", e)
    # token-guarded data API for the native integration (host-mapped port)
    api = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    api.require_token = True
    threading.Thread(target=api.serve_forever, daemon=True).start()
    log("[panel] data API on :%d (token-guarded)" % API_PORT)
    # Ingress UI (authenticated by HA)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.require_token = False
    log("[panel] Ingress UI on :%d" % PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
