#!/usr/bin/env bash
# Add-on entrypoint: reads user config from /data/options.json and the MQTT broker
# credentials from the Supervisor (mqtt:need service), then starts the bridge.
set -e

OPTS=/data/options.json

# --- standalone (no Supervisor): synthesize /data/options.json from the environment ---
# Lets the same image run as a plain Docker container (HA Container/Core users): pass
# EBO_EMAIL/EBO_PASSWORD/EBO_PAYLOAD_KEY/EBO_SIGN_KEY (+ optional EBO_EXPOSE_MQTT) as env and
# skip the options file. When Supervisor provides the file, this block is a no-op.
if [ ! -f "$OPTS" ]; then
  mkdir -p "$(dirname "$OPTS")"
  case "${EBO_EXPOSE_MQTT:-false}" in 1|true|on|yes) _MQ=true;; *) _MQ=false;; esac
  jq -n --arg e "${EBO_EMAIL:-}" --arg p "${EBO_PASSWORD:-}" \
        --arg pk "${EBO_PAYLOAD_KEY:-}" --arg sk "${EBO_SIGN_KEY:-}" --argjson mq "$_MQ" \
        '{email:$e,password:$p,payload_key:$pk,sign_key:$sk,expose_mqtt:$mq}' > "$OPTS"
  echo "[standalone] synthesized $OPTS from the environment"
fi

# --- account login + the app crypto keys (supplied by you, NOT shipped in the public code) ---
export EBO_EMAIL="$(jq -r '.email // empty' "$OPTS")"
export EBO_PASSWORD="$(jq -r '.password // empty' "$OPTS")"
export EBO_PAYLOAD_KEY="$(jq -r '.payload_key // empty' "$OPTS")"
export EBO_SIGN_KEY="$(jq -r '.sign_key // empty' "$OPTS")"

# --- account/connection + audio/video processing options all live in the add-on Configuration tab
# (/data/options.json). The old in-panel Settings (/data/panel.json) is kept only as a one-time
# migration fallback for users who had set these there. Precedence: options.json -> panel.json ->
# built-in default. NB: use `has($k)` (not `// empty`) so a `false` boolean isn't dropped. ---
PANEL_CFG=/data/panel.json
pget() {  # pget <key> <default>
  local v=""
  v="$(jq -r --arg k "$1" 'if has($k) then .[$k] else empty end' "$OPTS" 2>/dev/null || true)"
  [ -z "$v" ] && [ -f "$PANEL_CFG" ] && v="$(jq -r --arg k "$1" 'if has($k) then .[$k] else empty end' "$PANEL_CFG" 2>/dev/null || true)"
  [ -z "$v" ] && v="$2"
  printf '%s' "$v"
}
pbool() { [ "$(pget "$1" "$2")" = "true" ] && echo 1 || echo 0; }

export EBO_REGION="$(pget region GB)"
export EBO_HOST="$(pget host ebox-eu.enabotserverintl.com)"
export EBO_VIDEO="$(pbool video true)"
export EBO_AUDIO="$(pbool audio true)"
export EBO_TALK="$(pbool talk false)"
export EBO_AUDIO_PT="$(pget audio_codec 8)"
# log level: the Configuration-tab option wins; fall back to the panel setting, then 'info'.
EBO_LOG_LEVEL="$(jq -r '.log_level // empty' "$OPTS" 2>/dev/null)"
[ -z "$EBO_LOG_LEVEL" ] && EBO_LOG_LEVEL="$(pget log_level info)"
export EBO_LOG_LEVEL
export EBO_VIDEO_MAX_HEIGHT="$(pget video_max_height 720)"
export EBO_VIDEO_FPS="$(pget video_fps 20)"
export EBO_VIDEO_BITRATE="$(pget video_bitrate 2500)"
export EBO_VIDEO_PRESET="$(pget video_preset ultrafast)"
# Native-only: never publish HA entity discovery. The internal MQTT bus (localhost) is just glue
# between the bridges and the panel; Home Assistant gets everything from the companion integration.
export EBO_EXPOSE_MQTT=0
ROBOT_ID="$(pget robot_id 0)"
[ "$ROBOT_ID" != "0" ] && export EBO_ROBOT_ID="$ROBOT_ID"

