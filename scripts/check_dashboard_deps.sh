#!/usr/bin/env bash
# Verify requirements.txt actually covers everything the dashboard imports.
#
# Why this exists: a local Poetry environment has the scanner's dependencies
# installed too, so importing the dashboard there succeeds even when
# requirements.txt is missing something. Streamlit Cloud installs *only*
# requirements.txt — and that's where the gap surfaces, as a
# ModuleNotFoundError on a deployed page.
#
# This builds a throwaway venv with requirements.txt alone and imports every
# dashboard module, reproducing the hosted environment.
#
# Usage: make check-dashboard-deps
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$(mktemp -d)/venv"
trap 'rm -rf "$(dirname "$VENV")"' EXIT

PYTHON_BIN="$("$ROOT/scripts/select_python.sh")"
echo "==> creating isolated venv (requirements.txt only) via $PYTHON_BIN"
"$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt"

echo "==> importing every dashboard module"
cd "$ROOT"
# Streamlit puts the main script's directory on sys.path, so the pages import
# `db` / `ui` as top-level modules. Mirror that here.
PYTHONPATH="$ROOT:$ROOT/dashboard" "$VENV/bin/python" - <<'PY'
import importlib
import pathlib
import sys

failures = []

# Plain modules first.
for name in ("db", "ui"):
    try:
        importlib.import_module(name)
        print(f"    ok  {name}")
    except Exception as exc:
        failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"    FAIL {name}: {exc}")

# app.py and the pages have emoji/digits in their filenames, so load by path.
paths = [pathlib.Path("dashboard/app.py")] + sorted(
    pathlib.Path("dashboard/pages").glob("*.py")
)
for path in paths:
    spec = importlib.util.spec_from_file_location(f"_page_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        print(f"    ok  {path.name}")
    except SystemExit:
        # st.stop() raises SystemExit outside a Streamlit runtime — the module
        # imported fine, which is all we're checking.
        print(f"    ok  {path.name} (st.stop)")
    except ModuleNotFoundError as exc:
        failures.append(f"{path.name}: {exc}")
        print(f"    FAIL {path.name}: {exc}")
    except Exception as exc:
        # Anything else (no Streamlit runtime, no DB) is expected here; only
        # missing modules mean requirements.txt is incomplete.
        print(f"    ok  {path.name} (ran until {type(exc).__name__})")

if failures:
    print("\nrequirements.txt is missing dependencies:")
    for line in failures:
        print(f"  - {line}")
    sys.exit(1)
print("\nrequirements.txt covers every dashboard import.")
PY
