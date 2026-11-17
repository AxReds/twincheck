# syntax=docker/dockerfile:1
# Photo Organizer - minimal Alpine image, non-root from the first process.
ARG ALPINE_VERSION=3.24

# ---- build stage: pure-Python wheels only (no compiler reaches the final image) ----
FROM alpine:${ALPINE_VERSION} AS deps
RUN apk add --no-cache python3 py3-pip
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --only-binary=:all: --no-deps \
        --target /opt/photoorg/site -r /tmp/requirements.txt \
    && find /opt/photoorg/site -name '__pycache__' -prune -exec rm -rf {} + \
    && rm -rf /opt/photoorg/site/bin

# ---- final stage ----
FROM alpine:${ALPINE_VERSION}
ARG APP_VERSION=1.0.0
LABEL org.opencontainers.image.title="Photo Organizer" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.description="Safe, multi-user reorganisation of photo and video libraries"

RUN apk add --no-cache python3 py3-cryptography exiftool ffmpeg \
    && find /usr/lib/python3* -name '__pycache__' -prune -exec rm -rf {} + || true

COPY --from=deps /opt/photoorg/site /opt/photoorg/site
COPY app/backend/photoorg /opt/photoorg/app/backend/photoorg
COPY app/frontend /opt/photoorg/app/frontend
COPY docker/bin/ /usr/local/bin/
RUN chmod 0755 /usr/local/bin/photoorg-entrypoint /usr/local/bin/generate-key /usr/local/bin/reset-admin-password \
                /usr/local/bin/photoorg-healthcheck \
    && chmod -R a+rX /opt/photoorg \
    && rm -f /opt/photoorg/app/frontend/locales/README.md

ENV PYTHONPATH=/opt/photoorg/site:/opt/photoorg/app/backend \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    HOME=/tmp \
    DATA_DIR=/data \
    STATE_DIR=/state \
    APP_PORT=80

# UID/GID are normally chosen by "user:" in docker-compose.yml;
# The default below only applies to plain "docker run" and is still non-root.
USER 1000:1000
EXPOSE 80
VOLUME ["/data", "/state"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["photoorg-healthcheck"]
ENTRYPOINT ["photoorg-entrypoint"]
CMD ["serve"]
