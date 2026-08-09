#!/usr/bin/env bash
#
# run.sh — launch the Nonprofit AI toolkit.
#
#   ./run.sh           start the server and open the app in your browser
#   ./run.sh test      run full key-free unittest discovery
#
set -euo pipefail
cd "$(dirname "$0")"

# Local development may keep ignored credentials in .env. Tests deliberately
# skip it so their key-free environment remains deterministic.
if [[ "${1:-}" != "test" && -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

PORT="${PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
export PORT
export APP_ENV="${APP_ENV:-development}"
export PUBLIC_APP_URL="${PUBLIC_APP_URL:-$URL}"
export EMAIL_BACKEND="${EMAIL_BACKEND:-memory}"
export AUTH_PEPPER="${AUTH_PEPPER:-local-development-only-pepper-change-before-production}"

if [[ -n "${TOOLKIT_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$TOOLKIT_PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi

command -v "$PYTHON_BIN" >/dev/null || {
  echo "Python not found at ${PYTHON_BIN} — install Python 3.12 or set TOOLKIT_PYTHON_BIN." >&2
  exit 1
}
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Python 3.11 or newer is required; Python 3.12 is pinned in .python-version." >&2
  exit 1
}

# Run a deterministic local adapter when no model key is present. Production
# rejects this adapter in backend/config.py.
if [[ -n "${OLLAMA_API_KEY:-}" ]]; then
  export MODEL_BACKEND="${MODEL_BACKEND:-ollama}"
else
  export MODEL_BACKEND="${MODEL_BACKEND:-stub}"
fi

# 1. key-free test mode
if [[ "${1:-}" == "test" ]]; then
  "$PYTHON_BIN" -m unittest discover -s tests -p 'test_*.py' "${@:2}"
  exit $?
fi

# 2. if a previous run still holds the port, stop it
if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
  echo "port ${PORT} busy — stopping the old server…"
  lsof -ti "tcp:${PORT}" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

# 3. start the server; always stop it again on exit
echo "starting on ${URL}  (model backend ${MODEL_BACKEND})"
"$PYTHON_BIN" server.py &
SERVER_PID=$!
trap 'kill "${SERVER_PID}" 2>/dev/null || true' EXIT

# 4. wait until it answers
for _ in $(seq 1 30); do
  curl -s -o /dev/null "${URL}/" 2>/dev/null && break
  sleep 0.3
done

# 5. open the browser and stay up until ctrl-c
command -v open >/dev/null && open "${URL}" || echo "open ${URL} in your browser."
echo "ready — press ctrl-c to stop."
wait "${SERVER_PID}"
