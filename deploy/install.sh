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
    echo "    Edit it now: BOT_TOKEN, ADMIN_IDS, DATABASE_URL, POSTGRES_PASSWORD, REDIS_URL"
else
    echo "==> .env already exists, leaving it untouched"
fi

# Bring an older .env up to the current .env.example without touching what the
# operator wrote (E1, same contract as install.sh): every example key wholly
# absent from .env is appended with its default, under one annotated block,
# after a single atomic timestamped backup — and silence when nothing is missing.
sync_missing_env_vars() {
    [ -f .env.example ] || return 0
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "[INFO] Created .env from .env.example."
        return 0
    fi
    local line="" key="" default="" missing=""
    while IFS= read -r line || [ -n "$line" ]; do
        if printf '%s' "$line" | grep -qE '^[A-Z0-9_]+='
        then
            key="${line%%=*}"
            if ! grep -q "^${key}=" .env
            then
                default="${line#*=}"
                missing="${missing}${key}=${default}
"
            fi
        fi
    done <.env.example
    [ -n "$missing" ] || return 0
    cp .env .env.bak-$(date +%s) || return 1
    if ! grep -q "^# Auto-synced missing variables" .env
    then
        printf '\n# Auto-synced missing variables (appended by deploy/install.sh from .env.example)\n' >>.env
    fi
    printf '%s' "$missing" | while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        printf '%s\n' "$line" >>.env
        printf '%s\n' "[INFO] Added missing configuration variable ${line%%=*} to .env"
    done
}

sync_missing_env_vars

# Fill one .env key in place when .env carries it, append it when it does not
# (a blind append would leave duplicate lines — one filled, one still empty).
set_env_key() {
    if grep -q "^$1=" .env; then
        sed "s|^$1=.*|$1=$2|" .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

# --- Optional: database password ------------------------------------------
# .env.example documents POSTGRES_PASSWORD for the docker-compose stack (which
# refuses to start without it); a host-run bot keeps its password inline in
# DATABASE_URL instead. Prompted only on a terminal — a piped or unattended
# run leaves .env exactly as it was seeded.
if [ -t 0 ]; then
    printf 'PostgreSQL password for the downloader role (POSTGRES_PASSWORD, Enter = skip): '
    pg_password=""
    read -r pg_password || true
    if [ -n "$pg_password" ]; then
        set_env_key POSTGRES_PASSWORD "$pg_password"
        echo "==> POSTGRES_PASSWORD written into .env"
    fi
fi

# --- Optional: Local Telegram Bot API -------------------------------------
# The cloud API refuses bot uploads over 50 MB; a local telegram-bot-api
# server raises the ceiling to 2000 MB. It needs TELEGRAM_API_ID and
# TELEGRAM_API_HASH from https://my.telegram.org → API development tools.
# Offered only on a terminal and only ever on an explicit yes — a piped or
# unattended run stays non-interactive, and answering no leaves .env exactly
# as it was. Writing the keys also pins TELEGRAM_API_BASE_URL, which is what
# turns the local server on (see .env.example — the server itself starts with
# the stack like every other service).
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
                set_env_key TELEGRAM_API_ID "$api_id"
                set_env_key TELEGRAM_API_HASH "$api_hash"
                set_env_key TELEGRAM_API_BASE_URL "http://telegram-api:8081"
                echo "==> Local Telegram API configured"
                echo "    The server starts with the stack:"
                echo "      docker compose up -d --build"
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
