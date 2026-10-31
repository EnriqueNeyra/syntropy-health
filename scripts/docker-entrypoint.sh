#!/bin/sh
# Runs the server as the owner of the data directory, so a bind-mounted ./data stays owned by
# you on the host (and is writable whatever your host user id is). A fresh directory Docker
# created as root is handed to the image's own "syntropy" user instead. Files inside that belong
# to someone else, such as a backup restored with sudo, are handed to the same owner: the server
# can't start with them.
set -e
DATA="${SYNTROPY_DATA_DIR:-/data}"
export HOME="$DATA"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  owner="$(stat -c %u "$DATA")"
  group="$(stat -c %g "$DATA")"
  if [ "$owner" = "0" ]; then
    owner="$(id -u syntropy)"
    group="$(id -g syntropy)"
    echo "$DATA belonged to root, so it now belongs to this image's own account (user id $owner)." >&2
    echo "If it's a folder on this computer, reading it needs sudo. To own it yourself: stop the container, run sudo chown -R \"\$(id -u):\$(id -g)\" on it, and start again." >&2
    chown -R "$owner:$group" "$DATA" || true
  elif [ -n "$(find "$DATA" ! -user "$owner" -print -quit 2>/dev/null)" ]; then
    echo "Some files in $DATA belonged to another account (a restored backup, perhaps), so they now belong to user id $owner, like the folder." >&2
    chown -R "$owner:$group" "$DATA" || true
  fi
  if ! setpriv --reuid="$owner" --regid="$group" --clear-groups test -w "$DATA"; then
    echo "Syntropy Health can't write to $DATA as user id $owner. Check who owns that folder and whether it's mounted read-only." >&2
  fi
  exec setpriv --reuid="$owner" --regid="$group" --clear-groups "$@"
fi
exec "$@"
