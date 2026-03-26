FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Display + WM
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb openbox tint2 \
    # Browsers
    chromium-browser firefox \
    # Desktop apps
    libreoffice xterm thunar \
    # Image tools
    imagemagick \
    # CLI essentials
    curl wget jq git nodejs npm \
    # Python
    python3 python3-pip python3-venv \
    # Fonts
    fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
RUN pip3 install --break-system-packages \
    "anthropic>=0.52.0" \
    "patchright>=1.0.0" \
    "fastapi>=0.115.0" \
    "uvicorn>=0.34.0" \
    "httpx>=0.28.0" \
    "pyyaml>=6"

# Install Patchright's Chromium
RUN patchright install chromium

# Copy application code
COPY . /opt/cua

# Environment
ENV DISPLAY=:99 \
    DISPLAY_NUM=99 \
    WIDTH=1280 \
    HEIGHT=720 \
    PYTHONPATH=/opt/cua

WORKDIR /opt/cua

EXPOSE 8090

ENTRYPOINT ["/opt/cua/sandbox/entrypoint.sh"]
