# Card scanner images (see docs/INSTALL.md)
#   server - web app, card data, OCR reader and AI requests (docker-compose.yml)
#   client - a camera station: station.py next to the camera (docker-compose.client.yml)

# The OCR reader's packages (light-ocr: Node.js, x86-64 only)
FROM node:24-trixie-slim AS ocr
WORKDIR /ocr
COPY src/ocr/package.json src/ocr/package-lock.json ./
RUN npm ci --omit=dev


FROM python:3.12-slim-trixie AS server

# The OCR reader's GPU path is WebGPU through Vulkan: libvulkan1, and the X11 / GLVND libraries
# NVIDIA's Vulkan driver loads (measured: without them "Found no drivers" and OCR reads on the CPU)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 libatomic1 \
       libvulkan1 libx11-6 libxext6 libglvnd0 libgl1 libglx0 libegl1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ocr /usr/local/bin/node /usr/local/bin/node

# PYTHONPATH: the code is the package /app/src/card_scanner; app.py puts src/ on the path itself, this is for the
# entrypoint's `python -c "from config import ..."` and for anything run by hand in the container
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=ocr /ocr/node_modules src/ocr/node_modules

# data/ and scanned_cards/ are the volumes; the app runs as the user who owns them on the host
RUN mkdir -p data scanned_cards && chown 1000:1000 data scanned_cards
USER 1000:1000

EXPOSE 5000
ENTRYPOINT ["scripts/docker-entrypoint.sh"]


# The camera station: camera, card detection and focus only - x86-64 and ARM64
FROM python:3.12-slim-trixie AS client

# v4l2-ctl: the USB camera's focus and zoom controls
RUN apt-get update \
    && apt-get install -y --no-install-recommends v4l-utils \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements-client.txt .
RUN pip install --no-cache-dir -r requirements-client.txt

# Only what the station runs, laid out as in the project: the launcher and config.yaml in
# /app - where paths.py says the top folder is, and where the volumes put data/ and
# station_captures/ - and of the package card_scanner its station/ folder and the four
# modules both programs use. Not core/, server/ or games/: a station imports none of them
# (tests/test_layout.py). A new module in station/ is in the image without a change here;
# a new SHARED module must be named in this list AND in the client-pi target below.
COPY station.py config.yaml ./
COPY src/card_scanner/__init__.py src/card_scanner/paths.py src/card_scanner/config.py \
     src/card_scanner/config_loader.py src/card_scanner/settings.py src/card_scanner/
COPY src/card_scanner/station src/card_scanner/station

# The station's setup page (station_setup.py)
EXPOSE 8080

ENTRYPOINT ["python", "station.py"]


# The camera station for a Raspberry Pi camera module (ARM64 only): the same program as
# `client`, on a different base.
#
# Why a second image: a Pi camera is not reached through OpenCV like a USB webcam, but through
# picamera2 / libcamera, and the version that knows the Pi's cameras is not on PyPI - it comes
# from Raspberry Pi's own package archive, built against Debian's Python. So this image is
# plain Debian 13 with that archive added, and takes Python and every library from apt
# (python3-opencv instead of the pip wheel), exactly as the station runs on Raspberry Pi OS
# without Docker. It works for USB webcams too, but is larger than `client`.
#
# The container also needs more of the host than a webcam does - see
# docker-compose.client-pi.yml.
FROM debian:trixie-slim AS client-pi

# 1. Raspberry Pi's archive needs its signing keys before apt will trust it. They are in a
#    package of that same archive (raspberrypi-archive-keyring), so the package is fetched
#    by hand, over HTTPS: the archive's package list says where its current version is.
#    (The key file offered for download, raspberrypi.gpg.key, is signed with SHA-1, which
#    Debian 13 no longer accepts - with it apt calls the archive "not signed".)
#    curl is only needed for this step and is removed again in the same layer.
# 2. The archive as a package source, then the station's libraries, all as Debian packages.
#    --no-install-recommends keeps out the desktop preview windows that picamera2 can use
#    and a station never shows.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && archive=https://archive.raspberrypi.com/debian \
    && keyring=$(curl -fsSL "$archive/dists/trixie/main/binary-$(dpkg --print-architecture)/Packages.gz" | gzip -dc \
                 | awk '$1 == "Package:" { wanted = ($2 == "raspberrypi-archive-keyring") } wanted && $1 == "Filename:" { print $2 }' | tail -1) \
    && test -n "$keyring" \
    && curl -fsSL "$archive/$keyring" -o /tmp/keyring.deb \
    && dpkg -i /tmp/keyring.deb && rm /tmp/keyring.deb \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.pgp] http://archive.raspberrypi.com/debian/ trixie main" \
         > /etc/apt/sources.list.d/raspi.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-picamera2 python3-opencv python3-socketio python3-websocket \
         python3-requests python3-yaml v4l-utils \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
# The same files as the client target above (keep the two lists equal)
COPY station.py config.yaml ./
COPY src/card_scanner/__init__.py src/card_scanner/paths.py src/card_scanner/config.py \
     src/card_scanner/config_loader.py src/card_scanner/settings.py src/card_scanner/
COPY src/card_scanner/station src/card_scanner/station

EXPOSE 8080

ENTRYPOINT ["python3", "station.py"]
