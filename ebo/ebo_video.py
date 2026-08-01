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
import tempfile
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
    def __init__(self, rtsp_port=8554, path="ebo", fps=None):
        super().__init__()
        self.rtsp_port = rtsp_port
        self.rtsp_url = f"rtsp://127.0.0.1:{rtsp_port}/{path}"
        # target output fps (configurable) — we DROP source frames to hit it, cutting encode CPU.
        self.fps = int(fps or os.environ.get("EBO_VIDEO_FPS", "20") or "20")
        self.src_fps = int(os.environ.get("EBO_VIDEO_SRC_FPS", "25") or "25")  # robot's ~rate
        self._dec_acc = 0                 # frame-decimation accumulator
        # bitrate cap in kbps (VBV) so busy scenes can't spike bandwidth/CPU (0 = uncapped)
        self.bitrate = int(os.environ.get("EBO_VIDEO_BITRATE", "2500") or "0")
        # downscale to cut CPU on the re-encode (0 = keep the robot's native resolution)
        self.max_h = int(os.environ.get("EBO_VIDEO_MAX_HEIGHT", "720") or "0")
        self.preset = os.environ.get("EBO_VIDEO_PRESET", "ultrafast")
        # optional audio (listen): 16 kHz mono PCM from the SDK, muxed as AAC (default off)
        self.audio = os.environ.get("EBO_AUDIO", "0") == "1"
        # robot mic is 8 kHz mono (measured on the real app); must match the SDK PCM rate
        self.audio_rate = int(os.environ.get("EBO_AUDIO_RATE", "8000"))
        self._a_w = None              # write end of the audio pipe to ffmpeg
        self._audio_lock = threading.Lock()
        self._last_audio = 0.0        # last time real PCM arrived
        self.ff = None
        self.w = 0
        self.h = 0
        self.frames = 0
        self._last_frame = 0.0        # wall-clock of the last decoded frame (liveness: robot awake?)
        # LATENCY CONTROL: on_frame does NOT write to ffmpeg directly (a blocking write while ffmpeg
        # is behind would make decoded frames pile up in the Agora SDK and the delay grow without
        # bound). Instead it drops the newest frame into a single slot (overwriting = dropping any
        # older un-encoded frame) and a dedicated writer thread feeds ffmpeg. Result: we always
        # encode the FRESHEST frame ffmpeg can accept and simply skip the ones in between — latency
        # stays bounded (at the cost of fps when the CPU can't keep up), which is what you want for
        # driving.
        self._pending = None          # (y,u,v,w,h) latest frame awaiting encode; overwrite=drop
        self._pending_evt = threading.Event()
        self._writer = None
        self._dropped = 0
        self._src_count = 0           # source/encoded frame counters for the fps diagnostic
        self._enc_count = 0
        self._src_t0 = 0.0
        self.feeding = False          # only pipe to ffmpeg while the camera switch is on
        self.lock = threading.Lock()
        self._start_mediamtx()

    # ---- RTSP server ----
    def _start_mediamtx(self):
        # per-instance temp file: a fixed /tmp path would (a) be a predictable-path smell and
        # (b) COLLIDE when the add-on runs one bridge per robot (multi-robot). Key it by port.
        cfg = os.path.join(tempfile.gettempdir(), "ebo_mediamtx_%d.yml" % self.rtsp_port)
        # Low-Latency HLS for a FLUID browser preview (the snapshot path is choppy). HTTP-based, so
        # it works straight through the add-on's mapped port — no WebRTC/ICE finickiness. ~0.5-1s.
        self.hls_port = 8888 + (self.rtsp_port - 8554)
        # WebRTC (WHEP): the panel's fullscreen 'drive' view plays this for a TRULY fluid, ~200 ms
        # preview (much better than HLS for actually driving). The robot's H.265 is already
        # re-encoded to H.264 here, which the browser CAN decode over WebRTC. ICE candidates use the
        # host's real LAN IPs (below) so the browser reaches us even across NIC/VLAN boundaries.
        self.webrtc_port = 8189 + (self.rtsp_port - 8554)
        ice_hosts = [ip.strip() for ip in
                     os.environ.get("EBO_HOST_IPS", "").split(",") if ip.strip()]
        # de-dup, keep order; a YAML flow list "[a, b]" (empty -> mediamtx auto-detects interfaces)
        seen, hosts = set(), []
        for ip in ice_hosts:
            if ip not in seen:
                seen.add(ip)
                hosts.append(ip)
        additional_hosts = ("[" + ", ".join(hosts) + "]") if hosts else "[]"
        with open(cfg, "w") as f:
            f.write("logLevel: error\n"
                    f"rtspAddress: :{self.rtsp_port}\n"
                    "hls: yes\n"
                    f"hlsAddress: :{self.hls_port}\n"
                    "hlsVariant: lowLatency\n"
                    # only mux HLS when a client actually asks for it (fallback). Always-remux would
                    # burn CPU generating HLS even while everyone is on the fluid WebRTC path.
                    "hlsAlwaysRemux: no\n"
                    "hlsSegmentCount: 7\n"
                    "hlsSegmentDuration: 1s\n"
                    "hlsPartDuration: 200ms\n"
                    "hlsAllowOrigin: '*'\n"
                    "webrtc: yes\n"
                    f"webrtcAddress: :{self.webrtc_port}\n"
                    f"webrtcLocalUDPAddress: :{self.webrtc_port}\n"
                    f"webrtcAdditionalHosts: {additional_hosts}\n"
                    "webrtcAllowOrigin: '*'\n"
                    "webrtcICEServers2: []\n"
                    "rtmp: no\n"
                    "paths:\n  all_others:\n")
        log("[video] WebRTC(WHEP) on :%d — ICE hosts %s" % (self.webrtc_port, additional_hosts))
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
        gop = max(self.src_fps, 1)       # a keyframe every ~1s at the source rate
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
            # A default pipe holds 64 KB = ~4 s of 8 kHz mono PCM. That reservoir is exactly how the
            # audio ended up seconds behind the video. Shrink it so a backlog simply can't build:
            # when it's full we drop the newest chunk (see write_audio) and stay near real time.
            try:
                import fcntl
                fcntl.fcntl(self._a_w, 1031, 8192)      # F_SETPIPE_SZ = 1031, 8 KB ~= 0.5 s
            except Exception:
                pass
            # Small queue + no buffering: the video path already drops stale frames to bound
            # latency; audio had no such control, so it queued up and arrived seconds late.
            audio_in = ["-thread_queue_size", "64",
                        "-fflags", "+nobuffer", "-flags", "+low_delay",
                        "-f", "s16le",
                        "-ar", str(self.audio_rate), "-ac", "1", "-i", "pipe:%d" % a_r]
            # Opus, NOT AAC. WebRTC only carries Opus / G.711 / G.722 — with AAC the browser gets
            # no audio track at all in the drive view (the stream had sound, WebRTC just dropped it).
            # Opus also works in mediamtx's fMP4 HLS. 48 kHz mono, low bitrate: the source is an
            # 8 kHz telephony mic, so there is nothing to gain from more.
            audio_out = ["-af", "aresample=async=1:first_pts=0",
                         "-c:a", "libopus", "-ar", "48000", "-ac", "1",
                         "-b:a", "24k", "-application", "lowdelay",
                         "-frame_duration", "10"]
            pass_fds = (a_r,)
        _nullout = os.environ.get("EBO_VIDEO_NULLOUT") == "1"   # DIAG: encode to null (isolate mediamtx)
        self.ff = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            # low latency: timestamp frames by arrival (clean monotonic DTS/PTS — fixes the
            # Clean, MONOTONIC input timestamps from the raw framerate. Do NOT use
            # -use_wallclock_as_timestamps: under bursty feeding it hands ffmpeg duplicate/
            # non-monotonic DTS ("124 >= 124") that stall the encoder. rawvideo from a pipe is not
            # throttled by -framerate — it only assigns even PTS. The robot streams ~25 fps.
            "-fflags", "+nobuffer", "-flags", "+low_delay",
            "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", "%dx%d" % (w, h), "-framerate", str(self.src_fps),
            "-i", "pipe:0",
        ] + audio_in + scale + [
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p",
        ] + (
            # VBV cap: keep bitrate bounded so motion can't spike bandwidth/CPU
            ["-maxrate", "%dk" % self.bitrate, "-bufsize", "%dk" % (self.bitrate * 2)]
            if self.bitrate > 0 else []
        ) + audio_out + [
            # low muxer delay for latency. NB: do NOT add -flush_packets 1 here — over RTSP/TCP it
            # forces per-packet writes that stall ffmpeg's output and starve the encoder (throughput
            # collapsed to ~5 fps). muxdelay/muxpreload 0 is enough.
            "-muxdelay", "0", "-muxpreload", "0",
        ] + (["-f", "null", "-"] if _nullout else
             ["-f", "rtsp", "-rtsp_transport", "tcp", self.rtsp_url]),
            stdin=subprocess.PIPE, pass_fds=pass_fds)
        if _nullout:
            log("[video] DIAG: ffmpeg output = NULL (mediamtx bypassed)")
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
                # kill (not terminate): avoids ffmpeg trying to write an RTSP trailer to a
                # already-gone reader, which spams "Error writing trailer: Broken pipe"
                self.ff.kill()
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

    def is_streaming(self):
        """True when live frames are actually arriving — i.e. the robot is awake and publishing.
        Used to decide whether turning the camera on needs a fresh RTC re-join to WAKE the robot."""
        return self.feeding and self.frames > 0 and (time.time() - self._last_frame) < 3.0

    # ---- camera switch ----
    def start_feed(self):
        with self.lock:
            self.feeding = True

    def stop_feed(self):
        with self.lock:
            self.feeding = False
            self._stop_ffmpeg()
            self.w = self.h = 0

    def _ensure_writer(self):
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(target=self._writer_loop, daemon=True)
            self._writer.start()

    def _writer_loop(self):
        """The ONLY thread that writes YUV to ffmpeg's stdin. Blocking writes live here, off the
        Agora callback thread, so a slow encoder drops frames (via the 1-slot buffer) instead of
        backing up and inflating latency."""
        while True:
            try:
                if not self._pending_evt.wait(0.5):
                    continue
                self._pending_evt.clear()
                with self.lock:
                    item = self._pending
                    self._pending = None
                    ff = self.ff
                if item is None or ff is None or ff.stdin is None:
                    continue
                y, u, v = item[0], item[1], item[2]
                try:
                    ff.stdin.write(y)
                    ff.stdin.write(u)
                    ff.stdin.write(v)
                except (BrokenPipeError, ValueError, OSError):
                    with self.lock:
                        if self.ff is ff:
                            self._stop_ffmpeg()
            except Exception as e:
                log("[video] writer error:", e)

    # ---- Agora callback: one decoded YUV frame ----
    def on_frame(self, channel_id, remote_uid, frame):
        try:
            with self.lock:
                if not self.feeding:
                    return 0
                w, h = frame.width, frame.height
                if not w or not h or frame.y_buffer is None:
                    return 0
                if self.ff is None or (w, h) != (self.w, self.h) or self.ff.poll() is not None:
                    if self.ff is not None and self.ff.poll() is not None:
                        log("[video] ffmpeg exited (rc=%s) — restarting encoder" % self.ff.poll())
                    self._start_ffmpeg(w, h)
                    self.w, self.h = w, h
                    self.frames = 0
                    self._dec_acc = 0
                    self._pending = None
                    self._ensure_writer()
                # NOTE: no fixed frame decimation. At a sane resolution the encoder keeps up, so we
                # want EVERY source frame for smoothness; the 1-slot buffer already drops frames only
                # when the encoder actually falls behind (adaptive). Fixed decimation just threw away
                # good frames and made the video choppier than the source.
                self._src_count += 1              # every decoded frame (source rate diagnostic)
                now = time.time()
                if self._src_t0 == 0.0:
                    self._src_t0 = now
                elif now - self._src_t0 >= 30.0:
                    # every 30 s, not every 5: at 5 s this one line drowned out everything else in
                    # the add-on log (audio diagnostics especially) within a couple of minutes.
                    log("[video] source ~%.1f fps, encoded ~%.1f fps, %d dropped (encoder behind)"
                        % (self._src_count / (now - self._src_t0),
                           self._enc_count / (now - self._src_t0), self._dropped))
                    self._src_t0 = now
                    self._src_count = 0
                    self._enc_count = 0
                    self._dropped = 0
                # If the writer thread hasn't consumed the previous frame yet, ffmpeg is still busy —
                # DROP this frame *before* the expensive plane copy (packing a 3 MP frame just to
                # overwrite it wastes the very CPU ffmpeg needs). Dropping early keeps latency bounded
                # AND frees CPU for the encoder, so the frames we DO keep encode faster.
                if self._pending is not None:
                    self._dropped += 1
                    self._last_frame = time.time()   # still a live frame → robot is awake
                    return 0
                y = _pack_plane(frame.y_buffer, frame.y_stride or w, w, h)
                u = _pack_plane(frame.u_buffer, frame.u_stride or (w // 2), w // 2, h // 2)
                v = _pack_plane(frame.v_buffer, frame.v_stride or (w // 2), w // 2, h // 2)
                self._pending = (y, u, v, w, h)
                self._enc_count += 1
                self.frames += 1
                self._last_frame = time.time()
                first = self.frames == 1
                nframes = self.frames
            self._pending_evt.set()
            if first:
                log("[video] first decoded frame %dx%d (pix_type=%s) strides y=%s u=%s v=%s "
                    "(w=%d → %s) — encoding to RTSP"
                    % (w, h, getattr(frame, "type", "?"), frame.y_stride, frame.u_stride,
                       frame.v_stride, w, "PADDED (slow unpack!)" if (frame.y_stride or w) != w
                       else "no padding"))
            elif nframes % 4500 == 0:         # light heartbeat (~every few minutes)
                log("[video] streaming — %d frames (%dx%d), %d dropped for latency"
                    % (nframes, w, h, self._dropped))
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
