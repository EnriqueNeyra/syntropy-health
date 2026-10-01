#!/bin/sh
# Runs the server as the owner of the data directory, so a bind-mounted ./data stays owned by
# you on the host (and is writable whatever your host user id is). A fresh directory Docker
# created as root is handed to the image's own "syntropy" user instead.
set -e
DATA="${SYNTROPY_DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  owner="$(stat -c %u "$DATA")"
  group="$(stat -c %g "$DATA")"
  if [ "$owner" = "0" ]; then
    chown syntropy:syntropy "$DATA"
    owner="$(id -u syntropy)"
    group="$(id -g syntropy)"
  fi
  exec setpriv --reuid="$owner" --regid="$group" --clear-groups "$@"
fi
exec "$@"
