"""
ebo_video.py — receive the robot's Agora video as DECODED YUV (the SDK decodes H.265),
re-encode to H.264 and republish as RTSP so Home Assistant can show it as a camera.

Pipeline:  Agora video-frame observer (I420 YUV)  ->  ffmpeg (libx264)  ->  RTSP (mediamtx)

The SDK's *encoded* frame path segfaults for H.265, but it CAN decode H.265 to raw YUV via
the decoded video-frame observer — that's what we use here. The RTSP stream is served at
rtsp://<add-on host>:8554/ebo.
"""
import os
import subprocess
import threading
import time

from agora.rtc.video_frame_observer import IVideoFrameObserver

from ebo_log import log


def _pack_plane(buf, stride, width, height):
    """Return a tightly-packed plane (strip any stride padding)."""
    b = bytes(buf)
    if stride == width:
        return b[:width * height]
    out = bytearray(width * height)
    for row in range(height):
        src = row * stride
        out[row * width:(row + 1) * width] = b[src:src + width]
    return bytes(out)


class VideoPipeline(IVideoFrameObserver):
    def __init__(self, rtsp_port=8554, path="ebo", fps=15):
        super().__init__()
        self.rtsp_port = rtsp_port
        self.rtsp_url = f"rtsp://127.0.0.1:{rtsp_port}/{path}"
        self.fps = fps
        self.ff = None
        self.w = 0
        self.h = 0
        self.frames = 0
        self.feeding = False          # only pipe to ffmpeg while the camera switch is on
        self.lock = threading.Lock()
        self._start_mediamtx()

    # ---- RTSP server ----
    def _start_mediamtx(self):
        cfg = "/tmp/mediamtx.yml"
        with open(cfg, "w") as f:
            f.write("logLevel: error\n"
                    f"rtspAddress: :{self.rtsp_port}\n"
                    "paths:\n  all_others:\n")
        try:
            self.mediamtx = subprocess.Popen(
                ["/usr/local/bin/mediamtx", cfg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            log("[video] mediamtx RTSP server on :%d" % self.rtsp_port)
        except FileNotFoundError:
            log("[video] mediamtx not found — video disabled")
            self.mediamtx = None

    # ---- ffmpeg: raw I420 in -> H.264 RTSP out ----
    def _start_ffmpeg(self, w, h):
        self._stop_ffmpeg()
        log("[video] starting ffmpeg %dx%d -> RTSP (H.264)" % (w, h))
        gop = max(self.fps, 1) * 2       # a keyframe every ~2s so clients attach quickly
        self.ff = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", "%dx%d" % (w, h), "-framerate", str(self.fps),
            "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_url,
        ], stdin=subprocess.PIPE)

    def _stop_ffmpeg(self):
        if self.ff:
            try:
                if self.ff.stdin:
                    self.ff.stdin.close()
            except Exception:
                pass
            try:
                self.ff.terminate()
            except Exception:
                pass
            self.ff = None

    # ---- camera switch ----
    def start_feed(self):
        with self.lock:
            self.feeding = True

    def stop_feed(self):
        with self.lock:
            self.feeding = False
            self._stop_ffmpeg()
            self.w = self.h = 0

    # ---- Agora callback: one decoded YUV frame ----
    def on_frame(self, channel_id, remote_uid, frame):
        try:
            with self.lock:
                if not self.feeding:
                    return 0
                w, h = frame.width, frame.height
                if not w or not h or frame.y_buffer is None:
                    return 0
                if self.ff is None or (w, h) != (self.w, self.h):
                    self._start_ffmpeg(w, h)
                    self.w, self.h = w, h
                    self.frames = 0
                y = _pack_plane(frame.y_buffer, frame.y_stride or w, w, h)
                u = _pack_plane(frame.u_buffer, frame.u_stride or (w // 2), w // 2, h // 2)
                v = _pack_plane(frame.v_buffer, frame.v_stride or (w // 2), w // 2, h // 2)
                try:
                    self.ff.stdin.write(y)
                    self.ff.stdin.write(u)
                    self.ff.stdin.write(v)
                except (BrokenPipeError, ValueError):
                    self._stop_ffmpeg()
                    return 0
                self.frames += 1
                if self.frames == 1:
                    log("[video] first decoded frame %dx%d (pix_type=%s) — encoding to RTSP"
                        % (w, h, getattr(frame, "type", "?")))
                elif self.frames % 300 == 0:
                    log("[video] %d frames received (%dx%d)" % (self.frames, w, h))
        except Exception as e:
            log("[video] frame error:", e)
        return 0

    def stop(self):
        self.stop_feed()
        for p in (getattr(self, "mediamtx", None),):
            try:
                if p:
                    p.terminate()
            except Exception:
                pass
