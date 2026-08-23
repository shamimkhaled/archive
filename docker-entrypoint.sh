#!/bin/sh
# Fix Railway/Fly volume ownership then drop to appuser.
set -eu

UPLOAD_DIR="${UPLOAD_DIR:-/data/uploads}"

mkdir -p "$UPLOAD_DIR/meetings"

if [ "$(id -u)" = "0" ]; then
  # Bind mounts often arrive owned by root; appuser must be able to write.
  chown -R appuser:appuser "$UPLOAD_DIR" || true
  chmod -R u+rwX "$UPLOAD_DIR" || true
  exec runuser -u appuser -- "$@"
fi

exec "$@"