# API token for the native integration to read the panel's data API (persisted in /data)
# Prefer a token pinned via env (standalone) or the add-on option; else reuse the persisted one;
# else generate. When on Supervisor and the option is empty, persist it back to the add-on options
# so the companion integration can read it (via the Supervisor) and reach the data API — no MQTT.
OPT_TOKEN="$(jq -r '.api_token // empty' "$OPTS" 2>/dev/null)"
if [ -n "${EBO_API_TOKEN:-}" ]; then
  echo "$EBO_API_TOKEN" > /data/api_token 2>/dev/null || true
elif [ -n "$OPT_TOKEN" ]; then
  echo "$OPT_TOKEN" > /data/api_token 2>/dev/null || true
elif [ ! -f /data/api_token ]; then
  head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' > /data/api_token 2>/dev/null || true
fi
export EBO_API_TOKEN="$(cat /data/api_token 2>/dev/null || echo "${EBO_API_TOKEN:-}")"
if [ -z "$OPT_TOKEN" ] && [ -n "${SUPERVISOR_TOKEN:-}" ] && [ -n "$EBO_API_TOKEN" ]; then
  # Merge: send the FULL options (Supervisor replaces the whole block) so we don't wipe the login.
  MERGED="$(jq -n --arg e "$EBO_EMAIL" --arg p "$EBO_PASSWORD" --arg pk "$EBO_PAYLOAD_KEY" \
                  --arg sk "$EBO_SIGN_KEY" --arg t "$EBO_API_TOKEN" \
                  '{options:{email:$e,password:$p,payload_key:$pk,sign_key:$sk,api_token:$t}}')"
  curl -sf -X POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
    -d "$MERGED" http://supervisor/addons/self/options >/dev/null 2>&1 \
    && echo "[add-on] persisted api_token to options (readable by the integration)" || true
fi
export EBO_API_PORT="${EBO_API_PORT:-8098}"
# Home Assistant core reaches the add-on's API over the internal Supervisor network by hostname
# (works regardless of LAN/VLAN firewalls, unlike the host IP). Fall back to the container IP.
export EBO_API_HOST="$(hostname 2>/dev/null || hostname -i 2>/dev/null || echo '')"

# seed the panel store once from the resolved (migrated) values, so the panel has a file to edit
if [ ! -f "$PANEL_CFG" ]; then
  printf '{"region":"%s","host":"%s","robot_id":%s,"expose_mqtt":true,"video":%s,"audio":%s,"talk":%s,"audio_codec":%s,"log_level":"%s","video_max_height":%s,"video_fps":%s,"video_bitrate":%s,"video_preset":"%s"}\n' \
    "$EBO_REGION" "$EBO_HOST" "$ROBOT_ID" \
    "$([ "$EBO_VIDEO" = 1 ] && echo true || echo false)" \
    "$([ "$EBO_AUDIO" = 1 ] && echo true || echo false)" \
    "$([ "$EBO_TALK" = 1 ] && echo true || echo false)" \
    "$EBO_AUDIO_PT" "$EBO_LOG_LEVEL" "$EBO_VIDEO_MAX_HEIGHT" "$EBO_VIDEO_FPS" "$EBO_VIDEO_BITRATE" \
    "$EBO_VIDEO_PRESET" > "$PANEL_CFG" 2>/dev/null || true
fi

if [ -z "$EBO_EMAIL" ] || [ -z "$EBO_PASSWORD" ]; then
  echo "[add-on] ERROR: set email and password in the add-on configuration."
  exit 1
fi
if [ -z "$EBO_PAYLOAD_KEY" ] || [ -z "$EBO_SIGN_KEY" ]; then
  echo "[add-on] ERROR: the app crypto keys are not shipped with this add-on. Set 'payload_key'"
  echo "[add-on] and 'sign_key' in the configuration (the values for the EBO HOME app)."
  exit 1
fi

