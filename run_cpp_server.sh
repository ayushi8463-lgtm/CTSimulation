#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-./.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="${PYTHON:-python3}"
fi

PY_CONFIG="$(command -v python3-config)"
read -r -a PY_INCLUDES <<< "$("$PY_CONFIG" --includes)"
read -r -a PY_LINK <<< "$("$PY_CONFIG" --embed --ldflags)"

c++ -std=c++17 -O2 -pthread "${PY_INCLUDES[@]}" cpp_server.cpp "${PY_LINK[@]}" -o ct_simulator_server
export PYTHONPATH="${PWD}${PYTHONPATH:+:$PYTHONPATH}"
export CT_SIMULATOR_LOCAL=1
export CT_PYTHON_SITE="$("$PYTHON_BIN" -c 'import site; print(site.getsitepackages()[0])')"
exec ./ct_simulator_server "${1:-5000}"
