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
PANEL_CFG = "/data/panel.json"

# Command suffixes the panel may publish (allow-list). Movement is excluded on purpose.
ALLOWED_CMDS = {
    "camera/set", "laser/set", "dock", "sleep/set", "wake", "connected/set", "patrol/start",
    "say", "talk",
    "video_quality/set", "image_style/set", "volume/set", "talkback_volume/set",
    "speed/set", "sports_record/set", "call_rec/set", "eyes/set",
}

# Add-on settings the panel manages (stored in /data/panel.json, read by run.sh). Everything
# except the account login (email/password) lives here now, not in the Configuration tab.
EDITABLE_OPTS = {
    # NB: expose_mqtt now lives in the add-on options (Configuration tab / set by the integration),
    # not here — so it can be provisioned by the Enabot integration via Supervisor.
    "video": {"type": "bool", "default": True, "label": "Video"},
    "audio": {"type": "bool", "default": True, "label": "Audio (listen — best-effort)"},
    "talk": {"type": "bool", "default": False, "label": "Talk (speak to the robot)"},
    "video_max_height": {"type": "int", "default": 720, "label": "Video max height (px)"},
    "video_fps": {"type": "int", "default": 20, "label": "Video FPS"},
    "video_bitrate": {"type": "int", "default": 2500, "label": "Video bitrate (kbps, 0 = uncapped)"},
    "video_preset": {"type": "select",
                     "choices": ["ultrafast", "superfast", "veryfast", "faster", "fast"],
                     "default": "ultrafast", "label": "Video encoder preset"},
    "audio_codec": {"type": "select", "choices": [8, 9], "default": 8, "label": "Audio codec"},
    # log_level lives in the add-on Configuration tab now (not here).
    "region": {"type": "text", "default": "GB", "label": "Account region"},
    "host": {"type": "text", "default": "ebox-eu.enabotserverintl.com",
             "label": "Account server host"},
    "robot_id": {"type": "int", "default": 0, "label": "Robot id (0 = all robots)"},
}

_robots = {}
_lock = threading.Lock()
_snap_cache = {}
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
def _read_cfg():
    try:
        with open(PANEL_CFG) as f:
            cur = json.load(f)
    except Exception:
        cur = {}
    return {k: cur.get(k, s["default"]) for k, s in EDITABLE_OPTS.items()}


def _coerce(k, v):
    t = EDITABLE_OPTS[k]["type"]
    if t == "bool":
        return v is True or str(v).lower() == "true"
    if t == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            return EDITABLE_OPTS[k]["default"]
    if t == "select" and all(isinstance(c, int) for c in EDITABLE_OPTS[k]["choices"]):
        try:
            return int(v)
        except (TypeError, ValueError):
            return EDITABLE_OPTS[k]["default"]
    return v


