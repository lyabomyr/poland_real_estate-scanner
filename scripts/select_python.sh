#!/usr/bin/env bash
# Print a Python interpreter that can install requirements.txt.
#
# Streamlit and pandas no longer support the system's bundled Python 3.9 on
# some macOS setups, so local diagnostics must pick a newer interpreter
# explicitly instead of assuming `python3` is good enough.
set -euo pipefail

resolve_candidate() {
  local candidate="$1"

  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
    printf '%s\n' "$candidate"
    return 0
  fi

  command -v "$candidate" 2>/dev/null || return 1
}

supports_requirements() {
  local candidate="$1"
  local resolved

  resolved="$(resolve_candidate "$candidate")" || return 1
  "$resolved" - <<'PY' >/dev/null 2>&1
import sys

raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  printf '%s\n' "$resolved"
}

if [[ -n "${PYTHON_BIN:-}" ]]; then
  if resolved="$(supports_requirements "$PYTHON_BIN")"; then
    printf '%s\n' "$resolved"
    exit 0
  fi
  echo "ERROR: PYTHON_BIN=$PYTHON_BIN is not executable or is older than Python 3.10." >&2
  exit 1
fi

for candidate in python3.12 python3.11 python3.10 python3; do
  if resolved="$(supports_requirements "$candidate")"; then
    printf '%s\n' "$resolved"
    exit 0
  fi
done

cat >&2 <<'EOF'
ERROR: no compatible Python interpreter found.
Install Python 3.10+ or set PYTHON_BIN to an absolute path, for example:

  PYTHON_BIN=/opt/homebrew/bin/python3.12 make check-dashboard-deps
EOF
exit 1
