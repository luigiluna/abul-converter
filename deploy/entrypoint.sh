#!/bin/sh
# Docker creates a missing ./data bind mount as root:root, which the unprivileged
# app user cannot write. Fix ownership, then run the app as "app" (never as root).
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    find "${DATA_DIR:-/data}" \! -user app -exec chown app:app {} +
    export HOME=/home/app
    exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi

exec "$@"
