ARG BUILD_FROM=docker.io/library/python:3.11-slim
FROM ${BUILD_FROM}

# Required tools: jq + curl are used by run.sh (options + Supervisor MQTT). ca-certificates
# for TLS. Keep this step minimal so an update rebuild rarely fails here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends jq curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Core Python deps (required). Retry so a flaky network doesn't break the build.
# The Agora SDK version is pinned so control is reproducible; bump it to test the video path.
ARG AGORA_SDK_VERSION=2.4.9
RUN pip install --no-cache-dir --retries 5 --timeout 60 \
        "agora-python-server-sdk==${AGORA_SDK_VERSION}" paho-mqtt cryptography

# --- video-only extras (optional): a failure here must NOT break the add-on update ---
# ffmpeg (republish the stream). If it can't be installed, video just won't work.
RUN (apt-get update \
        && apt-get install -y --no-install-recommends ffmpeg \
        && rm -rf /var/lib/apt/lists/*) \
    || echo "WARN: ffmpeg not installed — video will be unavailable"

# mediamtx (RTSP server). Downloaded from GitHub; make it non-fatal so a GitHub
# outage/rate-limit can't fail the whole image rebuild (video is off by default anyway).
ARG MEDIAMTX_VERSION=1.9.3
RUN curl -fsSL --retry 5 --retry-delay 3 \
      "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
      | tar xz -C /usr/local/bin mediamtx \
    || echo "WARN: mediamtx download failed — video will be unavailable"

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/agora/agora_sdk
WORKDIR /app
# VERSION.txt changes every release, so this COPY layer is never stale-cached: the running
# image always contains the current code. run.sh logs it to prove which version is live.
COPY VERSION.txt ebo_bridge.py ebo_cloud.py ebo_sign.py ebo_video.py ebo_log.py run.sh /app/
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
