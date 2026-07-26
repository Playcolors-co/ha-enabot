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
    "expose_mqtt": {"type": "bool", "default": True,
                    "label": "Expose entities over MQTT (off = native integration only)"},
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
    "log_level": {"type": "select", "choices": ["debug", "info", "warning"], "default": "info",
                  "label": "Log level"},
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
    for t in ("ebo_air2/discovery/#", "+/status", "+/state", "+/camera/state", "+/camera/url"):
        client.subscribe(t)


def _on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace")
        if topic.startswith("ebo_air2/discovery/"):
            data = json.loads(payload) if payload else {}
            node = data.get("node") or topic.rsplit("/", 1)[-1]
            with _lock:
                _robot(node).update({k: data.get(k) for k in
                                     ("name", "sn", "mac", "model", "rtsp")})
            return
        node = topic.split("/", 1)[0]
        # the +/status, +/state wildcards also catch non-EBO topics (e.g. homeassistant/status) —
        # only track real EBO nodes (from discovery, or the ebo_air2 prefix).
        if node not in _robots and not node.startswith("ebo_air2"):
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
    if cached and now - ts < 0.8:
        return cached
    # one grab per node at a time — concurrent requests get the last frame (no ffmpeg pile-up)
    lock = _snap_lock.setdefault(node, threading.Lock())
    if not lock.acquire(blocking=False):
        return cached
    try:
        ts, cached = _snap_cache.get(node, (0, None))
        if cached and time.time() - ts < 0.8:
            return cached
        p = urlparse(url)
        internal = "rtsp://127.0.0.1:%s%s" % (p.port or 8554, p.path)
        out = subprocess.run(
            ["ffmpeg", "-nostdin", "-rtsp_transport", "tcp", "-i", internal,
             "-frames:v", "1", "-q:v", "6", "-f", "mjpeg", "pipe:1"],
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
        path = urlparse(self.path).path.rstrip("/")
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
        path = urlparse(self.path).path.rstrip("/")
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"error": "bad body"}))
        if path.endswith("/api/cmd"):
            node, suffix = str(body.get("node", "")), str(body.get("suffix", ""))
            if not node or suffix not in ALLOWED_CMDS or _client is None:
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
function openRobot(n){SEL=n; render(true)}
function goBack(){SEL=null; render(true)}

function listView(){
  if(!ROBOTS.length) return `<div class="empty">Waiting for robots… make sure the add-on is running.</div>`;
  return `<div class="list">`+ROBOTS.map(r=>`
    <div class="rowitem" onclick="openRobot('${r.node}')">
      <img class="thumb prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25">
      <div>
        <div class="ri-name"><span id="dot-${r.node}" class="dot ${r.online?'on':''}"></span>${esc(r.name||r.node)}</div>
        <div id="meta-${r.node}" class="ri-meta">${meta(r)}</div>
      </div><div class="chev">›</div>
    </div>`).join('')+`</div>`;
}
function detailView(r){
  const st=r.state||{}, cam=(r.camera==='on');
  return `<div class="detail">
    <img class="big prev" data-node="${r.node}" src="${B}/api/snapshot?node=${encodeURIComponent(r.node)}&t=${Date.now()}" onerror="this.style.opacity=.25">
    <div class="dname"><span id="d-dot" class="dot ${r.online?'on':''}"></span>${esc(r.name||r.node)}</div>
    <div id="d-meta" class="dmeta">${r.model||'EBO'} · SN ${esc(r.sn)||'—'} · 🔋 ${st.battery??'—'}% · 📶 ${st.wifi??'—'}</div>
    <div class="row">
      <button id="d-cam" class="btn ${cam?'pri':''}" onclick="cmd('${r.node}','camera/set','${cam?'off':'on'}')">${cam?'Camera ON':'Camera OFF'}</button>
      <button class="btn" onclick="cmd('${r.node}','wake','')">☀ Wake</button>
      <button class="btn" onclick="cmd('${r.node}','sleep/set','on')">🌙 Standby</button>
      <button class="btn" onclick="cmd('${r.node}','laser/set','on')">Laser</button>
      <button class="btn" onclick="cmd('${r.node}','dock','')">Dock</button>
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
previewLoop(); setInterval(previewLoop, 900);
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
