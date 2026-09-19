# Standby-slate sidecar.
#
# A custom image is unavoidable: fallbackswitch/fallbacksrc are not packaged by
# any distro (checked Ubuntu 24.04, Debian trixie and sid) and the stock
# restreamio/gstreamer image ships GStreamer 1.20.2 without them. The plugin
# exists only as Rust source, so it has to be compiled. That is the entire
# reason for the build stage; everything else is stock apt.
#
# Both stages share a base release so the plugin links against the same
# GStreamer the runtime has.

FROM rust:1-trixie AS plugin
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      libssl-dev pkg-config git \
 && rm -rf /var/lib/apt/lists/*
# cargo-c produces a real shared library with the right soname, which a plain
# cargo build does not.
RUN cargo install cargo-c --locked
# The 0.14 line targets GStreamer 1.26, which is what debian:trixie ships.
# Bumping the runtime base means bumping this to match.
ARG PLUGINS_RS_BRANCH=0.14
RUN git clone --depth 1 --branch "$PLUGINS_RS_BRANCH" \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src
WORKDIR /src
# Only fallbackswitch. Building the whole workspace would take far longer and
# pull in dependencies this sidecar has no use for.
RUN cargo cinstall -p gst-plugin-fallbackswitch --release \
      --prefix=/usr --libdir=/usr/lib/gst-plugin


FROM debian:trixie
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      gstreamer1.0-tools \
      gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly \
      gstreamer1.0-libav \
      gstreamer1.0-vaapi \
      gstreamer1.0-x \
      va-driver-all \
      fonts-dejavu-core \
      ca-certificates \
 && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      apt-get install -y --no-install-recommends intel-media-va-driver; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# cargo-c installs into a gstreamer-1.0/ subdirectory, which is the level
# GST_PLUGIN_PATH has to name. The static lib and pkgconfig are build artefacts
# with no use at runtime.
COPY --from=plugin /usr/lib/gst-plugin/gstreamer-1.0/libgstfallbackswitch.so \
                   /usr/lib/gstreamer-1.0/
ENV GST_PLUGIN_PATH=/usr/lib/gstreamer-1.0

COPY server.py /opt/sidecar/server.py
COPY make-slate.sh /opt/sidecar/make-slate.sh

# Fail the build rather than ship an image whose whole point is missing.
RUN gst-inspect-1.0 fallbacksrc > /dev/null \
 && mkdir -p /opt/slate \
 && /opt/sidecar/make-slate.sh /opt/slate/slate.ts

ENV PORT=8099 \
    OUT_W=1280 \
    OUT_H=720 \
    OUT_FPS=30 \
    BITRATE_KBPS=5000 \
    SLATE=/opt/slate/slate.ts

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8099/healthz',timeout=3)"

CMD ["python3", "/opt/sidecar/server.py"]
