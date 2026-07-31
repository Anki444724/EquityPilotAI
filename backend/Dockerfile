# =============================================================================
# Backend image — multi-stage.
#
# Two stages rather than one because the build needs compilers (argon2-cffi,
# lxml and psycopg all build C extensions) and the runtime does not. Shipping
# gcc in a production image adds ~250 MB and hands an attacker a toolchain.
#
# The wheel-building stage compiles once; the runtime stage installs the
# prebuilt wheels and keeps only the shared libraries actually needed at run
# time — tesseract for Module 7's OCR path and the DejaVu fonts Module 9's PDF
# engine registers for the rupee glyph.
# =============================================================================

# --- stage 1: build wheels ---------------------------------------------------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libffi-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# --- stage 2: runtime --------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # matplotlib writes a font cache on first use; without a writable home it
    # fails inside a read-only container.
    MPLCONFIGDIR=/tmp/matplotlib

RUN apt-get update && apt-get install -y --no-install-recommends \
        # Module 7: OCR for scanned filings.
        tesseract-ocr \
        tesseract-ocr-eng \
        # Module 9: the PDF engine registers DejaVu Sans because base-14
        # Helvetica has no rupee glyph and every ₹ renders as a black box.
        fonts-dejavu-core \
        # Postgres client library, and pg_dump for the backup service.
        libpq5 \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

# A non-root user. A container that runs as root turns a code-execution bug
# into host-level access.
RUN groupadd --system --gid 1001 ierp \
    && useradd --system --uid 1001 --gid ierp --create-home ierp

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=ierp:ierp . .

# /data/documents is where the Railway Volume holding uploaded PDFs is
# mounted. It must exist and be owned by the application user *before* the
# volume is attached: the container runs as non-root `ierp`, and a volume
# mounted onto a path that does not exist is created owned by root, leaving
# the app unable to write a single byte. Every upload then fails with
# PermissionError, which surfaces to the client as a bare HTTP 500.
RUN mkdir -p /app/backups /tmp/matplotlib /data/documents \
    && chown -R ierp:ierp /app/backups /tmp/matplotlib /data

# su-exec drops privileges in the entrypoint. gosu is the Debian equivalent;
# su-exec is a few kilobytes and does the same job.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gosu; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf "$(command -v gosu)" /usr/local/bin/su-exec

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Deliberately NOT `USER ierp`: the entrypoint starts as root purely to take
# ownership of the mounted volume, then drops to `ierp` before exec'ing the
# application. A volume is mounted over its mount point after the image is
# built, so build-time ownership is discarded and this is the only point at
# which it can be corrected.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000

# Readiness, not liveness: the orchestrator should route traffic only when the
# database and schema are actually usable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ready || exit 1

# One worker per container; scale by adding containers rather than by adding
# workers, so a crash takes down one replica rather than four.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
