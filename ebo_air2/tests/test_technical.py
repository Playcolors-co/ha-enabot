"""Technical/structural tests: everything compiles, every MQTT-discovery config is
well-formed and its command topic is subscribed, config files are valid and consistent."""
import glob
import os
import py_compile

import yaml

HERE = os.path.dirname(__file__)
ADDON = os.path.dirname(HERE)
N = "ebo_air2"


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


def test_build_yaml_valid():
    b = yaml.safe_load(open(os.path.join(ADDON, "build.yaml"), encoding="utf-8"))
    assert "build_from" in b and "amd64" in b["build_from"]


def test_opcodes_are_ints(B_mod):
    ops = [v for k, v in vars(B_mod).items() if k.startswith("OP_") or k.startswith("RESP_")]
    assert ops and all(isinstance(v, int) for v in ops)


def test_value_maps_have_unique_ints(B_mod):
    for name in ("VIDEO_QUALITY_MAP", "IMAGE_STYLE_MAP", "SHOOT_MODE_MAP",
                 "MOVE_MODE_MAP", "EYES_MODE_MAP"):
        m = getattr(B_mod, name)
        assert len(set(m.values())) == len(m), "%s has duplicate ints" % name
        assert all(isinstance(v, int) for v in m.values())
