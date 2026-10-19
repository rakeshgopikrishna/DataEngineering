#!/usr/bin/env bash
# AzCopy Runner launcher - macOS, Linux, and Git Bash on Windows.
# Usage: bash start.sh [--port 8765] [--no-browser]
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "AzCopy Runner needs Python 3.8 or newer."
  case "$(uname -s)" in
    Darwin) echo "On a Mac, run:  xcode-select --install   (installs python3), then start again." ;;
    Linux)  echo "Install it with your package manager, e.g.:  sudo apt install python3" ;;
    *)      echo "Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'." ;;
  esac
  exit 1
fi

exec "$PY" app/server.py "$@"
