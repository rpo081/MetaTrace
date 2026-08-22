#!/bin/sh
# MetaTrace container entrypoint.
#
# Runs as root only to fix volume ownership on first start, then re-executes
# the server as the unprivileged `appuser`. If the container is already
# started as a non-root user (e.g. docker-compose `user: "1000:1000"` with a
# pre-owned volume), it proceeds directly.
set -e

if [ "$(id -u)" = "0" ]; then
    # Named volumes start root-owned; hand /data to appuser (idempotent).
    # mkdir guards bare runs without a volume mounted at /data.
    mkdir -p /data
    chown -R appuser:appuser /data
    # Drop privileges for the actual server process (setpriv ships in
    # debian-slim via util-linux; clean exec, no shell-quoting pitfalls).
    exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi

exec "$@"
