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
    def __init__(self, rtsp_port=8554, path="ebo", fps=25):
        super().__init__()
        self.rtsp_port = rtsp_port
        self.rtsp_url = f"rtsp://127.0.0.1:{rtsp_port}/{path}"
        self.fps = fps
        # downscale to cut CPU on the re-encode (0 = keep the robot's native resolution)
        self.max_h = int(os.environ.get("EBO_VIDEO_MAX_HEIGHT", "720") or "0")
        self.preset = os.environ.get("EBO_VIDEO_PRESET", "ultrafast")
        # optional audio (listen): 16 kHz mono PCM from the SDK, muxed as AAC (default off)
        self.audio = os.environ.get("EBO_AUDIO", "0") == "1"
        self.audio_rate = 16000
        self._a_w = None              # write end of the audio pipe to ffmpeg
        self._audio_lock = threading.Lock()
        self._last_audio = 0.0        # last time real PCM arrived
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
        gop = max(self.fps, 1) * 2       # a keyframe every ~2s so clients attach quickly
        scale = []
        if self.max_h and h > self.max_h:
            scale = ["-vf", "scale=-2:%d" % self.max_h]   # keep aspect, even width
            log("[video] starting ffmpeg %dx%d -> ~%dp H.264/RTSP (preset %s)"
                % (w, h, self.max_h, self.preset))
        else:
            log("[video] starting ffmpeg %dx%d -> H.264/RTSP (preset %s)"
                % (w, h, self.preset))
        # optional audio input via a dedicated pipe (fd inherited by ffmpeg)
        audio_in, audio_out, pass_fds = [], ["-an"], ()
        a_r = None
        if self.audio:
            a_r, self._a_w = os.pipe()
            os.set_inheritable(a_r, True)
            audio_in = ["-thread_queue_size", "1024", "-f", "s16le",
                        "-ar", str(self.audio_rate), "-ac", "1", "-i", "pipe:%d" % a_r]
            audio_out = ["-c:a", "aac", "-b:a", "48k"]
            pass_fds = (a_r,)
        self.ff = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            # low latency: timestamp frames by arrival (clean monotonic DTS/PTS — fixes the
            # "No dts" issue) and DON'T resample the frame rate (forcing CFR buffered/dropped
            # frames and added delay). The robot streams ~25 fps.
            "-fflags", "+genpts+nobuffer", "-flags", "+low_delay",
            "-use_wallclock_as_timestamps", "1",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", "%dx%d" % (w, h), "-framerate", str(self.fps),
            "-i", "pipe:0",
        ] + audio_in + scale + [
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p",
        ] + audio_out + [
            "-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_url,
        ], stdin=subprocess.PIPE, pass_fds=pass_fds)
        if a_r is not None:
            os.close(a_r)             # parent drops the read end (ffmpeg owns it)
            os.set_blocking(self._a_w, False)   # never block the SDK audio thread
            self._last_audio = 0.0
            threading.Thread(target=self._silence_loop, args=(self.ff,),
                             daemon=True).start()

    def _silence_loop(self, ff):
        """Feed silence to the audio pipe when no real audio is arriving, so ffmpeg never
        stalls waiting for the second input (which would freeze the video)."""
        chunk = b"\x00" * int(self.audio_rate * 2 * 0.05)   # 50 ms of s16le silence
        while self.ff is ff and self._a_w is not None:
            if time.time() - self._last_audio > 0.2:
                with self._audio_lock:
                    if self._a_w is not None:
                        try:
                            os.write(self._a_w, chunk)
                        except (BlockingIOError, BrokenPipeError, OSError):
                            pass
            time.sleep(0.05)

    def _stop_ffmpeg(self):
        if self._a_w is not None:
            try:
                os.close(self._a_w)
            except Exception:
                pass
            self._a_w = None
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

    def write_audio(self, pcm):
        """Feed one real PCM chunk (16-bit mono @ audio_rate) to ffmpeg. Non-blocking: drop if
        the pipe is full so the SDK's audio thread never stalls."""
        if self._a_w is None or not self.feeding:
            return
        with self._audio_lock:
            w = self._a_w
            if w is None:
                return
            try:
                os.write(w, bytes(pcm))
                self._last_audio = time.time()
            except (BlockingIOError, BrokenPipeError, OSError):
                pass

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
                elif self.frames % 4500 == 0:     # light heartbeat (~every few minutes)
                    log("[video] streaming — %d frames (%dx%d)" % (self.frames, w, h))
                elif self.frames % 300 == 0:      # detailed, only in debug
                    log("[video] %d frames received" % self.frames, level="debug")
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
