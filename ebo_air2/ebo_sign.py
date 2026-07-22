"""
ebo_sign.py — Enabot API request signing (x-ebo-sign v2), reconstructed via RE.

Verified by exactly reproducing signatures captured from the app.

    x-ebo-sign = base64( HMAC_SHA256( KEY, canonical ) )
    canonical  = METHOD & PATH & QUERY & "x-ebo-app-type=2&x-ebo-sign-nonce=<n>&"
                 "x-ebo-sign-timestamp=<ts>&x-ebo-sign-version=2&" [ + sha256hex(body) if there is a body ]

KEY is an app-level constant (extracted from a hook on javax.crypto.Mac).
"""
import base64
import hashlib
import hmac
import os
import time

SIGN_KEY = os.environ.get("EBO_SIGN_KEY", "Oxzf^!ss[dU-sD!9").encode()
APP_TYPE = os.environ.get("EBO_APP_TYPE", "2")
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _nonce(n=8):
    # alphanumeric nonce; does not need to be cryptographically strong
    import random
    return "".join(random.choice(_ALPHABET) for _ in range(n))


def sign(method: str, path: str, query: str = "", body: bytes = b"",
         ts: int | None = None, nonce: str | None = None):
    ts = ts if ts is not None else int(time.time())
    nonce = nonce if nonce is not None else _nonce()
    canonical = (
        f"{method}&{path}&{query}&"
        f"x-ebo-app-type={APP_TYPE}&x-ebo-sign-nonce={nonce}&"
        f"x-ebo-sign-timestamp={ts}&x-ebo-sign-version=2&"
    )
    if body:
        canonical += hashlib.sha256(body).hexdigest()
    sig = base64.b64encode(hmac.new(SIGN_KEY, canonical.encode(), hashlib.sha256).digest()).decode()
    return {
        "x-ebo-sign": sig,
        "x-ebo-sign-nonce": nonce,
        "x-ebo-sign-timestamp": str(ts),
        "x-ebo-sign-version": "2",
        "x-ebo-app-type": APP_TYPE,
        "x-platform": "Android",
    }


if __name__ == "__main__":
    # regression against captured signatures
    h = sign("GET", "/api/v1/ebox/robots/robot", "", b"",
             ts=1784577185, nonce="muSUKk2d")
    assert h["x-ebo-sign"] == "G7Vwr2513Jua/nnCof+3iJbV3XcadBz9EK50C6CQWjk=", h["x-ebo-sign"]
    b = b'{"ebo_id":"5ZEXGBH9","login_region":"GB","lang":"en"}'
    h2 = sign("POST", "/api/v1/data/activity/ns/latest", "", b,
              ts=1784577185, nonce="mrRM7IKT")
    assert h2["x-ebo-sign"] == "mtlqdQIz2lvGNDm9r7cdcRfazlmbHCu+qBKBgoK0NDA=", h2["x-ebo-sign"]
    print("x-ebo-sign signatures reproduced correctly (GET and POST)")