# --- internal MQTT bus: a mosquitto bound to localhost, private to this container ---
# It's just the glue between the bridge processes and the panel. Home Assistant never sees it
# (no external broker, no MQTT integration needed). Overridable by env for standalone/testing.
if [ -z "${EBO_MQTT_HOST:-}" ] && command -v mosquitto >/dev/null 2>&1; then
  printf 'listener 1883 127.0.0.1\nallow_anonymous true\npersistence false\n' > /data/mosquitto.conf
  mosquitto -c /data/mosquitto.conf -d 2>/dev/null || mosquitto -d 2>/dev/null || true
  for _ in $(seq 1 20); do
    (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null && { exec 3>&- 3<&-; break; }
    sleep 0.2
  done
  echo "[add-on] internal MQTT bus on 127.0.0.1:1883 (private to the add-on)"
fi
: "${EBO_MQTT_HOST:=127.0.0.1}"
: "${EBO_MQTT_PORT:=1883}"
export EBO_MQTT_HOST EBO_MQTT_PORT

# Home Assistant host IP for the RTSP camera URL: use the manual option if set, else ask
# the Supervisor for the primary interface address.
EBO_HOST_IP="$(pget host_ip "")"
if [ -z "$EBO_HOST_IP" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
  NET_JSON="$(curl -sf -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/network/info 2>/dev/null || true)"
  EBO_HOST_IP="$(echo "$NET_JSON" | jq -r 'first((.data.interfaces[]? | select(.primary==true) | .ipv4.address[0]) // empty) // (.data.interfaces[]? | select(.enabled==true) | .ipv4.address[0])' 2>/dev/null | sed 's#/.*##' | head -1)"
fi
export EBO_HOST_IP
if [ -n "$EBO_HOST_IP" ]; then
  echo "[add-on] host IP for camera URL: ${EBO_HOST_IP}"
else
  echo "[add-on] could not detect host IP — set 'host_ip' in the add-on config for the camera URL"
fi

# ALL the host's LAN IPv4s (comma-separated): mediamtx advertises these as WebRTC ICE candidates so
# the browser (which may reach HA on a different NIC/VLAN than the robot's) can connect to the
# panel's fluid WebRTC drive video. We don't know in advance which IP the browser uses, so we offer
# them all and ICE picks the reachable one.
EBO_HOST_IPS="$(pget host_ip "")"
if [ -n "${NET_JSON:-}" ]; then
  EBO_HOST_IPS="$(echo "$NET_JSON" | jq -r '[.data.interfaces[]? | select(.enabled==true) | .ipv4.address[]?] | join(",")' 2>/dev/null | sed 's#/[0-9]*##g')"
fi
[ -z "$EBO_HOST_IPS" ] && EBO_HOST_IPS="$EBO_HOST_IP"
export EBO_HOST_IPS
echo "[add-on] host IPs advertised for WebRTC ICE: ${EBO_HOST_IPS:-<none>}"

# Log the version actually running (baked into the image) vs what the Supervisor thinks is
# installed. If they differ, the image wasn't rebuilt on update (stale) — that's the real bug.
CODE_VER="$(cat /app/VERSION.txt 2>/dev/null || echo '?')"
INST_VER="?"
if [ -n "$SUPERVISOR_TOKEN" ]; then
  INST_VER="$(curl -sf -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/addons/self/info 2>/dev/null | jq -r '.data.version // "?"')"
fi
if [ "$CODE_VER" = "$INST_VER" ]; then
  echo "[add-on] version ${CODE_VER} (running code matches installed)"
else
  echo "[add-on] ⚠ version MISMATCH: running code=${CODE_VER}, Supervisor installed=${INST_VER} — the image was NOT rebuilt (stale). Try: uninstall + reinstall the add-on."
fi

# --- install the companion integration into Home Assistant (no HACS) ---
# The image bundles it; copy it into <ha-config>/custom_components/ebo so HA can load it. A new
# custom component needs ONE Home Assistant restart to be picked up (logged for the user).
# The config dir is mounted at /homeassistant (homeassistant_config map) or /config on older setups.
HA_ROOT=""
for d in /homeassistant /config; do [ -d "$d" ] && { HA_ROOT="$d"; break; }; done
HA_CC="$HA_ROOT/custom_components"
if [ -d /app/ha_integration/custom_components/ebo ] && [ -n "$HA_ROOT" ]; then
  mkdir -p "$HA_CC"
  if cp -r /app/ha_integration/custom_components/ebo "$HA_CC/ebo.tmp" 2>/dev/null; then
    rm -rf "$HA_CC/ebo" && mv "$HA_CC/ebo.tmp" "$HA_CC/ebo"
    echo "[add-on] installed the EBO integration into ${HA_CC}/ebo"
    echo "[add-on] → FIRST TIME: restart Home Assistant once, then Settings → Devices & Services"
    echo "[add-on]   → + Add Integration → 'EBO' (it finds your robots automatically, nothing to type)."
  fi
fi

echo "[add-on] starting Enabot integration bridge (region ${EBO_REGION})"

# --- which robot(s) to run: a specific robot_id, or discover every robot on the account ---
RIDS=(); RNAMES=()
if [ -n "$EBO_ROBOT_ID" ]; then
  RIDS=("$EBO_ROBOT_ID"); RNAMES=("EBO Air 2")
else
  DISC="$(EBO_DISCOVER=1 python /app/ebo_bridge.py 2>/dev/null)"
  while IFS=$'\t' read -r tag id name; do
    [ "$tag" = "ROBOT" ] && [ -n "$id" ] && { RIDS+=("$id"); RNAMES+=("$name"); }
  done <<< "$DISC"
fi
NR=${#RIDS[@]}
if [ "$NR" -eq 0 ]; then
  # discovery failed (network/creds): fall back to a single default bridge (picks 1st robot)
  RIDS=(""); RNAMES=("EBO Air 2"); NR=1
fi
[ "$NR" -gt 1 ] && echo "[add-on] ${NR} robots on the account — running one bridge each"

stopping=0
term() {
  stopping=1
  echo "[add-on] stopping…"
  pkill -TERM -f '/app/panel.py' 2>/dev/null || true
  pkill -TERM -f '/app/ebo_mcp.py' 2>/dev/null || true
  pkill -TERM -f '/app/ebo_bridge.py' 2>/dev/null || true
  for _ in $(seq 1 16); do
    pgrep -f '/app/ebo_bridge.py' >/dev/null 2>&1 || break
    sleep 0.5
  done
  exit 0
}
trap term SIGTERM SIGINT

# Supervise ONE robot: restart on exit; after repeated quick crashes with A/V on, fall back
# to control-only for that robot (control and video share one Agora connection).
run_robot() {
  local id="$1" idx="$2" name="$3" crashes=0 v="$EBO_VIDEO" a="$EBO_AUDIO"
  while [ "$stopping" -eq 0 ]; do
    local start; start=$(date +%s)
    (
      export EBO_VIDEO="$v" EBO_AUDIO="$a"
      [ -n "$id" ] && export EBO_ROBOT_ID="$id"
      export EBO_DEVICE_NAME="$name"    # always use the robot's real (account) name
      if [ "$NR" -gt 1 ]; then          # distinct node/port/path only when there's more than one
        export EBO_NODE="ebo_${id}" EBO_RTSP_PATH="ebo_${id}" \
               EBO_RTSP_PORT="$((8554 + idx))"
      fi
      exec python /app/ebo_bridge.py
    ) &
    wait $!; local rc=$?
    [ "$stopping" -eq 1 ] && break
    local ran=$(( $(date +%s) - start ))
    if [ "$ran" -lt 60 ] && { [ "$rc" -ge 128 ] || [ "$rc" -ne 0 ]; }; then
      crashes=$(( crashes + 1 ))
    else
      crashes=0
    fi
    if [ "$crashes" -ge 2 ] && { [ "$v" != "0" ] || [ "$a" = "1" ]; }; then
      echo "[add-on] robot ${id:-single} crashed ${crashes}× with A/V — control only."
      v=0; a=0; crashes=0
    fi
    echo "[add-on] bridge (${id:-single}) exited (rc=${rc}), restarting in 15s…"
    sleep 15 & wait $!
  done
}

# Ingress web panel (one for the whole add-on): aggregates every robot's state over MQTT.
export EBO_PANEL_PORT="${EBO_PANEL_PORT:-8099}"
python /app/panel.py &
PANEL_PID=$!

# MCP server for AI agents (opt-in): only started when the 'mcp' option is ON, so it costs nothing
# when unused. Guarded by the add-on's api_token (Bearer). Non-fatal: if fastmcp isn't available or
# it fails to start, the add-on keeps working normally.
EBO_MCP="$(pget mcp false)"
if [ "$EBO_MCP" = "true" ]; then
  export EBO_MCP_PORT="${EBO_MCP_PORT:-8100}"
  if python -c 'import fastmcp' 2>/dev/null; then
    echo "[add-on] starting MCP server for AI agents on :${EBO_MCP_PORT} (token-guarded)"
    python /app/ebo_mcp.py &
  else
    echo "[add-on] MCP requested but 'fastmcp' isn't installed in this image — skipping"
  fi
fi

for i in "${!RIDS[@]}"; do
  run_robot "${RIDS[$i]}" "$i" "${RNAMES[$i]}" &
done
wait
