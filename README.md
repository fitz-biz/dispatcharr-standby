# dispatcharr-standby

A GStreamer sidecar that makes an IPTV channel start showing something
**immediately** and switch to the live feed the moment it is genuinely
decodable, instead of leaving the player on a spinner until it gives up.

Built for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) feeding Plex
Live TV, but it is just an HTTP service that takes an upstream URL and returns
MPEG-TS, so nothing about it is Dispatcharr-specific.

```
GET /stream?u=<upstream url>&ua=<user agent>[&w=&h=&b=]  ->  MPEG-TS
GET /healthz                                             ->  ok
```

`w`, `h` and `b` (kbps) override the output size and bitrate per request, so one
sidecar can back several profiles, for example a 1080p one and a 720p one,
rather than a container each. They are clamped (`w` ≤ 1920, `h` ≤ 1080, `b` ≤
20000) and rounded to even. Bitrate defaults from the height: 8000 at 1080p,
5000 at 720p, 2500 below.

## The problem

A provider whose feed is off the air, or whose CDN serves empty responses from
most of its edges, leaves the player with nothing. Measured on a real setup, a
not-yet-live channel took **23.1 seconds to deliver its first byte**, which is
far longer than Plex will wait. The viewer gets a spinner and then an error,
with no indication that the event simply has not started.

## Why this is not just an ffmpeg command

The obvious fix is to play a card and splice the live feed on when it arrives.
That does not work.

Two independently muxed MPEG-TS streams both start their clock at the same PTS,
so concatenating them sends the timeline backwards. Plex copies Live TV bytes
straight through to the client (`-codec:V copy -codec:a copy -segment_time 1`),
so its segmenter sees the break with no decoder in the middle to absorb it:

```
Packet corrupt (stream = 0, dts = 844920)
DTS 127920 < 844920 out of order
```

ffmpeg's concat demuxer fixes the clock, because it owns the output timeline.
But a live stream is joined mid-GOP, so the decoder has no parameter sets yet:

```
non-existing PPS 0 referenced
no frame!
```

GStreamer's [`fallbacksrc`](https://gstreamer.freedesktop.org/documentation/fallbackswitch/fallbacksrc.html)
has no splice at all. One pipeline owns running-time across both sources, and it
waits for a decodable keyframe before switching. It also retries the main source
on its own, which handles flaky CDN edges with no extra logic.

Observed switch on a live source:

```
0:00:00.049  Switched to fallback stream
0:00:00.502  Switched to main stream (audio)
0:00:02.220  Switched to main stream (video)
```

## Measured

Through the built image, against a real provider:

| Source | Time to headers | Backward jumps | Decode errors | Plex segments |
|---|---|---|---|---|
| Live channel | 1.5ms | 0 | 0 | 18, keyframe in segment 0 |
| Dead channel | 1.3ms | 0 | 0 | continuous, no spinner |

Against 23.1s of nothing without it. Encoding in those runs was `x264enc`.

## Run it

```sh
docker run -d --name dispatcharr-standby --restart unless-stopped \
  -p 8099:8099 --device /dev/dri:/dev/dri \
  ghcr.io/fitz-biz/dispatcharr-standby:latest

curl -s http://localhost:8099/healthz
docker logs dispatcharr-standby | head -2
```

`--device /dev/dri` hands it the GPU. The server prefers `vah264enc`, then
`vaapih264enc`, then falls back to `x264enc`, and **prints the choice on
startup**. Check that line: a silent fall back to software encoding will hurt at
1080p.

`linux/amd64` only. The image compiles a Rust plugin, and building that for
arm64 under emulation costs far more than it is worth.

### Settings

| Variable | Default | |
|---|---|---|
| `PORT` | `8099` | |
| `OUT_W` / `OUT_H` | `1280` / `720` | output size |
| `OUT_FPS` | `30` | |
| `BITRATE_KBPS` | `5000` | |
| `TIMEOUT_S` | `2` | seconds before showing the card |
| `RESTART_S` | `1` | seconds before retrying a stalled source |
| `SLATE_TEXT` | `Starting soon` | baked in at build time |
| `PRIME_BYTES` | `3145728` | null-packet burst to prime a buffering client |
| `DEBUG` | unset | `1` surfaces pipeline stderr in the log |

### Why the priming burst

Dispatcharr will not release a client until it has buffered roughly 2.3MB. A
live feed fills that in about 3.6s, but a standby card is a static image that
compresses to ~65KB/s and takes **35 seconds**, which is worse than the problem
this exists to solve.

So the response opens with MPEG-TS null packets (PID 0x1FFF), which are padding
that every demuxer discards. Measured through Dispatcharr:

| Channel | Without priming | With priming |
|---|---|---|
| Dead source | 35.2s | **0.59s** |
| Live source | 3.64s | **0.61s** |

Both still segment cleanly under Plex's copy-mode segmenter with a keyframe in
segment 0. Set `PRIME_BYTES=0` for a consumer that does not buffer this way.

## Wiring it to Dispatcharr

Dispatcharr builds a stream profile as `[command] + shlex.split(parameters)`, as
argv with no shell, so point it at the sidecar with `curl`, which is already in
the Dispatcharr image. Nothing about the Dispatcharr image or your channel URLs
has to change.

- **command:** `/bin/sh`
- **parameters:**
  `-c 'exec /usr/bin/curl -s --max-time 86400 -G --data-urlencode "u=$1" --data-urlencode "ua=$2" http://<sidecar-host>:8099/stream' _ {streamUrl} {userAgent}`

For a second profile at a different size, add the size to the URL:

  `... http://<sidecar-host>:8099/stream?w=1920&h=1080' _ {streamUrl} {userAgent}`

`-G` appends the encoded `u` and `ua` to an existing query string correctly, so
the two forms compose.

The values arrive as positional arguments, so a hostile URL cannot inject.
Rolling back is switching the channels' profile back.

**Use `-G --data-urlencode`, not a hand-built query string.** Dispatcharr's
`{userAgent}` contains spaces, and pasting it into a URL gets the request
rejected before it leaves the container:

```
curl: (3) URL rejected: Malformed input to a URL function
```

which surfaces as a channel that simply never starts. Letting curl encode the
query also covers stream URLs that contain `&` or `?`.

## Why the image is custom

`fallbackswitch` is not packaged anywhere: not Ubuntu 24.04, not Debian trixie
or sid, and the stock `restreamio/gstreamer` image ships GStreamer 1.20.2
without it. Debian carries Rust GStreamer plugins only as
`librust-gst-plugin-*-dev` source packages and fallbackswitch is not among them.
It exists as Rust source, so it has to be compiled. That is the only reason for
the build stage; the rest of the image is stock apt.

Two version constraints, both deliberate:

- **The plugin line must match the runtime GStreamer.** `debian:trixie` ships
  GStreamer 1.26, so `PLUGINS_RS_BRANCH` is pinned to `0.14`. The current plugin
  release is 0.15, which targets 1.28. Bumping the base means bumping the arg.
- **The pipeline uses only properties common to 0.14 and 0.15.** 0.14 spells the
  generated-source options `enable-video`/`fallback-video-caps`; 0.15 spells them
  `enable-dummy`/`dummy-video-caps`. `gst-launch` treats an unknown property as
  fatal, which presents as an empty stream rather than an error.

## Known limits

- `fallbacksrc` has an [open upstream issue](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/issues/132)
  about not always recovering when a source goes down and comes back.
- One pipeline per client, so concurrent viewers cost concurrent upstream
  connections. Mind your provider's connection cap.
- Not tested behind Plex at scale.

## License

MIT. See [LICENSE](LICENSE).
