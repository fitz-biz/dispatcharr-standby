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


def _encoder():
    """Prefer the iGPU. vah264enc is the modern VA element, vaapih264enc the old
    one; fall back to software so the sidecar still runs on a box without VA."""
    for element, args in (
        ("vah264enc", f"vah264enc bitrate={BITRATE} key-int-max={OUT_FPS}"),
        ("vaapih264enc", f"vaapih264enc bitrate={BITRATE} keyframe-period={OUT_FPS}"),
    ):
        if subprocess.run(["gst-inspect-1.0", element],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return args
    return (f"x264enc tune=zerolatency speed-preset=veryfast "
            f"key-int-max={OUT_FPS} bitrate={BITRATE}")


ENCODER = None  # resolved once at startup


def build_pipeline(uri):
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
        "fb.video_0", "!", "queue", "!", "videoconvert", "!", "videoscale", "!",
        "videorate", "!",
        f"video/x-raw,width={OUT_W},height={OUT_H},framerate={OUT_FPS}/1", "!",
        *ENCODER.split(), "!",
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

        # Swallowing pipeline stderr makes a broken pipeline look like an empty
        # stream, which is a miserable thing to debug. DEBUG=1 surfaces it.
        stderr = None if os.environ.get("DEBUG") else subprocess.DEVNULL
        proc = subprocess.Popen(
            build_pipeline(uri), stdout=subprocess.PIPE,
            stderr=stderr, start_new_session=True)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        try:
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
    ENCODER = _encoder()
    print(f"encoder: {ENCODER}", flush=True)
    print(f"slate:   {SLATE} ({'present' if os.path.exists(SLATE) else 'absent, using black'})",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
