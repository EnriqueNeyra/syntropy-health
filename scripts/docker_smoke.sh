#!/bin/sh
# Runs a built image the ways people do, for the Docker workflows:  sh scripts/docker_smoke.sh IMAGE [PLATFORM]
set -eu
IMAGE="$1"
PLATFORM="${2:-}"
NAME=syntropy-smoke
VOLUME=syntropy-smoke-data
SECONDS_TO_START=240     # arm64 runs under emulation

run() { docker run ${PLATFORM:+--platform "$PLATFORM"} "$@"; }

wait_for_health() {
  i=0
  while [ "$i" -lt "$SECONDS_TO_START" ]; do
    if curl -fs localhost:8000/health >/dev/null; then return 0; fi
    i=$((i + 1)); sleep 1
  done
  docker logs "$NAME"
  echo "::error::The server didn't answer: $1"
  exit 1
}

stop() { docker rm -f "$NAME" >/dev/null; }

data="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm -f "$VOLUME" >/dev/null 2>&1 || true' EXIT

# A folder on this computer, with the limits docker-compose.yml sets. The server runs as the folder's owner, not root.
run -d --name "$NAME" -p 8000:8000 -v "$data:/data" --cap-drop ALL --cap-add CHOWN --cap-add DAC_READ_SEARCH \
  --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges "$IMAGE"
wait_for_health "with docker-compose.yml's limits"
test "$(stat -c %u "$data/syntropy.db")" = "$(id -u)"
docker exec "$NAME" sh -c 'grep -q "^CapEff:.*0000000000000000" /proc/1/status'

# The command the app's messages name. docker exec starts as root; what it writes has to stay the folder's owner's.
docker exec "$NAME" syntropy-health version
docker exec "$NAME" syntropy-health accounts
docker exec "$NAME" syntropy-health backup /data/backups
backup="$(find "$data/backups" -name 'syntropy-backup-*.zip')"
test -n "$backup" && test "$(stat -c %u "$backup")" = "$(id -u)"
docker exec -w /tmp "$NAME" syntropy-health version     # from any folder

# What the licence asks to travel with it, and the tool that reads photos of lab reports.
docker exec "$NAME" ls /app/LICENSE /app/NOTICE
docker exec "$NAME" tesseract --version >/dev/null
stop

# A backup restored as root into a volume: the entrypoint gives it back, with nothing to do by hand.
docker volume create "$VOLUME" >/dev/null
run -d --name "$NAME" -p 8000:8000 -v "$VOLUME:/data" "$IMAGE"
wait_for_health "on a new volume"
docker stop "$NAME" >/dev/null
docker rm "$NAME" >/dev/null
run --rm --entrypoint sh -v "$VOLUME:/data" "$IMAGE" -c 'chown -R 0:0 /data'
run -d --name "$NAME" -p 8000:8000 -v "$VOLUME:/data" "$IMAGE"
wait_for_health "with files owned by root"
docker logs "$NAME" 2>&1 | grep -q "belonged to root"
stop
echo "The image passed."
