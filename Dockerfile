FROM cloakhq/cloakbrowser:latest@sha256:2fdd1289154f30594c7bab74943ea7baa6b617a9ec026ab4bf339c2ebabcb712

WORKDIR /app

ENV HOME=/app/data/home \
    XDG_CONFIG_HOME=/app/data/home/.config \
    XDG_CACHE_HOME=/app/data/home/.cache \
    CLOAKBROWSER_CACHE_DIR=/app/data/.cloakbrowser \
    CLOAKBROWSER_BINARY_PATH=/opt/cloakbrowser-binary/chrome \
    CLOAKBROWSER_AUTO_UPDATE=false \
    DISPLAY=:99 \
    LAY4FLOW_PROFILE_VIEWER_ENABLED=1 \
    PYTHONUNBUFFERED=1

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

RUN python -c "from pathlib import Path; import shutil; browsers=list(Path('/root/.cloakbrowser').glob('chromium-*/chrome')); assert len(browsers)==1, 'expected one bundled Cloak browser'; shutil.move(str(browsers[0].parent), '/opt/cloakbrowser-binary')" \
    && rm -rf /app/cloakbrowser

COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

ARG APP_VERSION=1.2.0
LABEL org.opencontainers.image.title="lay4flow-remote" \
      org.opencontainers.image.version="$APP_VERSION"

COPY . .

RUN mkdir -p /app/data/home /app/data/.cloakbrowser /app/tmp \
    && touch /app/logs.txt \
    && sed -i 's/\r$//' /app/docker/entrypoint.debug.sh \
    && chmod +x /app/docker/entrypoint.debug.sh

EXPOSE 8000
CMD ["/app/docker/entrypoint.debug.sh"]
