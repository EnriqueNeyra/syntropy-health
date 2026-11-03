#!/bin/sh
# The syntropy-health command inside the image:
#   docker exec -it syntropy-health syntropy-health backup /data/backups
#   docker exec -it syntropy-health syntropy-health accounts reset-password NAME
# docker exec starts as root. Going through the entrypoint runs the command as the data folder's owner, as the server
# runs, so what it writes stays usable by the server.
export PYTHONPATH=/app
exec docker-entrypoint.sh python -m app "$@"
