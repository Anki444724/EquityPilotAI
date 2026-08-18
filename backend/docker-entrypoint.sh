#!/bin/sh
# Container entrypoint.
#
# Exists for one reason: a Railway Volume is mounted *over* its mount point
# after the image is built, so any ownership set with `chown` in the
# Dockerfile is replaced by the volume's own — root. The application runs as
# the unprivileged `ierp` user and cannot write a single byte to it, and every
# upload fails.
#
# The container therefore starts as root, takes ownership of the mount, and
# immediately drops to `ierp` with `su-exec` before executing the real
# command. Nothing application-related ever runs as root.
set -e

STORAGE_PATH="${DOCUMENT_STORAGE_PATH:-/data/documents}"
SQLITE_DATA_PATH="${SQLITE_DATA_PATH:-/data/database}"
APP_USER="${APP_USER:-ierp}"

if [ "$(id -u)" = "0" ]; then
    # Create the directory if the volume mounted an empty root, then hand it
    # to the application user. Both are no-ops when already correct, so a
    # restart costs nothing.
    mkdir -p "$STORAGE_PATH" 2>/dev/null || true
    if ! chown -R "$APP_USER":"$APP_USER" "$STORAGE_PATH" 2>/dev/null; then
        echo "entrypoint: WARNING could not chown $STORAGE_PATH; uploads may fail" >&2
    fi

    # The zero-config image stores SQLite under /data rather than beside the
    # read-only application source. This also repairs ownership when a user
    # mounts a fresh volume over the image's build-time directory.
    mkdir -p "$SQLITE_DATA_PATH" 2>/dev/null || true
    if ! chown -R "$APP_USER":"$APP_USER" "$SQLITE_DATA_PATH" 2>/dev/null; then
        echo "entrypoint: WARNING could not chown $SQLITE_DATA_PATH; SQLite may fail" >&2
    fi

    # Backups live on the same principle.
    mkdir -p /app/backups 2>/dev/null || true
    chown -R "$APP_USER":"$APP_USER" /app/backups 2>/dev/null || true

    echo "entrypoint: writable data paths prepared; dropping privileges to $APP_USER"
    exec su-exec "$APP_USER" "$@"
fi

# Already unprivileged (e.g. a platform that pins the user): run as-is.
exec "$@"
