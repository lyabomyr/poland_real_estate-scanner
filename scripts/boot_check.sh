#!/usr/bin/env bash
# Boot the dashboard in a pristine venv (requirements.txt only) and hit every
# page — the same environment Streamlit Cloud builds.
#
# Use this to decide whether a stalled deploy is our code or the platform: if
# this passes, the app is healthy and the stall is Streamlit-side.
#
# Usage: ./scripts/boot_check.sh   (set TURSO_URL / TURSO_AUTH_TOKEN to test
#                                   against the real database)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8599}"
TMP="$(mktemp -d)"
trap 'pkill -f "server.port=$PORT" 2>/dev/null || true; rm -rf "$TMP"' EXIT

PYTHON_BIN="$("$ROOT/scripts/select_python.sh")"
echo "==> venv from requirements.txt only via $PYTHON_BIN"
"$PYTHON_BIN" -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install --quiet --upgrade pip
"$TMP/venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"

echo "==> booting dashboard/app.py"
cd "$ROOT"
PYTHONPATH="$ROOT" nohup "$TMP/venv/bin/streamlit" run dashboard/app.py \
  --server.headless=true --server.port="$PORT" > "$TMP/boot.log" 2>&1 &

elapsed=0
until curl -sS -o /dev/null "http://localhost:$PORT" 2>/dev/null; do
  sleep 2; elapsed=$((elapsed + 2))
  if [ "$elapsed" -gt 90 ]; then
    echo "FAIL: did not respond within 90s"
    tail -30 "$TMP/boot.log"
    exit 1
  fi
done
echo "    booted in ~${elapsed}s"

status=0
for page in "" "Listings" "Chat_config"; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://localhost:$PORT/$page")"
  echo "    /$page -> HTTP $code"
  [ "$code" = "200" ] || status=1
done

if grep -qiE "modulenotfound|traceback" "$TMP/boot.log"; then
  echo "FAIL: errors in the boot log"
  grep -iE -A5 "modulenotfound|traceback" "$TMP/boot.log" | head -20
  exit 1
fi

[ "$status" -eq 0 ] && echo "
Dashboard boots clean. A stalled deploy is platform-side, not the app."
exit "$status"
