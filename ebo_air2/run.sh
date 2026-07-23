#!/usr/bin/env bash
# Add-on entrypoint: reads user config from /data/options.json and the MQTT broker
# credentials from the Supervisor (mqtt:need service), then starts the bridge.
set -e

OPTS=/data/options.json

export EBO_EMAIL="$(jq -r '.email // empty' "$OPTS")"
export EBO_PASSWORD="$(jq -r '.password // empty' "$OPTS")"
export EBO_REGION="$(jq -r '.region // "GB"' "$OPTS")"
export EBO_HOST="$(jq -r '.host // "ebox-eu.enabotserverintl.com"' "$OPTS")"
export EBO_VIDEO="$(jq -r 'if .video==false then "0" else "1" end' "$OPTS")"
# experimental encoded-video path (may crash the SDK) — off unless explicitly enabled
export EBO_VIDEO_ENCODED="$(jq -r 'if .video_encoded==true then "1" else "0" end' "$OPTS")"
export EBO_AUDIO="$(jq -r 'if .audio==true then "1" else "0" end' "$OPTS")"
export EBO_LOG_LEVEL="$(jq -r '.log_level // "info"' "$OPTS")"
# video re-encode tuning: max height (0 = native) + libx264 preset
export EBO_VIDEO_MAX_HEIGHT="$(jq -r '.video_max_height // 720' "$OPTS")"
export EBO_VIDEO_PRESET="$(jq -r '.video_preset // "ultrafast"' "$OPTS")"
ROBOT_ID="$(jq -r '.robot_id // 0' "$OPTS")"
[ "$ROBOT_ID" != "0" ] && export EBO_ROBOT_ID="$ROBOT_ID"

if [ -z "$EBO_EMAIL" ] || [ -z "$EBO_PASSWORD" ]; then
  echo "[add-on] ERROR: set email and password in the add-on configuration."
  exit 1
fi

# --- MQTT from the Supervisor ---
if [ -n "$SUPERVISOR_TOKEN" ]; then
  MQTT_JSON="$(curl -sf -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/services/mqtt || true)"
  if [ -n "$MQTT_JSON" ]; then
    export EBO_MQTT_HOST="$(echo "$MQTT_JSON" | jq -r '.data.host')"
    export EBO_MQTT_PORT="$(echo "$MQTT_JSON" | jq -r '.data.port')"
    export EBO_MQTT_USER="$(echo "$MQTT_JSON" | jq -r '.data.username // empty')"
    export EBO_MQTT_PASS="$(echo "$MQTT_JSON" | jq -r '.data.password // empty')"
    echo "[add-on] MQTT from Supervisor: ${EBO_MQTT_HOST}:${EBO_MQTT_PORT}"
  fi
fi
: "${EBO_MQTT_HOST:=core-mosquitto}"
: "${EBO_MQTT_PORT:=1883}"
export EBO_MQTT_HOST EBO_MQTT_PORT

# Home Assistant host IP for the RTSP camera URL: use the manual option if set, else ask
# the Supervisor for the primary interface address.
EBO_HOST_IP="$(jq -r '.host_ip // empty' "$OPTS")"
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

echo "[add-on] starting Enabot integration bridge (region ${EBO_REGION})"

# Clean shutdown: when the Supervisor stops the add-on it sends SIGTERM. Forward it
# to the bridge, wait for it to exit, and stop WITHOUT restarting (otherwise the
# restart loop would sleep through the stop and get force-killed → "error").
child=""
stopping=0
term() {
  stopping=1
  echo "[add-on] stopping…"
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  # give the bridge up to ~8s to close cleanly, then move on
  for _ in $(seq 1 16); do
    kill -0 "$child" 2>/dev/null || break
    sleep 0.5
  done
  exit 0
}
trap term SIGTERM SIGINT

# Supervisor with a safety net: control and video share one Agora/RTC connection, so a
# native video crash takes the whole bridge down. If that keeps happening, fall back to
# control-only (video off) so the add-on never gets stuck in a crash loop.
crashes=0
while [ "$stopping" -eq 0 ]; do
  start=$(date +%s)
  python /app/ebo_bridge.py &
  child=$!
  wait "$child"
  rc=$?
  [ "$stopping" -eq 1 ] && break
  ran=$(( $(date +%s) - start ))

  # a quick exit (<60s) with a crash code counts as a crash; a long run resets the counter
  if [ "$ran" -lt 60 ] && { [ "$rc" -ge 128 ] || [ "$rc" -ne 0 ]; }; then
    crashes=$(( crashes + 1 ))
  else
    crashes=0
  fi

  if [ "$crashes" -ge 2 ] && { [ "${EBO_VIDEO}" != "0" ] || [ "${EBO_AUDIO}" = "1" ]; }; then
    echo "[add-on] bridge crashed ${crashes}× quickly with A/V ON — disabling video+audio and continuing with control only."
    export EBO_VIDEO=0
    export EBO_AUDIO=0
    crashes=0
  fi

  echo "[add-on] bridge exited (rc=${rc}), restarting in 15s…"
  sleep 15 &
  wait $!
done
