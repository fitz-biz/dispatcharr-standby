#!/usr/bin/env python3
"""Standby-slate sidecar for Dispatcharr.

Serves GET /stream?u=<upstream url>&ua=<user agent> as an MPEG-TS stream that
starts instantly with a card and switches to the live feed the moment it is
actually decodable.

The work is done by GStreamer's fallbacksrc, which owns the pipeline's
running-time clock. That is the whole reason this exists: an ffmpeg-side splice
of slate-then-live cannot keep the timeline monotonic across the join, and Plex
copies Live TV bytes straight through (-codec:V copy -segment_time 1), so its
segmenter sees the break. fallbacksrc has no splice to get wrong, and it waits
for a decodable keyframe before switching, which is what makes a mid-GOP join
survivable.

Dispatcharr reaches this through a stream profile whose command is curl, so
nothing in the Dispatcharr image has to change.
"""
import os
import shutil
import signal
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8099"))
OUT_W = int(os.environ.get("OUT_W", "1280"))
OUT_H = int(os.environ.get("OUT_H", "720"))
OUT_FPS = int(os.environ.get("OUT_FPS", "30"))
BITRATE = int(os.environ.get("BITRATE_KBPS", "5000"))
SLATE = os.environ.get("SLATE", "/opt/slate/slate.ts")
# Seconds before giving up on the main source and showing the card.
TIMEOUT_S = float(os.environ.get("TIMEOUT_S", "2"))
# Seconds before retrying a main source that stopped producing.
RESTART_S = float(os.environ.get("RESTART_S", "1"))

NS = 1_000_000_000

# Dispatcharr will not release a client until it has buffered roughly 2.3MB.
# A live feed fills that in about 3.6s, but a standby card is a static image
# that compresses to ~65KB/s and so takes 35s, which is worse than the problem
# this came to fix. So prime the buffer with MPEG-TS null packets: PID 0x1FFF
# is padding, every demuxer discards it, and it costs nothing but loopback
# bandwidth. Set PRIME_BYTES=0 for a consumer that does not need it.
PRIME_BYTES = int(os.environ.get("PRIME_BYTES", str(3 * 1024 * 1024)))
NULL_TS_PACKET = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xFF" * 184
NULL_TS_BLOCK = NULL_TS_PACKET * 348  # ~64KB, a whole number of TS packets


