#!/usr/bin/env bash
# Start the resume editor locally.
#
# Guards the two things that actually go wrong on this machine:
#   - `python3` resolving to a Python without the dependencies
#   - a stale server still holding the port
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"

# There are three python3 on the PATH here (miniconda, Homebrew, /usr/bin) and
# only miniconda's has the dependencies. Which one wins depends on whether conda
# has activated in this shell yet, so pick by capability rather than by name.
PY=""
for cand in "$HOME/miniconda3/bin/python3" python3 python; do
  if command -v "$cand" >/dev/null 2>&1 \
     && "$cand" -c "import fastapi, uvicorn, psycopg" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "No Python on PATH has the dependencies installed."
  echo
  echo "  python3 -m pip install -r requirements.txt"
  exit 1
fi

# A stale server is the usual reason a start looks like it 'failed' — uvicorn's
# bind error scrolls past under a deprecation warning and is easy to miss.
# -sTCP:LISTEN matters: without it this also matches every browser tab holding a
# connection to the server, which is not what is blocking the port.
if lsof -ti:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use by:"
  lsof -ti:"$PORT" -sTCP:LISTEN | while read -r pid; do
    ps -p "$pid" -o pid=,command= | cut -c1-100 | sed 's/^/  /'
  done
  echo
  echo "  kill \$(lsof -ti:$PORT -sTCP:LISTEN)"
  exit 1
fi

# Without DATABASE_URL the app silently falls back to a local SQLite file, which
# is a different database from the one the editor normally shows.
if [ ! -f .env ]; then
  echo "Warning: no .env — falling back to local SQLite instead of Neon."
  echo "         Copy .env.example to .env and add your DATABASE_URL."
  echo
fi

echo "Python: $PY"
echo "URL:    http://127.0.0.1:$PORT"
echo
exec "$PY" server.py
