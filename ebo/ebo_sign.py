"""
ebo_sign.py — request signing for interoperability with the Enabot cloud API (x-ebo-sign v2).

    x-ebo-sign = base64( HMAC_SHA256( KEY, canonical ) )
    canonical  = METHOD & PATH & QUERY & "x-ebo-app-type=2&x-ebo-sign-nonce=<n>&"
                 "x-ebo-sign-timestamp=<ts>&x-ebo-sign-version=2&" [ + sha256hex(body) if there is a body ]

KEY must be provided via the EBO_SIGN_KEY environment variable (not shipped with the code).
"""
import base64
import hashlib
import hmac
import os
import time

SIGN_KEY = os.environ.get("EBO_SIGN_KEY", "").encode()   # supplied by the user, not shipped
APP_TYPE = os.environ.get("EBO_APP_TYPE", "2")
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _nonce(n=8):
    # alphanumeric nonce; use secrets (CSPRNG) so it's clean under security scanners
    import secrets
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


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
    # self-consistency: the canonical string is stable for fixed inputs
    if not SIGN_KEY:
        raise SystemExit("set EBO_SIGN_KEY to run this")
    h = sign("GET", "/api/v1/ebox/robots/robot", "", b"", ts=1700000000, nonce="EXAMPLE1")
    print("x-ebo-sign:", h["x-ebo-sign"])
