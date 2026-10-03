#!/usr/bin/env bash
# Syntropy Health launcher.
#   ./run.sh            run locally (creates .venv on first use)  -> http://localhost:8000
#   ./run.sh dev        run with auto-reload for development
#   ./run.sh docker     build and start the Docker container
#   ./run.sh down       stop the Docker container
#   ./run.sh test       run the test suite
#   ./run.sh lint       check the code with ruff
#   ./run.sh mcp        start the MCP server on stdio (for AI assistants)
#   ./run.sh directory  refresh the health-system directory from Epic, Oracle Health and eClinicalWorks
#   ./run.sh check-sign-in  check that every Epic health system reaches its MyChart sign-in
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p data
PORT="${PORT:-8000}"

venv() {    # the app from this checkout, plus any groups from pyproject.toml given as arguments ("--group dev")
  if [ ! -x .venv/bin/python ]; then
    echo "[*] Creating virtual environment…"
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e . "$@"
}

case "${1:-local}" in
  docker)      docker compose up --build -d && echo "[+] Running at http://localhost:${PORT} (logs: docker compose logs -f)";;
  down)        docker compose down;;
  test)        venv --group dev && .venv/bin/python -m pytest -q tests;;
  lint)        venv --group dev && .venv/bin/ruff check app tests desktop scripts;;
  mcp)         venv >/dev/null && exec .venv/bin/python -m app.mcp_server;;
  directory)   venv >/dev/null && exec .venv/bin/python scripts/refresh_directory.py "${@:2}";;
  check-sign-in) venv >/dev/null && exec .venv/bin/python scripts/check_sign_in.py "${@:2}";;
  dev)         venv && exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload;;
  local|*)     venv && echo "[+] Syntropy Health at http://localhost:${PORT}" && \
               exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers;;
esac
