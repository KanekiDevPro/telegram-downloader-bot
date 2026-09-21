#!/usr/bin/env bash
# Foreground runner used by hosting panels that expect a single start command.
#
#   bash deploy/run.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

if [ ! -x .venv/bin/python ]; then
    echo "ERROR: .venv is missing — run 'bash deploy/install.sh' first." >&2
    exit 1
fi

exec ./.venv/bin/python main.py
