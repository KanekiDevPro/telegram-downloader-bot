#!/usr/bin/env bash
# Installer for shared Python hosting panels and plain VPS boxes (no Docker).
#
#   bash deploy/install.sh
#
# Creates .venv, installs dependencies, verifies the ffmpeg/ffprobe toolchain
# and seeds .env.
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

# The pipeline needs both halves of the toolchain *before* anything is
# installed: ffmpeg converts (audio, stream merges) and ffprobe verifies what
# was produced (services/verify.py). A missing binary is a broken install, so
# this fails early with the exact fix instead of surfacing as a failed download
# three days later.
missing=""
command -v ffmpeg  >/dev/null 2>&1 || missing="$missing ffmpeg"
command -v ffprobe >/dev/null 2>&1 || missing="$missing ffprobe"
if [ -n "$missing" ]; then
    echo "ERROR: required binaries missing:$missing" >&2
    echo "       Both ship in the one ffmpeg package:" >&2
    echo "         Debian/Ubuntu:  sudo apt-get install -y ffmpeg" >&2
    echo "         RHEL/Fedora:    sudo dnf install -y ffmpeg" >&2
    echo "         Alpine:         apk add ffmpeg" >&2
    echo "       Then re-run: bash deploy/install.sh" >&2
    exit 1
fi
echo "==> ffmpeg: $(ffmpeg -version | head -n1)"
echo "==> ffprobe: $(ffprobe -version | head -n1)"

if [ ! -d .venv ]; then
    echo "==> creating virtualenv"
    "$PYTHON_BIN" -m venv .venv
fi

echo "==> installing dependencies"
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> created .env from .env.example"
    echo "    Edit it now: BOT_TOKEN, ADMIN_IDS, DATABASE_URL, REDIS_URL"
else
    echo "==> .env already exists, leaving it untouched"
fi

# --- Optional: Local Telegram Bot API -------------------------------------
# The cloud API refuses bot uploads over 50 MB; a local telegram-bot-api
# server raises the ceiling to 2000 MB. It needs TELEGRAM_API_ID and
# TELEGRAM_API_HASH from https://my.telegram.org → API development tools.
# Offered only on a terminal and only ever on an explicit yes — a piped or
# unattended run stays non-interactive, and answering no leaves .env exactly
# as it was. Writing the keys also pins TELEGRAM_API_BASE_URL, which is what
# turns the local server on (see .env.example and the `local-api` compose
# profile for how the server itself is started).
if [ -t 0 ]; then
    printf 'Configure the Local Telegram API now (bypasses the 50 MB upload limit)? [y/N] '
    reply=""
    read -r reply || true
    case "$reply" in
        [Yy]*)
            printf 'TELEGRAM_API_ID (https://my.telegram.org -> API development tools): '
            api_id=""
            read -r api_id || true
            printf 'TELEGRAM_API_HASH: '
            api_hash=""
            read -r api_hash || true
            if [ -n "$api_id" ] && [ -n "$api_hash" ]; then
                # .env already carries these keys (it is seeded from
                # .env.example), so a blind append would leave duplicate lines
                # — one filled, one still empty. Fill the key in place when it
                # is there, and append it only when it is not.
                set_env_key() {
                    if grep -q "^$1=" .env; then
                        sed "s|^$1=.*|$1=$2|" .env > .env.tmp && mv .env.tmp .env
                    else
                        printf '%s=%s\n' "$1" "$2" >> .env
                    fi
                }
                set_env_key TELEGRAM_API_ID "$api_id"
                set_env_key TELEGRAM_API_HASH "$api_hash"
                set_env_key TELEGRAM_API_BASE_URL "http://telegram-api:8081"
                echo "==> Local Telegram API configured"
                echo "    Start the server with the local-api profile:"
                echo "      docker compose --profile local-api up -d --build"
            else
                echo "==> empty TELEGRAM_API_ID/TELEGRAM_API_HASH — .env left as it is"
            fi
            ;;
    esac
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