def _encoder_kind():
    """Prefer the iGPU. vah264enc is the modern VA element, vaapih264enc the old
    one; fall back to software so the sidecar still runs on a box without VA."""
    for element in ("vah264enc", "vaapih264enc"):
        if subprocess.run(["gst-inspect-1.0", element],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return element
    return "x264enc"


ENCODER_KIND = None  # resolved once at startup


def _encoder_args(bitrate):
    if ENCODER_KIND == "vah264enc":
        return f"vah264enc bitrate={bitrate} key-int-max={OUT_FPS}"
    if ENCODER_KIND == "vaapih264enc":
        return f"vaapih264enc bitrate={bitrate} keyframe-period={OUT_FPS}"
    return (f"x264enc tune=zerolatency speed-preset=veryfast "
            f"key-int-max={OUT_FPS} bitrate={bitrate}")


def _default_bitrate(height):
    if height >= 1080:
        return 8000
    if height >= 720:
        return 5000
    return 2500


def _int_param(qs, key, default, lo, hi):
    """Clamped, even-valued integer. Callers are trusted (Dispatcharr), but a
    typo should not hand the encoder an absurd frame size."""
    try:
        v = int((qs.get(key) or [None])[0])
    except (TypeError, ValueError):
        return default
    v = max(lo, min(hi, v))
    return v - (v % 2)  # h264 wants even dimensions


def build_pipeline(uri, width, height, bitrate):
    fallback = []
    if os.path.exists(SLATE):
        fallback = [f"fallback-uri=file://{SLATE}"]
    # Only properties that exist in both the 0.14 and 0.15 plugin lines. 0.14
    # spells the generated-source options enable-video/fallback-video-caps and
    # 0.15 spells them enable-dummy/dummy-video-caps, and gst-launch treats an
    # unknown property as a fatal pipeline error. None of them are needed: the
    # card comes from fallback-uri and videoscale fixes the size downstream.
    return [
        "gst-launch-1.0", "-q",
        "fallbacksrc", "name=fb", f"uri={uri}",
        *fallback,
        "immediate-fallback=true", "restart-on-eos=true",
        f"timeout={int(TIMEOUT_S * NS)}",
        f"restart-timeout={int(RESTART_S * NS)}",
        # Two things matter here.
        #
        # pixel-aspect-ratio=1/1: without it videoscale is free to preserve the
        # source's display aspect by emitting a non-square PAR instead of adding
        # borders, which then relies on the player honouring the SAR. That shows
        # up as intermittent stretching when a source changes shape mid-stream
        # (ad breaks, SD inserts). Pinned, videoscale must letterbox or
        # pillarbox instead, which every player renders identically.
        #
        # format=I420 before the scale: videoscale fills its borders in the
        # negotiated format, and with the format left open it picks one where
        # the fill comes out magenta rather than black. Measured: RGB
        # (255,184,0) unpinned, (0,0,0) pinned.
        #
        # The format is deliberately NOT pinned on the output caps. vaapih264enc
        # wants NV12, so pinning I420 there fails negotiation and the pipeline
        # silently produces nothing. Leave it open and let the encoder pick.
        "fb.video_0", "!", "queue", "!", "videoconvert", "!",
        "video/x-raw,format=I420", "!",
        "videoscale", "add-borders=true", "!", "videoconvert", "!",
        "videorate", "!",
        f"video/x-raw,width={width},height={height},"
        f"framerate={OUT_FPS}/1,pixel-aspect-ratio=1/1", "!",
        *_encoder_args(bitrate).split(), "!",
        "h264parse", "config-interval=1", "!", "mux.",
        "fb.audio_0", "!", "queue", "!", "audioconvert", "!", "audioresample", "!",
        "audio/x-raw,rate=48000,channels=2", "!",
        "avenc_aac", "bitrate=128000", "!", "aacparse", "!", "mux.",
        "mpegtsmux", "name=mux", "!", "fdsink", "fd=1",
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path != "/stream":
            self.send_error(404)
            return

        qs = urllib.parse.parse_qs(parsed.query)
        uri = (qs.get("u") or [""])[0]
        if not uri.startswith(("http://", "https://")):
            self.send_error(400, "u must be an http(s) URL")
            return

        # Output size is per-request so one sidecar can back several Dispatcharr
        # profiles (a 1080p one and a 720p one) instead of a container each.
        width = _int_param(qs, "w", OUT_W, 160, 1920)
        height = _int_param(qs, "h", OUT_H, 120, 1080)
        bitrate = _int_param(qs, "b", _default_bitrate(height), 200, 20000)

        # Swallowing pipeline stderr makes a broken pipeline look like an empty
        # stream, which is a miserable thing to debug. DEBUG=1 surfaces it.
        stderr = None if os.environ.get("DEBUG") else subprocess.DEVNULL
        proc = subprocess.Popen(
            build_pipeline(uri, width, height, bitrate), stdout=subprocess.PIPE,
            stderr=stderr, start_new_session=True)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        try:
            sent = 0
            while sent < PRIME_BYTES:
                chunk = NULL_TS_BLOCK[:min(len(NULL_TS_BLOCK), PRIME_BYTES - sent)]
                self.wfile.write(chunk)
                sent += len(chunk)
            shutil.copyfileobj(proc.stdout, self.wfile, 65536)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # The client (Plex, via Dispatcharr) hanging up must tear down the
            # whole pipeline, not leave gst-launch holding a provider connection.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=5)


if __name__ == "__main__":
    ENCODER_KIND = _encoder_kind()
    print(f"encoder: {ENCODER_KIND} (bitrate chosen per request from output height)",
          flush=True)
    print(f"slate:   {SLATE} ({'present' if os.path.exists(SLATE) else 'absent, using black'})",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
