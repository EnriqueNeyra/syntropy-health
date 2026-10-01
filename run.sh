#!/usr/bin/env bash
# Syntropy Health launcher.
#   ./run.sh            run locally (creates .venv on first use)  -> http://localhost:8000
#   ./run.sh dev        run with auto-reload for development
#   ./run.sh docker     build and start the Docker container
#   ./run.sh down       stop the Docker container
#   ./run.sh test       run the test suite
#   ./run.sh mcp        start the MCP server on stdio (for AI assistants)
#   ./run.sh directory  refresh the health-system directory from Epic, Oracle Health and eClinicalWorks
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mkdir -p data
PORT="${PORT:-8000}"

venv() {
  if [ ! -x .venv/bin/python ]; then
    echo "[*] Creating virtual environment…"
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
  fi
  .venv/bin/pip install -q -r "${1:-requirements.txt}"
}

case "${1:-local}" in
  docker)      docker compose up --build -d && echo "[+] Running at http://localhost:${PORT} (logs: docker compose logs -f)";;
  down)        docker compose down;;
  test)        venv requirements-dev.txt && .venv/bin/python -m pytest -q tests;;
  mcp)         venv >/dev/null && exec .venv/bin/python -m app.mcp_server;;
  directory)   venv >/dev/null && exec .venv/bin/python scripts/refresh_directory.py "${@:2}";;
  dev)         venv && exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload;;
  local|*)     venv && echo "[+] Syntropy Health at http://localhost:${PORT}" && \
               exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers;;
esac