def _save_cfg(patch):
    cur = _read_cfg()
    for k, v in patch.items():
        if k in EDITABLE_OPTS:
            cur[k] = _coerce(k, v)
    with open(PANEL_CFG, "w") as f:
        json.dump(cur, f)
    log("[panel] saved /data/panel.json — restarting add-on to apply")
    threading.Thread(target=_restart_self, daemon=True).start()


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
    if not url:
        return None
    now = time.time()
    ts, cached = _snap_cache.get(node, (0, None))
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
            ["ffmpeg", "-nostdin", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "32", "-analyzeduration", "0", "-rtsp_transport", "tcp",
             "-i", internal, "-frames:v", "1", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
            capture_output=True, timeout=8).stdout
        if out:
            _snap_cache[node] = (time.time(), out)
            return out
    except Exception:
        pass
    finally:
        lock.release()
    return cached


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
        if path.endswith("/api/options"):
            return self._send(200, json.dumps({"values": _read_cfg(), "schema": EDITABLE_OPTS}))
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

    def _proxy_whep(self, raw_path):
        """Forward a WHEP SDP offer to the local mediamtx WebRTC endpoint and return its SDP answer.
        Restricted to the WebRTC ports (8189-8192) so it can't be used as an open proxy."""
        rest = raw_path.split("/whepp/", 1)[1]
        port, _, sub = rest.partition("/")
        if not port.isdigit() or not (8189 <= int(port) <= 8192) or not sub:
            return self._send(400, b"", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            offer = self.rfile.read(n)
        except Exception:
            return self._send(400, b"", "text/plain")
        url = "http://127.0.0.1:%s/%s/whep" % (port, sub)
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
            log("[whep] proxy failed:", e)
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
        if path.endswith("/api/options"):
            try:
                _save_cfg(body.get("options", {}))
                return self._send(200, json.dumps({"ok": True, "restarting": True}))
            except Exception as e:
                log("[panel] save options failed:", e)
                return self._send(500, json.dumps({"error": str(e)}))
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
/* fullscreen gamepad */
#fs{position:fixed;inset:0;background:#000;z-index:9999;display:none}
#fsvid{position:absolute;inset:0;width:100%;height:100%;border:0;background:#000;object-fit:contain}
.fs-tap{position:absolute;inset:0;z-index:1}   /* tap the video to show/hide controls */
.fsx{position:absolute;top:12px;right:14px;z-index:2;background:#000a;color:#fff;border:0;border-radius:50%;width:42px;height:42px;font-size:18px;cursor:pointer}
.fs-pad{position:absolute;left:22px;bottom:22px;z-index:2;opacity:.92}
.fs-pad .dpad{grid-template-columns:repeat(3,68px);grid-template-rows:repeat(3,68px)}
.fs-pad .db{background:#ffffff26;color:#fff;backdrop-filter:blur(3px)}
.fs-act{position:absolute;right:22px;bottom:22px;z-index:2;display:flex;flex-direction:column;gap:10px;opacity:.92}
.fs-act .btn{background:#ffffff26;color:#fff;backdrop-filter:blur(3px);min-width:120px}
.fs-sp{position:absolute;left:50%;bottom:26px;transform:translateX(-50%);z-index:2;width:200px;opacity:.9}
#fs.hidectl .fs-pad,#fs.hidectl .fs-act,#fs.hidectl .fs-sp{display:none}
/* press feedback (so you SEE the button react) */
.btn:active{transform:scale(.96);filter:brightness(1.3)}
.db.on,.db:active{background:#2b6cff !important;color:#fff !important;transform:scale(.9)}
.fs-pad .db.on,.fs-pad .db:active{background:#2b6cffcc !important}
.fs-act .btn:active{background:#2b6cffcc !important}
/* list action icons + detail camera hint + charging warning */
.ic{border:0;background:transparent;font-size:20px;cursor:pointer;padding:6px 8px;border-radius:9px;line-height:1;flex:none}
.ic:hover{background:#0001}
@media(prefers-color-scheme:dark){.ic:hover{background:#ffffff14}}
.bigwrap{position:relative;cursor:pointer}
.fshint{position:absolute;right:10px;bottom:20px;background:#0008;color:#fff;font-size:11px;padding:3px 8px;border-radius:8px;pointer-events:none}
.warn{background:#e67e22;color:#fff;border-radius:10px;padding:9px 12px;font-size:13px;margin:10px 0;font-weight:600}
dialog{border:0;border-radius:14px;padding:0;max-width:440px;width:92%;background:#fff;color:#111}
@media(prefers-color-scheme:dark){dialog{background:#1c2126;color:#e9ecef}}
dialog .in{padding:18px}h3{margin:0 0 10px}.note{font-size:12px;color:#8a929a;margin-top:10px}
</style></head><body>
<header>
  <span><span id="title" onclick="goBack()" style="cursor:pointer">🤖 EBO</span>
        <span id="acct" style="font-size:12px;color:#8a929a;font-weight:400"></span></span>
  <span><button class="btn" id="addbtn" onclick="openAdd()">+ Add robot</button>
        <button class="btn" onclick="openOpts()">⚙ Settings</button></span>
</header>
<div id="view"></div>

<div id="fs" tabindex="0">
  <video id="fsvid" autoplay muted playsinline></video>
  <div class="fs-tap" onclick="toggleFsControls()"></div>
  <button class="fsx" onclick="exitFS()">✕</button>
  <div class="fs-pad" id="fs-pad"></div>
  <input class="fs-sp" id="fs-sp" type="range" min="1" max="100" value="60" oninput="driveSpeed=+this.value">
  <div class="fs-act" id="fs-act"></div>
</div>

<dialog id="opts"><div class="in">
  <h3>Add-on settings</h3><div id="optform"></div>
  <div class="row" style="justify-content:flex-end;margin-top:16px">
    <button class="btn" onclick="document.getElementById('opts').close()">Cancel</button>
    <button class="btn pri" onclick="saveOpts()">Save &amp; restart</button></div>
  <div class="note">Saving restarts the add-on (brief interruption).</div>
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
const VQ=["Low","Medium","High"], IS=["Standard","Vivid","Soft"], EY=["Dynamic","Clock","Custom"];
let ROBOTS=[], SEL=null;
async function cmd(node,suffix,payload){
  await fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({node,suffix,payload})}); setTimeout(refresh,400);
}
function esc(s){return (s==null?'':(''+s))}
function opt(list,cur){return list.map(v=>`<option ${v==cur?'selected':''}>${v}</option>`).join('')}
function meta(r){const st=r.state||{};
  const bat=(st.battery!=null)?st.battery+'%':'—', wifi=(st.wifi!=null?st.wifi:(st.rssi!=null?st.rssi:'—'));
  return `${r.model||'EBO'} · 🔋 ${bat} · 📶 ${wifi}`;}
function thumb(n){return `${B}/api/snapshot?node=${encodeURIComponent(n)}&t=${Math.floor(Date.now()/4000)}`}
function bg(node,suffix,payload){ fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({node,suffix,payload})}).catch(()=>{}); }
// Enter detail/drive → camera/set on. Bridge-side this JOINS the Agora RTC channel, which WAKES
// the robot exactly like opening the app (real viewer present). goBack → connected/set off leaves
// the channel so the robot goes back to standby (ZZ). No unreliable isSleeping opcode dance.
function openRobot(n){ SEL=n; render(true); bg(n,'camera/set','on'); }   // join RTC = wake (like the app)
function goBack(){ const p=SEL; SEL=null; render(true); if(p) bg(p,'connected/set','off'); }  // leave = standby
function driveNow(n){ SEL=n; render(true); bg(n,'camera/set','on'); setTimeout(()=>enterFS(n),60); }

// --- driving: hold direction(s) to move, release to stop. MULTIPLE directions COMBINE into one
// analog vector (move/vector carries ly=forward/back AND rx=turn together), so forward+right drives
// a smooth diagonal instead of only the last key winning. A watchdog re-sends while held. ---
let driveSpeed=60, moveNode=null, moveTimer=null;
const pressed=new Set();          // currently-held directions (keyboard and/or D-pad)
function sendVec(node,ly,rx,hold){
  fetch(B+'/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({node,suffix:'move/vector',payload:JSON.stringify({ly,rx,hold})})}).catch(()=>{});
}
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
  sendVec(moveNode, Math.round(ly*driveSpeed), Math.round(rx*driveSpeed), 0.7);
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
// --- fullscreen gamepad: live view fills the screen, controls overlaid ---
let fsTimer=null;
function fsActions(node){
  const b=(s,p,t)=>`<button class="btn" onclick="cmd('${node}','${s}','${p}')">${t}</button>`;
  return b('camera/set','on','☀ Wake')+b('laser/set','on','• Laser')+b('dock','','⌂ Dock')+b('connected/set','off','🌙 Standby');
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
// small diagnostic badge (top-left): shows whether the live view is WebRTC (fluid) or HLS (fallback)
// and the live decoded fps — so we can see, while driving, exactly what the video path is doing.
function _fsBadge(txt){
  const fs=document.getElementById('fs'); if(!fs) return;
  let el=document.getElementById('fs-badge');
  if(!el){ el=document.createElement('div'); el.id='fs-badge';
    el.style.cssText='position:absolute;top:10px;left:10px;z-index:4;background:#000b;color:#0f8;font:12px monospace;padding:4px 8px;border-radius:8px;pointer-events:none';
    fs.appendChild(el); }
  el.textContent=txt;
}
function _fsWatchStats(v, pc){
  if(v._statTimer) clearInterval(v._statTimer);
  v._statTimer=setInterval(async()=>{
    if(v._pc!==pc){ return; }
    try{ const st=await pc.getStats(); let fps=null,w=0;
      st.forEach(s=>{ if(s.type==='inbound-rtp'&&s.kind==='video'){ fps=s.framesPerSecond; w=s.frameWidth||w; } });
      _fsBadge('WebRTC · '+(fps==null?'…':Math.round(fps))+'fps'+(w?' · '+w+'px':''));
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
function hlsPlay(node){
  const v=document.getElementById('fsvid'); const src=hlsSrc(node);
  if(!src) return;
  if(window.Hls && Hls.isSupported()){
    const hls=new Hls({lowLatencyMode:true, backBufferLength:4});
    hls.on(Hls.Events.ERROR,(e,d)=>{ if(d.fatal){ try{hls.destroy();}catch(x){} setTimeout(()=>{ if(document.getElementById('fs').style.display==='block') hlsPlay(node); },1500); }});
    hls.loadSource(src); hls.attachMedia(v); v._hls=hls;
    v.play().catch(()=>{});
  } else if(v.canPlayType('application/vnd.apple.mpegurl')){
    v.src=src; v.play().catch(()=>{});
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
  _fsStatus('Connessione al robot…');
  const deadline=Date.now()+20000;   // keep trying while the robot wakes + first frame arrives
  let iceFails=0;
  while(open() && Date.now()<deadline){
    let pc;
    try{ pc=await whepAttempt(node); }
    catch(e){ if(!open()) return; await new Promise(r=>setTimeout(r,900)); continue; }  // not ready → retry
    if(!open()){ try{pc.close();}catch(e){} return; }
    v._pc=pc;
    const st=await _waitConn(pc, 6000);
    if(!open()){ try{pc.close();}catch(e){} return; }
    if(st==='connected'){
      _fsStatus(null);                 // FLUID WebRTC is playing
      _fsWatchStats(v, pc);            // badge: WebRTC · Nfps
      pc.addEventListener('connectionstatechange',()=>{   // self-heal if the stream drops
        if((pc.connectionState==='failed'||pc.connectionState==='disconnected') && open() && v._pc===pc){
          bg(node,'camera/set','on'); setTimeout(()=>{ if(open()&&v._pc===pc) fsPlay(node); },800);
        } });
      return;
    }
    try{pc.close();}catch(e){} v._pc=null;
    if(++iceFails>=2) break;          // answer OK but ICE won't connect → network issue → HLS
    await new Promise(r=>setTimeout(r,600));
  }
  if(open()){ _fsStatus(null); _fsBadge('HLS (ripiego)'); console.log('[ebo] WHEP unavailable → HLS fallback'); hlsPlay(node); }
}
let _driveVQ=null;   // video quality saved on entering drive, restored on exit
function enterFS(node){
  document.getElementById('fs-pad').innerHTML=dpad(node);
  document.getElementById('fs-act').innerHTML=fsActions(node);
  const v=document.getElementById('fsvid');
  v.setAttribute('data-node',node);                 // keyboard driving reads the node from here
  document.getElementById('fs-sp').value=driveSpeed;
  const fs=document.getElementById('fs'); fs.classList.remove('hidectl'); fs.style.display='block';
  fs.focus();                                       // keyboard focus so the arrow keys reach us
  bg(node,'camera/set','on');                       // join RTC + feed = wake (like opening the app)
  // FLUID DRIVING: force a low resolution while driving. The robot's High mode is 2304×1296 (3 MP),
  // which our real-time H.265→H.264 re-encode can't keep up with — frames pile up and the video lags
  // by SECONDS. At Low (848×480) the encoder keeps up → smooth ~20 fps at ~200 ms. We save the
  // previous quality and restore it on exit (so still-viewing keeps your chosen quality).
  const r=ROBOTS.find(x=>x.node===node);
  _driveVQ=(r&&r.state&&r.state.video_quality)||null;
  if(_driveVQ && _driveVQ!=='Low') bg(node,'video_quality/set','Low');
  setTimeout(()=>fsPlay(node),400);                 // give the camera a moment, then play
  if(fs.requestFullscreen) fs.requestFullscreen().then(()=>fs.focus()).catch(()=>{});
  if(wakeTimer) clearInterval(wakeTimer);
  // keep-alive while driving: re-assert the camera/RTC session so the robot can't drift to standby
  wakeTimer=setInterval(()=>bg(node,'camera/set','on'),20000);
}
function toggleFsControls(){ document.getElementById('fs').classList.toggle('hidectl'); }
function exitFS(){
  stopMove(); if(fsTimer){clearInterval(fsTimer);fsTimer=null;}
  if(wakeTimer){clearInterval(wakeTimer);wakeTimer=null;}
  const v=document.getElementById('fsvid');
  const node=v.getAttribute('data-node');
  if(_driveVQ && _driveVQ!=='Low' && node) bg(node,'video_quality/set',_driveVQ);   // restore quality
  _driveVQ=null;
  _cleanupVid(v);                                      // stop WebRTC + HLS
  document.getElementById('fs').style.display='none';
  if(document.fullscreenElement) document.exitFullscreen().catch(()=>{});
}
// keyboard driving in fullscreen: arrow keys (or WASD) hold-to-move, Esc exits. Multiple keys held
// at once combine (e.g. Up+Right = forward-right diagonal) — each key adds/removes its own direction.
const KEYDIR={ArrowUp:'fwd',ArrowDown:'back',ArrowLeft:'left',ArrowRight:'right',w:'fwd',s:'back',a:'left',d:'right'};
document.addEventListener('keydown',e=>{
  const open=document.getElementById('fs').style.display==='block';
  if(e.key==='Escape'&&open){ exitFS(); return; }
  if(!open) return;
  const dir=KEYDIR[e.key]; if(!dir) return;
  e.preventDefault();
  startMove(document.getElementById('fsvid').getAttribute('data-node'),dir);   // auto-repeat ignored inside
});
document.addEventListener('keyup',e=>{
  const dir=KEYDIR[e.key]; if(dir){ e.preventDefault(); stopMove(dir); }
});

function listView(){
  if(!ROBOTS.length) return `<div class="empty">Waiting for robots… make sure the add-on is running.</div>`;
  return `<div class="list">`+ROBOTS.map(r=>`
    <div class="rowitem" onclick="openRobot('${r.node}')">
      <img class="thumb prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25">
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
    <div class="bigwrap" onclick="enterFS('${r.node}')" title="Tap for fullscreen">
      <img class="big prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25">
      <span class="fshint">⛶ tap for fullscreen</span>
    </div>
    ${charging? '<div class="warn">🔌 On the charger — take the robot off the base to drive it.</div>':''}
    <div class="dname"><span id="d-dot" class="dot ${r.online?'on':''}"></span>${esc(r.name||r.node)}</div>
    <div id="d-meta" class="dmeta">${r.model||'EBO'} · SN ${esc(r.sn)||'—'} · 🔋 ${st.battery??'—'}% · 📶 ${st.wifi??'—'}</div>
    <div class="row">
      <button id="d-cam" class="btn ${cam?'pri':''}" onclick="cmd('${r.node}','camera/set','${cam?'off':'on'}')">${cam?'Camera ON':'Camera OFF'}</button>
      <button class="btn" onclick="cmd('${r.node}','camera/set','on')">☀ Wake</button>
      <button class="btn" onclick="cmd('${r.node}','connected/set','off')">🌙 Standby</button>
      <button class="btn" onclick="cmd('${r.node}','laser/set','on')">Laser</button>
      <button class="btn" onclick="cmd('${r.node}','dock','')">Dock</button>
    </div>
    <div class="sec"><h4>Drive</h4>
      <div class="drive">
        ${dpad(r.node)}
        <div class="sp">
          <label>Speed (${driveSpeed})</label>
          <input type="range" min="1" max="100" value="${driveSpeed}" oninput="driveSpeed=+this.value;this.previousElementSibling.textContent='Speed ('+this.value+')'">
          <button class="btn pri" style="margin-top:12px;width:100%" onclick="enterFS('${r.node}')">⛶ Fullscreen gamepad</button>
        </div>
      </div>
      <div class="note" style="font-size:11px;color:#8a929a;margin-top:8px">Hold a button to move; release to stop. The camera must be on to see the live view.</div>
    </div>
    <div class="sec"><h4>Robot settings</h4>
      <label>Video quality</label><select onchange="cmd('${r.node}','video_quality/set',this.value)">${opt(VQ,st.video_quality)}</select>
      <label>Image style</label><select onchange="cmd('${r.node}','image_style/set',this.value)">${opt(IS,st.image_style)}</select>
      <label>Eyes</label><select onchange="cmd('${r.node}','eyes/set',this.value)">${opt(EY,st.eyes)}</select>
      <label>Volume (${st.volume??st.playback_volume??'—'})</label>
      <input type="range" min="0" max="100" value="${st.volume??st.playback_volume??50}" onchange="cmd('${r.node}','volume/set',this.value)">
      <label>Speed (${st.speed??'—'})</label>
      <input type="range" min="1" max="100" value="${st.speed??50}" onchange="cmd('${r.node}','speed/set',this.value)">
      <div class="row">
        <button class="btn" onclick="cmd('${r.node}','sports_record/set','on')">Motion rec ON</button>
        <button class="btn" onclick="cmd('${r.node}','sports_record/set','off')">OFF</button>
      </div>
    </div>
    <div class="row" style="margin-top:14px"><button class="btn danger" onclick="removeRobot('${r.node}')">🗑 Remove from account</button></div>
    <div class="url">${esc(r.url)}</div>
  </div>`;
}
let lastSig=null;
function sig(){ return SEL ? 'd:'+SEL : 'l:'+ROBOTS.map(r=>r.node).join(','); }
function render(force){
  const s=sig();
  if(!force && s===lastSig){ updateValues(); return; }   // same structure: update in place, don't rebuild (keeps the live preview from flickering)
  lastSig=s;
  document.getElementById('addbtn').style.display = SEL?'none':'';
  document.getElementById('title').innerHTML = SEL? '‹ EBO' : '🤖 EBO';
  const r = SEL && ROBOTS.find(x=>x.node===SEL);
  document.getElementById('view').innerHTML = r? detailView(r) : listView();
}
function updateValues(){
  if(SEL){
    const r=ROBOTS.find(x=>x.node===SEL); if(!r) return;
    const st=r.state||{}, cam=(r.camera==='on');
    const dot=document.getElementById('d-dot'); if(dot) dot.className='dot '+(r.online?'on':'');
    const m=document.getElementById('d-meta'); if(m) m.textContent=`${r.model||'EBO'} · SN ${esc(r.sn)||'—'} · 🔋 ${st.battery??'—'}% · 📶 ${st.wifi??'—'}`;
    const cb=document.getElementById('d-cam'); if(cb){ cb.className='btn '+(cam?'pri':''); cb.textContent=cam?'Camera ON':'Camera OFF'; cb.setAttribute('onclick',`cmd('${r.node}','camera/set','${cam?'off':'on'}')`); }
  }else{
    ROBOTS.forEach(r=>{
      const dot=document.getElementById('dot-'+r.node); if(dot) dot.className='dot '+(r.online?'on':'');
      const m=document.getElementById('meta-'+r.node); if(m) m.textContent=meta(r);
    });
  }
}
async function refresh(){
  try{ ROBOTS = await (await fetch(B+'/api/robots')).json(); render(); }catch(e){}
}
async function openOpts(){
  const d = await (await fetch(B+'/api/options')).json(); const sc=d.schema, v=d.values;
  let h='';
  for(const k in sc){const s=sc[k];
    h+=`<label>${s.label||k}</label>`;
    if(s.type==='bool') h+=`<select id="o-${k}"><option ${v[k]?'selected':''}>true</option><option ${!v[k]?'selected':''}>false</option></select>`;
    else if(s.type==='select') h+=`<select id="o-${k}">${s.choices.map(c=>`<option ${c==v[k]?'selected':''}>${c}</option>`).join('')}</select>`;
    else if(s.type==='text') h+=`<input id="o-${k}" type="text" value="${v[k]??''}">`;
    else h+=`<input id="o-${k}" type="number" value="${v[k]??''}">`;
  }
  document.getElementById('optform').innerHTML=h;
  document.getElementById('opts').showModal();
}
async function saveOpts(){
  const d = await (await fetch(B+'/api/options')).json(); const sc=d.schema; const out={};
  for(const k in sc){const el=document.getElementById('o-'+k); if(!el)continue;
    out[k] = sc[k].type==='bool' ? (el.value==='true') : el.value;}
  await fetch(B+'/api/options',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({options:out})});
  document.getElementById('opts').close(); alert('Saved. The add-on is restarting…');
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
