"""
ebo_cloud.py — standalone Enabot cloud (ecp/ebox) client, pure Python library.

Reconstructed via reverse engineering. Signs requests with ebo_sign (x-ebo-sign v2,
verified) and authenticates with a `sessionid` cookie (from email+password login).

Known endpoints (regional host, e.g. ebox-eu.enabotserverintl.com):
  POST /api/v2/users/login         {encrypted}               -> Set-Cookie: sessionid
  GET  /api/v1/ebox/robots/robot                             -> robot list (robot_id, agora_info, ...)
  POST /api/v1/ebox/robots/session {robot_id}                -> Agora session (app_rtc_token, app_rtm_token, rtc_channel, sid)
"""
import json
import http.cookiejar
import urllib.request

import base64, os as _os, secrets as _secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import ebo_sign

# AES-128-GCM key for the login payload (app-level constant, extracted via a Cipher hook)
_PAYLOAD_KEY = (_os.environ.get("EBO_PAYLOAD_KEY", "&1V@!H8*82hi4gzH")).encode()

def _enc(obj):
    iv = _secrets.token_bytes(16)
    pt = __import__("json").dumps(obj, separators=(",", ":")).encode()
    ct = AESGCM(_PAYLOAD_KEY).encrypt(iv, pt, None)
    return base64.b64encode(iv + ct).decode()

def _dec(b64):
    raw = base64.b64decode(b64)
    return __import__("json").loads(AESGCM(_PAYLOAD_KEY).decrypt(raw[:16], raw[16:], None))



class EboCloud:
    def __init__(self, host="ebox-eu.enabotserverintl.com", sessionid=None):
        self.host = host
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self._sessionid = sessionid

    def _req(self, method, path, query="", body_obj=None):
        body = json.dumps(body_obj, separators=(",", ":")).encode() if body_obj is not None else b""
        url = f"https://{self.host}{path}" + (("?" + query) if query else "")
        req = urllib.request.Request(url, data=body if body else None, method=method)
        req.add_header("User-Agent", "okhttp/4.12.0")
        if body:
            req.add_header("Content-Type", "application/json;charset=utf-8")
        if self._sessionid:
            req.add_header("Cookie", f"sessionid={self._sessionid}")
        for k, v in ebo_sign.sign(method, path, query, body).items():
            req.add_header(k, v)
        with self.opener.open(req, timeout=15) as r:
            data = json.loads(r.read())
        # update sessionid from any Set-Cookie
        for c in self.cj:
            if c.name == "sessionid":
                self._sessionid = c.value
        return data

    # --- API ---
    def login(self, email, password, region="GB", device_id=None, app_token=""):
        """Email+password login. The payload is AES-128-GCM encrypted (e_ver 1.0).
        Sets the sessionid cookie from the response."""
        device_id = device_id or ("Android" + _secrets.token_urlsafe(16))
        payload = {
            "app_token": app_token, "app_kind": "Android", "language": "en",
            "device_id": device_id, "account": email, "password": password,
            "login_region": region,
        }
        body_obj = {"app_type": 2, "data": _enc(payload), "e_ver": "1.0"}
        out = self._req("POST", "/api/v2/users/login", body_obj=body_obj)
        # the response is encrypted; decrypt it to read the outcome
        if isinstance(out.get("data"), str):
            out = {"app_type": out.get("app_type"), **_dec(out["data"])}
        return out

    def robots(self):
        return self._req("GET", "/api/v1/ebox/robots/robot")

    def robot_session(self, robot_id: int):
        """Return a fresh Agora session for the robot."""
        return self._req("POST", "/api/v1/ebox/robots/session", body_obj={"robot_id": robot_id})

    @property
    def sessionid(self):
        return self._sessionid


def build_bridge_session(sessionid: str, robot_id: int, app_id: str,
                         host="ebox-eu.enabotserverintl.com") -> dict:
    """Call the cloud and produce the session.json dict the bridge expects."""
    c = EboCloud(host=host, sessionid=sessionid)
    d = c.robot_session(robot_id)["data"]
    import time
    return {
        "app_id": app_id,
        "rtm_user": d["app_rtm_uid"],
        "rtm_token": d["app_rtm_token"],
        "rtc_uid": str(d["app_rtc_uid"]),
        "rtc_token": d["app_rtc_token"],
        "rtc_channel": d["rtc_channel"],
        "robot_rtm": d.get("robot_rtm_uid", ""),
        "robot_rtc_uid": str(d.get("robot_rtc_uid", "")),
        "sid": d.get("sid"),
        "captured_at": int(time.time()),
    }


def build_bridge_session_from(client: "EboCloud", robot_id: int, app_id: str) -> dict:
    """Like build_bridge_session but with an already-authenticated EboCloud client."""
    import time
    d = client.robot_session(robot_id)["data"]
    return {
        "app_id": app_id, "rtm_user": d["app_rtm_uid"], "rtm_token": d["app_rtm_token"],
        "rtc_uid": str(d["app_rtc_uid"]), "rtc_token": d["app_rtc_token"],
        "rtc_channel": d["rtc_channel"], "robot_rtm": d.get("robot_rtm_uid", ""),
        "robot_rtc_uid": str(d.get("robot_rtc_uid", "")), "sid": d.get("sid"),
        "captured_at": int(time.time()),
    }
