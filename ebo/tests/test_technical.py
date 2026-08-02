"""Technical/structural tests: everything compiles, every MQTT-discovery config is
well-formed and its command topic is subscribed, config files are valid and consistent."""
import glob
import os
import py_compile

import yaml

HERE = os.path.dirname(__file__)
ADDON = os.path.dirname(HERE)
N = "ebo"


def test_all_python_compiles():
    for path in glob.glob(os.path.join(ADDON, "*.py")):
        py_compile.compile(path, doraise=True)


def _topic_matches(sub, topic):
    """MQTT topic match supporting + and # wildcards."""
    s, t = sub.split("/"), topic.split("/")
    for i, seg in enumerate(s):
        if seg == "#":
            return True
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(s) == len(t)


def test_discovery_configs_valid(bridge):
    bridge._publish_discovery(bridge.mqtt)
    configs = bridge.mqtt.disc_configs()
    subs = bridge.mqtt.subscribed
    assert len(configs) >= 30, "expected the full entity set, got %d" % len(configs)

    uids = []
    problems = []
    for topic, cfg in configs.items():
        for req in ("unique_id", "availability_topic", "device"):
            if req not in cfg:
                problems.append("%s missing %s" % (topic, req))
        uids.append(cfg.get("unique_id"))
        # a command entity must have its command topic actually subscribed
        ct = cfg.get("command_topic")
        if ct and not any(_topic_matches(s, ct) for s in subs):
            problems.append("%s command_topic %s not subscribed" % (topic, ct))
        # selects must offer options
        if "/select/" in topic and not cfg.get("options"):
            problems.append("%s select without options" % topic)
        # numbers must have bounds
        if "/number/" in topic and not ("min" in cfg and "max" in cfg):
            problems.append("%s number without min/max" % topic)
    assert not problems, problems
    assert len(uids) == len(set(uids)), "duplicate unique_id: %s" % uids


def test_every_subscribe_is_reachable(bridge):
    """Every subscribed command topic should be handled (no dead subscriptions)."""
    bridge._publish_discovery(bridge.mqtt)
    # feed a benign payload to each concrete (non-wildcard) subscribed topic; must not raise
    for sub in bridge.mqtt.subscribed:
        if "+" in sub or "#" in sub:
            continue
        bridge._on_mqtt_message(None, None,
                                type("M", (), {"topic": sub, "payload": b"0"}))


def test_config_yaml_structure_and_version():
    cfg = yaml.safe_load(open(os.path.join(ADDON, "config.yaml"), encoding="utf-8"))
    for key in ("name", "slug", "version", "arch", "options", "schema", "startup"):
        assert key in cfg, "config.yaml missing %s" % key
    ver_txt = open(os.path.join(ADDON, "VERSION.txt"), encoding="utf-8").read().strip()
    assert cfg["version"] == ver_txt, "config.yaml %s != VERSION.txt %s" % (
        cfg["version"], ver_txt)
    assert cfg["slug"] == N
    assert "amd64" in cfg["arch"]      # Agora SDK is amd64-only


def test_base_image_pinned_and_glibc():
    """build.yaml is deprecated: the base image now lives in the Dockerfile. It MUST stay a
    Debian/glibc python — the Agora SDK ships glibc .so files and won't run on alpine/musl."""
    df = open(os.path.join(ADDON, "Dockerfile"), encoding="utf-8").read()
    froms = [ln for ln in df.splitlines() if ln.strip().upper().startswith("FROM ")]
    assert froms, "no FROM in the Dockerfile"
    assert "python:3.11-slim" in froms[0], froms[0]
    assert all("alpine" not in f.lower() for f in froms)


def test_opcodes_are_ints(B_mod):
    ops = [v for k, v in vars(B_mod).items() if k.startswith("OP_") or k.startswith("RESP_")]
    assert ops and all(isinstance(v, int) for v in ops)


def test_value_maps_have_unique_ints(B_mod):
    for name in ("VIDEO_QUALITY_MAP", "IMAGE_STYLE_MAP", "NIGHT_MODE_MAP",
                 "MOVE_MODE_MAP", "STEERING_MAP"):
        m = getattr(B_mod, name)
        assert len(set(m.values())) == len(m), "%s has duplicate ints" % name
        assert all(isinstance(v, int) for v in m.values())


# ---- the panel's JavaScript is a Python string: nothing type-checks it ----
def _panel_js():
    import re
    s = open(os.path.join(ADDON, "panel.py"), encoding="utf-8").read()
    js = max(re.findall(r"<script>(.*?)</script>", s, re.S), key=len)
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)          # comments and string/template literals
    js = re.sub(r"(?m)//.*$", "", js)                       # would otherwise look like calls
    js = re.sub(r"`(?:\\.|[^`\\])*`", "''", js, flags=re.S)
    js = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", js)
    js = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', js)
    return js


def test_panel_js_has_no_undefined_function_calls():
    """A call to a function that was never written is a silent runtime break: the panel keeps
    working until that code path runs. This shipped once — askSwitchToHls was called by the video
    player and didn't exist, so the HLS fallback died with a ReferenceError."""
    import re
    js = _panel_js()
    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", js))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", js))
    defined |= set(re.findall(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?[A-Za-z_$][\w$]*\s*=>", js))
    called = {m.group(1) for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", js)}
    keywords = {"if", "for", "while", "switch", "catch", "return", "function", "typeof", "new",
                "await", "do", "else", "delete", "void", "in", "of", "case", "yield", "async",
                "instanceof", "throw"}
    # browser/JS globals + names that are only ever locals (parameters, promise callbacks)
    allowed = {"Promise", "setTimeout", "setInterval", "clearTimeout", "clearInterval", "parseInt",
               "parseFloat", "String", "Number", "Boolean", "Array", "Object", "JSON", "Math",
               "Date", "fetch", "encodeURIComponent", "decodeURIComponent", "Image", "URL",
               "Error", "RTCPeerConnection", "Hls", "MediaStream", "isNaN",
               "requestAnimationFrame", "cancelAnimationFrame", "alert", "confirm", "prompt",
               "Set", "Map", "AudioContext", "webkitAudioContext", "Uint8Array", "Float32Array",
               "btoa", "atob", "structuredClone", "queueMicrotask", "FormData", "Blob", "Event",
               "CustomEvent", "AbortController", "WebSocket", "matchMedia", "getComputedStyle",
               "TextDecoder", "TextEncoder", "RegExp", "Symbol", "BigInt", "Intl",
               "C", "badge", "open", "status", "res", "resolve", "finish"}
    missing = sorted(called - defined - keywords - allowed)
    assert not missing, "panel JS calls undefined function(s): %s" % missing
