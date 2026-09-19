#!/bin/sh
# Render the standby card once, as a short MPEG-TS clip that fallbacksrc loops.
# Rendered with GStreamer rather than ffmpeg so the container needs no ffmpeg.
set -e
OUT="${1:-/opt/slate/slate.ts}"
W="${OUT_W:-1280}"
H="${OUT_H:-720}"
FPS="${OUT_FPS:-30}"
TEXT="${SLATE_TEXT:-Starting soon}"

gst-launch-1.0 -e \
  videotestsrc pattern=black num-buffers=$((FPS * 5)) ! \
  "video/x-raw,width=$W,height=$H,framerate=$FPS/1" ! \
  textoverlay text="$TEXT" font-desc="Sans Bold 48" \
    valignment=center halignment=center ! \
  x264enc tune=zerolatency speed-preset=ultrafast key-int-max="$FPS" bitrate=1500 ! \
  h264parse ! mux. \
  audiotestsrc wave=silence num-buffers=$((FPS * 8)) ! \
  "audio/x-raw,rate=48000,channels=2" ! \
  avenc_aac bitrate=128000 ! aacparse ! mux. \
  mpegtsmux name=mux ! filesink location="$OUT"

test -s "$OUT"
