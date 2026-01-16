FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    dbus \
    dbus-x11 \
    at-spi2-core \
    git \
    python3 \
    python3-pip \
    python3-tk \
    python3-lxml \
    python3-pyatspi \
    gnome-screenshot \
    scrot \
    xvfb \
    xauth \
  && rm -rf /var/lib/apt/lists/*

# Keep image reproducible-ish.
RUN python3 -m pip install --no-cache-dir --upgrade pip
RUN python3 -m pip install --no-cache-dir gui-agents==0.1.3 PyAutoGUI==0.9.54 pillow==11.3.0

WORKDIR /workspace
RUN chmod 0777 /workspace

COPY entrypoint.sh /app/entrypoint.sh
COPY selftest.py /app/selftest.py
COPY run_task.py /app/run_task.py

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
