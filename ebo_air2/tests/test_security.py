"""Security tests: signing correctness, robust input handling (no crash / no garbage
sent on hostile payloads), no leaked user secrets, secure config."""
import glob
import os

import yaml
import pytest

from conftest import deliver

HERE = os.path.dirname(__file__)
ADDON = os.path.dirname(HERE)
N = "ebo_air2"

# files that actually get published / shipped in the image
PUBLISHABLE = [os.path.join(ADDON, f) for f in (
    "ebo_bridge.py", "ebo_cloud.py", "ebo_sign.py", "ebo_video.py", "ebo_log.py",
    "run.sh", "config.yaml", "build.yaml", "Dockerfile", "CHANGELOG.md",
    "DOCS.md", "README.md",
)]


# ---- request signing is deterministic and driven by the user-supplied key ----
def test_signature_deterministic(monkeypatch):
    monkeypatch.setenv("EBO_SIGN_KEY", "test-key")
    import importlib
    import ebo_sign
    importlib.reload(ebo_sign)
    a = ebo_sign.sign("GET", "/api/v1/ebox/robots/robot", "", b"", ts=1700000000, nonce="EXAMPLE1")
    b = ebo_sign.sign("GET", "/api/v1/ebox/robots/robot", "", b"", ts=1700000000, nonce="EXAMPLE1")
    assert a["x-ebo-sign"] == b["x-ebo-sign"]        # deterministic for fixed inputs
    assert a["x-ebo-sign-version"] == "2"
    monkeypatch.delenv("EBO_SIGN_KEY")
    importlib.reload(ebo_sign)


def test_signing_key_from_env(monkeypatch):
    # the key is supplied by the user via EBO_SIGN_KEY — it is NOT shipped with the code
    monkeypatch.setenv("EBO_SIGN_KEY", "custom")
    import importlib
    import ebo_sign
    importlib.reload(ebo_sign)
    assert ebo_sign.SIGN_KEY == b"custom"
    monkeypatch.delenv("EBO_SIGN_KEY")
    importlib.reload(ebo_sign)


# ---- hostile / malformed MQTT payloads must not crash and must not send garbage ----
HOSTILE = [
    ("speed/set", "abc"),                 # non-numeric -> ValueError
    ("speed/set", ""),                    # empty
    ("rotate/set", "NaNaN"),
    ("volume/set", "1e999999"),           # absurd number
    ("joystick", "{not json"),
    ("move/vector", "[]"),                # wrong JSON shape
    ("cmd", "not json at all"),
    ("cmd", '{"noid": 1}'),               # missing id
    ("cmd", '{"id": "DROP TABLE"}'),      # non-int id (injection attempt)
    ("cmd", '{"id": null}'),
    ("video_quality/set", "'; rm -rf /"), # shell-y string into a mapped select
    ("say", "\x00\x01\x02"),              # control bytes
]


@pytest.mark.parametrize("topic,payload", HOSTILE)
def test_hostile_payload_no_crash_no_garbage(bridge, topic, payload):
    before = len(bridge.sent)
    deliver(bridge, "%s/%s" % (N, topic), payload)   # must not raise
    # any command it *did* send must be well-formed (opcode int, data dict-or-None)
    for op, data in bridge.sent[before:]:
        assert isinstance(op, int)
        assert data is None or isinstance(data, dict)


def test_raw_cmd_rejects_non_int_id(bridge):
    deliver(bridge, "%s/cmd" % N, '{"id": "evil", "data": {}}')
    assert bridge.sent == []                       # nothing sent
    deliver(bridge, "%s/cmd" % N, '{"id": 103501, "data": {"text": "ok"}}')
    assert bridge.sent == [(103501, {"text": "ok"})]


def test_video_quality_unknown_falls_back_safely(bridge):
    deliver(bridge, "%s/video_quality/set" % N, "'; rm -rf /")
    # unknown option maps to the safe default (2 = Medium), never crashes
    assert bridge.sent[-1] == (102055, {"videoQuality": 2})


# ---- no user secrets leaked into shipped files ----
# precise patterns that match actual leaked VALUES, not code or public links.
# (the Buy-Me-a-Coffee slug "scattolacom" is a public link, intentional — not a secret;
#  `sessionid={...}` / `EBO_PASSWORD="$(...)"` are code, not values.)
import re  # noqa: E402

_SECRET_PATTERNS = [
    (r"[\w.+-]+@me\.scattola\.com", "user personal email"),
    (r"sessionid=[A-Za-z0-9]{12,}", "captured session cookie value"),
    (r"EBO_PASSWORD\s*=\s*['\"][^\"'$)]", "hardcoded password literal"),
    (r"EBO_EMAIL\s*=\s*['\"][^\"'$)]", "hardcoded email literal"),
]


def test_no_user_credentials_in_shipped_files():
    leaked = []
    for path in PUBLISHABLE:
        if not os.path.exists(path):
            continue
        txt = open(path, encoding="utf-8", errors="replace").read()
        for pat, desc in _SECRET_PATTERNS:
            if re.search(pat, txt):
                leaked.append("%s: %s" % (os.path.basename(path), desc))
    assert not leaked, leaked


def test_no_secrets_file_shipped():
    # a .env / secrets file must never sit inside the published add-on dir
    for pat in ("*.env", "secrets*", "*session*.json"):
        found = glob.glob(os.path.join(ADDON, pat))
        assert not found, "secret-like file in add-on dir: %s" % found


def test_config_defaults_have_no_credentials():
    cfg = yaml.safe_load(open(os.path.join(ADDON, "config.yaml"), encoding="utf-8"))
    assert cfg["options"].get("email", "") == ""
    assert cfg["options"].get("password", "") == ""


def test_password_field_is_masked():
    cfg = yaml.safe_load(open(os.path.join(ADDON, "config.yaml"), encoding="utf-8"))
    assert cfg["schema"]["password"] == "password"   # masked input, not plain str
