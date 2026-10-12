#!/bin/sh
# Fix Railway/Fly volume ownership, optionally migrate, then drop to appuser.
set -eu

UPLOAD_DIR="${UPLOAD_DIR:-/data/uploads}"

mkdir -p "$UPLOAD_DIR/meetings"

if [ "$(id -u)" = "0" ]; then
  # Bind mounts often arrive owned by root; appuser must be able to write.
  chown -R appuser:appuser "$UPLOAD_DIR" || true
  chmod -R u+rwX "$UPLOAD_DIR" || true
fi

# Railway runs migrations as a pre-deploy command. Other hosts (docker
# compose, Fly, bank VMs) can set MIGRATE_ON_START=1 instead. A Postgres
# advisory lock makes concurrent replicas run it once.
if [ "${MIGRATE_ON_START:-0}" = "1" ]; then
  if [ "$(id -u)" = "0" ]; then
    runuser -u appuser -- python -m bcp_project.migrate
  else
    python -m bcp_project.migrate
  fi
  export AUTO_MIGRATE=0
fi

if [ "$(id -u)" = "0" ]; then
  exec runuser -u appuser -- "$@"
fi

exec "$@"
