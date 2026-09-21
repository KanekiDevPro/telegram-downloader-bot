#!/usr/bin/env bash
# Installer for shared Python hosting panels and plain VPS boxes (no Docker).
#
#   bash deploy/install.sh
#
# Creates .venv, installs dependencies, sanity-checks ffmpeg and seeds .env.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_DIR"
echo "==> target directory: $APP_DIR"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "ERROR: $PYTHON_BIN not found. Install Python 3.11+ first." >&2
    exit 1
}

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "ERROR: Python 3.11+ is required (found: $("$PYTHON_BIN" --version))." >&2
    exit 1
}

if [ ! -d .venv ]; then
    echo "==> creating virtualenv"
    "$PYTHON_BIN" -m venv .venv
fi

echo "==> installing dependencies"
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if command -v ffmpeg >/dev/null 2>&1; then
    echo "==> ffmpeg: $(ffmpeg -version | head -n1)"
else
    echo "WARNING: ffmpeg not found on PATH — MP3 (audio) downloads will be rejected."
    echo "         Install it (apt-get install ffmpeg / yum install ffmpeg) and re-run."
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> created .env from .env.example"
    echo "    Edit it now: BOT_TOKEN, ADMIN_IDS, DATABASE_URL, REDIS_URL"
else
    echo "==> .env already exists, leaving it untouched"
fi

cat <<'EOF'

==> done.

Start it manually:   bash deploy/run.sh
Run it as a service: see deploy/README.md (systemd or supervisor)

Reminder: this bot needs PostgreSQL. If your shared host does not provide one,
point DATABASE_URL at a managed Postgres (Neon, Supabase, a VPS, ...).
Redis is optional — without it the queue and FSM state stay in memory
(QUEUE_BACKEND=memory), which is fine for a single small instance.
EOF
