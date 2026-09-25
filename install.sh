#!/bin/bash
#
# Telegram Downloader Bot — installer *and* control center.
#
# One file, two jobs. On a machine without the bot it installs it; on a machine that
# already has it, running the same command again opens a menu instead of cloning over
# a working deployment. That is why the script is safe to re-run — and why it is
# written to be reachable with `curl … | bash` and then repeatedly as `./install.sh`.
#
# Everything a human reads here is plain English on purpose: this runs on a bare
# terminal over SSH, where a Persian line is one missing font or one wrong locale away
# from arriving as boxes. The *bot* still speaks both languages — that is decided per
# user inside Telegram (DEFAULT_LANGUAGE in .env.example), not by the terminal.
#
# There is deliberately no global `set -e`. A menu that closes itself because one
# `docker compose ps` returned non-zero is worse than one that prints the failure and
# keeps the menu up: the operator's next step is almost always another menu action.
# Commands that must not be ignored are checked explicitly with `|| return 1`.
#
set -u

REPO_URL="${BOT_REPO_URL:-https://github.com/KanekiDevPro/telegram-downloader-bot.git}"
PROJECT_NAME="telegram-downloader-bot"

BOLD=$'\e[1m'
DIM=$'\e[2m'
RED=$'\e[31m'
GREEN=$'\e[32m'
YELLOW=$'\e[33m'
BLUE=$'\e[34m'
CYAN=$'\e[36m'
RESET=$'\e[0m'

# ---------------------------------------------------------------------------
# Talking to the terminal
# ---------------------------------------------------------------------------
#
# `curl … | bash` gives this script a *pipe* on stdin, so a `read` there answers
# itself with EOF and the menu spins. When there is a terminal to talk to, talk to it
# instead; when there is not (CI, `bash install.sh < /dev/null`), fall through to the
# non-interactive install path rather than pretending to ask.
if [ -t 0 ]; then
    TTY_IN=/dev/stdin
elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
    TTY_IN=/dev/tty
else
    TTY_IN=""
fi

#: The divider's width: the terminal's own when it can be measured (a fixed-width
#: rule wraps and garbles the box on a narrow SSH client — a phone), the classic
#: fixed width when it cannot (no tty, or no tput to ask).
rule_width() {
    local cols=""
    [ -n "$TTY_IN" ] || { printf '%s' 48; return 0; }
    command -v tput >/dev/null 2>&1 || { printf '%s' 48; return 0; }
    cols="$(tput cols 2>/dev/null)" || cols=""
    case "$cols" in
    '' | *[!0-9]*) printf '%s' 48 ;;
    *) printf '%s' "$cols" ;;
    esac
}

rule() {
    local width line
    width="$(rule_width)"
    printf -v line '%*s' "$width" ''
    printf '%b\n' "${BLUE}${line// /─}${RESET}"
}
say() { printf '%b\n' "$*"; }
ok() { printf '%b\n' "${GREEN}$*${RESET}"; }
warn() { printf '%b\n' "${YELLOW}$*${RESET}"; }
fail() { printf '%b\n' "${RED}$*${RESET}"; }

#: The last answer typed at a prompt (declared here so `set -u` has something to
#: read even when every prompt was skipped, as in the non-interactive install).
ASK_REPLY=""

#: The service an action names (the log picker sets it; default: bot).
SERVICE_PICK=""

#: Prompt, with an optional default. The answer lands in ASK_REPLY.
ask() {
    local prompt="$1" default="${2:-}" answer=""
    ASK_REPLY="$default"
    [ -z "$TTY_IN" ] && return 0
    printf '%b ' "${CYAN}${prompt}${RESET}" >&2
    IFS= read -r answer <"$TTY_IN" || answer=""
    answer="${answer%$'\r'}"   # a terminal that sends CRLF
    [ -n "$answer" ] && ASK_REPLY="$answer"
}

#: Prompt for a secret without echo — the answer lands in ASK_REPLY. A bot token
#: is a credential: it must never sit in the terminal scrollback, where a shared
#: screen or a recording hands it to everyone.
ask_secret() {
    local prompt="$1" answer=""
    ASK_REPLY=""
    [ -z "$TTY_IN" ] && return 0
    printf '%b ' "${CYAN}${prompt}${RESET}" >&2
    IFS= read -rs answer <"$TTY_IN" || answer=""
    printf '\n' >&2
    answer="${answer%$'\r'}"
    [ -n "$answer" ] && ASK_REPLY="$answer"
}

#: Yes/no, defaulting to *no*: the answers here include deleting volumes.
confirm() {
    ask "$1 [y/N]" ""
    case "$(printf '%s' "$ASK_REPLY" | tr '[:upper:]' '[:lower:]')" in
    y | yes) return 0 ;;
    *) return 1 ;;
    esac
}

pause() {
    [ -z "$TTY_IN" ] && return 0
    ask "Press ENTER to return to the dashboard" ""
}

# ---------------------------------------------------------------------------
# Docker, and where the project lives
# ---------------------------------------------------------------------------

has_docker() { command -v docker >/dev/null 2>&1; }

#: `docker compose` (v2 plugin) or the standalone `docker-compose`, whichever exists.
compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        fail "Neither 'docker compose' nor 'docker-compose' is installed."
        return 127
    fi
}

install_docker() {
    has_docker && return 0
    warn "Docker is not installed — installing it (needs root)."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y && apt-get install -y git docker.io docker-compose-v2 || return 1
        systemctl enable --now docker || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y git docker docker-compose-plugin || return 1
        systemctl enable --now docker || true
    else
        fail "No supported package manager — install Docker manually, then re-run."
        return 1
    fi
    has_docker && return 0
    fail "Docker still is not on PATH after installing it."
    return 1
}

#: True when the project exists at PROJECT_DIR and looks like this project.
installed() { [ -f "$PROJECT_DIR/docker-compose.yml" ] && [ -f "$PROJECT_DIR/main.py" ]; }

running_services() {
    local count
    count="$(compose ps --status running --services 2>/dev/null | grep -c . )" || count=0
    printf '%s' "${count:-0}"
}

#: The nine services this stack runs — docker-compose.yml is the source of truth.
#: The dashboard's status list and the log picker read this one list, so "the
#: service" can never mean two different things on two screens.
SERVICES=(postgres redis warp yt-session-generator telegram-api pot-provider cobalt cobalt-warp bot)

#: Per-service status: the "X/9 running" summary first, then one colored ●/○ per
#: service. One stopped service is a *partial* failure and must never read as
#: healthy — the old single aggregate line happily showed "8 running" in green.
service_status() {
    local running_list="" service mark color running=0 total="${#SERVICES[@]}" rows=()
    running_list="$(compose ps --status running --services 2>/dev/null)" || running_list=""
    for service in "${SERVICES[@]}"; do
        if printf '%s\n' "$running_list" | grep -qx "$service"; then
            mark="${GREEN}●"
            running=$((running + 1))
        else
            mark="${DIM}○"
        fi
        rows+=("     ${mark} ${service}${RESET}")
    done
    if [ "$running" -ge "$total" ]; then
        color="$GREEN"
    else
        color="$YELLOW"   # partial (or total) failure — exactly what this line shows
    fi
    printf '   %bstatus:%b  %b● %d/%d running%b\n' "$DIM" "$RESET" "$color" "$running" "$total" "$RESET"
    printf '%b\n' "${rows[@]}"
}

#: The revision this install runs — what "the installed version" means here.
#: .git is gone in some deployments (a copied tree, a release archive), and
#: "unknown" is the honest answer there, never a crash.
project_version() {
    local sha=""
    if [ -d "$PROJECT_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        sha="$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null)" || sha=""
    fi
    printf '%s' "${sha:-unknown}"
}

#: Decide where the project is before touching anything.
#:
#: Two ways in, and they need different answers: a piped `curl … | bash` has no
#: directory of its own, so the project is `./$PROJECT_NAME`, while `./install.sh`
#: from inside a checkout *is* the project and must never clone a second copy on top
#: of itself.
detect_layout() {
    local script_dir=""
    if [ -n "${BASH_SOURCE[0]:-}" ]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)" || script_dir=""
    fi
    if [ -n "$script_dir" ] && [ -f "$script_dir/docker-compose.yml" ]; then
        PROJECT_DIR="$script_dir"
        LAYOUT="checkout"
    elif [ -f "$PWD/$PROJECT_NAME/docker-compose.yml" ]; then
        PROJECT_DIR="$PWD/$PROJECT_NAME"
        LAYOUT="installed"
    else
        PROJECT_DIR="$PWD/$PROJECT_NAME"
        LAYOUT="fresh"
    fi
}

# ---------------------------------------------------------------------------
# 1. Install
# ---------------------------------------------------------------------------

write_env() {
    if [ -f .env ]; then
        ok "Keeping the existing .env (delete it and re-run to write a new one)."
        return 0
    fi
    local token="${BOT_TOKEN:-}" admins="${ADMIN_IDS:-}" card="" lang=""
    # Read silently, like a password prompt: the token is typed once and must
    # not end up in the terminal's scrollback.
    [ -z "$token" ] && ask_secret "Bot token from @BotFather (BOT_TOKEN, input hidden):" ""
    [ -z "$token" ] && token="$ASK_REPLY"
    ask "Admin Telegram IDs, comma separated (e.g. 1234,5678):" "${ADMIN_IDS:-}"
    admins="$ASK_REPLY"
    ask "Card number shown on the VIP payment screen (optional):" ""
    card="$ASK_REPLY"
    ask "Default language for new users [en/fa] (Enter = en):" "en"
    lang="$ASK_REPLY"

    if [ -z "$token" ]; then
        fail "A bot token is required (from @BotFather). Nothing was written."
        fail "Set it and re-run:  BOT_TOKEN=123:abc ./install.sh"
        return 1
    fi

    cat >.env <<EOF
BOT_TOKEN=$token
ADMIN_IDS=$admins
MANUAL_CARD_NUMBER=$card
DEFAULT_LANGUAGE=$lang
EOF
    chmod 600 .env 2>/dev/null || true
    ok "Wrote .env (only the settings that have no safe default; see .env.example)."
}

cookie_jar_step() {
    if [ -f cookies.txt ] && [ -s cookies.txt ]; then
        ok "cookies.txt is present — the running bot reloads it on its own."
        return 0
    fi
    say ""
    say "${DIM}The cookie jar is optional: everything that does not need a login works"
    say "without it, and the bot says so in its log instead of failing quietly.${RESET}"
    if [ -z "$TTY_IN" ]; then
        return 0
    fi
    confirm "Paste the contents of cookies.txt now?" || {
        say "${DIM}Skipped. Drop a cookies.txt here later — the bot picks it up live.${RESET}"
        return 0
    }
    say "Paste it, then press ENTER and CTRL+D."
    cat >cookies.txt <"$TTY_IN"
    [ -s cookies.txt ] && ok "Wrote cookies.txt." || warn "cookies.txt is empty — ignored."
}

#: The bind-mounted directory the fallback's cookie file is generated into.
#:
#: It is mounted into *two* containers (the bot writes, cobalt reads), and a bind
#: mount keeps the host's ownership — so a directory Docker creates as root is not
#: writable by the bot, which runs as uid 10001. That is the PermissionError this
#: line prevents; the bot also checks it at boot and names the fix if it ever breaks.
cobalt_dir_step() {
    mkdir -p cobalt 2>/dev/null && chmod 777 cobalt 2>/dev/null && return 0
    warn "Could not prepare ./cobalt — the fallback will run unsessioned until it is"
    warn "writable:  mkdir -p cobalt && chmod 777 cobalt"
    return 0
}

#: A soft pre-flight for the build: nine containers and a compile want room.
#: Advisory only — a thin box is warned about and then *still* installed on; the
#: numbers are shown so the operator knows what to upgrade, never to block.
preflight_resources() {
    local mem_kb="" disk_kb=""
    [ -r /proc/meminfo ] && mem_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)"
    if [ -z "$mem_kb" ] && command -v sysctl >/dev/null 2>&1; then
        mem_kb="$(sysctl -n hw.memsize 2>/dev/null | awk '{print int($1 / 1024)}')"
    fi
    disk_kb="$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')"
    case "$mem_kb" in
    '' | *[!0-9]*) ;; # nothing measured here — silence beats a made-up number
    *)
        if [ "$mem_kb" -lt $((2 * 1024 * 1024)) ]; then
            warn "Only $((mem_kb / 1024)) MB of RAM on this machine — 2 GB or more keeps"
            warn "this nine-container stack comfortable. Continuing anyway."
        fi
        ;;
    esac
    case "$disk_kb" in
    '' | *[!0-9]*) ;;
    *)
        if [ "$disk_kb" -lt $((5 * 1024 * 1024)) ]; then
            warn "Only $(awk -v k="$disk_kb" 'BEGIN { printf "%.1f", k / 1024 / 1024 }') GB of free disk here — images,"
            warn "builds and downloads want 5 GB or more. Continuing anyway."
        fi
        ;;
    esac
}

#: Build the stack with every line also written to a timestamped log in the
#: project dir — a failed build scrolls its evidence off the screen, and the
#: operator needs a file to inspect (or paste into an issue). `tee` is last in
#: the pipe, so the build's own exit code comes back via PIPESTATUS.
build_and_log() {
    local log_file="$1"
    say "${DIM}Build log: $log_file${RESET}"
    compose up -d --build 2>&1 | tee "$log_file"
    return "${PIPESTATUS[0]}"
}

do_install() {
    rule
    say "${BOLD}🚀 Install${RESET}"
    rule
    if installed; then
        warn "Already installed at $PROJECT_DIR."
        say "Use ${BOLD}[2] Update Bot${RESET} to pull the latest code and rebuild."
        return 0
    fi
    if [ "$LAYOUT" = "fresh" ] && [ -e "$PROJECT_DIR" ]; then
        fail "$PROJECT_DIR exists but does not look like this project."
        fail "Move it away, or run this script from inside a checkout."
        return 1
    fi
    if ! install_docker; then
        return 1
    fi

    if [ "$LAYOUT" = "fresh" ]; then
        command -v git >/dev/null 2>&1 || {
            fail "git is required to fetch the source."
            return 1
        }
        say "${GREEN}Downloading the bot's source into $PROJECT_DIR...${RESET}"
        git clone --depth 1 "$REPO_URL" "$PROJECT_DIR" || {
            fail "git clone failed — check the network and the repository URL."
            return 1
        }
    fi

    cd "$PROJECT_DIR" || return 1
    write_env || return 1
    cookie_jar_step
    cobalt_dir_step

    say ""
    preflight_resources
    say "${GREEN}Building and starting the containers...${RESET}"
    say "${DIM}(bot, PostgreSQL, Redis, local Bot API, Cobalt fallback, PO-token"
    say "provider, YouTube session server, WARP tunnel — the first build takes a while)${RESET}"
    local log_file="install-$(date +%Y%m%d-%H%M%S).log"
    build_and_log "$log_file" || {
        fail "docker compose failed to bring the stack up."
        say "The build log above names the service — then:  docker compose logs <service>"
        say "Everything the build printed is kept in:  $PROJECT_DIR/$log_file"
        return 1
    }
    print_next_steps
}

print_next_steps() {
    say ""
    rule
    ok "The bot is installed and running."
    rule
    say ""
    say "Next:"
    say "  • Send /start to your bot in Telegram."
    say "  • Open the admin panel with /admin (stats, health, queue, tools)."
    say "  • If YouTube refuses downloads, run /doctor — it names the cause and one fix."
    say "  • Downloads leave through a WARP tunnel by default (the 'warp' service), so"
    say "    a flagged VPS address is not what YouTube sees. /doctor prints the exit it"
    say "    came from; if the tunnel is down the bot runs direct and tells the admins."
    say "  • Logs:  docker compose logs -f bot"
    say "  • Menu:  ./install.sh   (update, restart, status, uninstall)"
}

# ---------------------------------------------------------------------------
# 2. Update
# ---------------------------------------------------------------------------

do_update() {
    rule
    say "${BOLD}🔄 Update${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    if [ -d .git ]; then
        local old_head="" new_head="" changes="" count=0
        old_head="$(git rev-parse --short HEAD 2>/dev/null)" || old_head=""
        say "${GREEN}Pulling the latest code...${RESET}"
        if ! git pull --ff-only; then
            fail "git pull failed (local changes, or a diverged branch)."
            say "Your files and containers are untouched. Resolve it with:"
            say "  git status   /   git stash   /   git pull --ff-only"
            return 1
        fi
        new_head="$(git rev-parse --short HEAD 2>/dev/null)" || new_head=""
        # "Updated." alone says nothing about *what* arrived — the commits
        # between the two heads are the honest answer, capped so one huge pull
        # cannot bury the menu.
        changes="$(git log --oneline "$old_head..HEAD" 2>/dev/null)" || changes=""
        count="$(printf '%s\n' "$changes" | grep -c .)"
        if [ -n "$old_head" ] && [ "$old_head" != "$new_head" ] && [ "${count:-0}" -gt 0 ]; then
            say ""
            say "${BOLD}What changed ($count commit(s), $old_head → $new_head):${RESET}"
            printf '%s\n' "$changes" | sed -n '1,10p' | sed 's/^/  • /'
            [ "$count" -gt 10 ] && say "  ${DIM}...and $((count - 10)) more — 'git log' has the rest${RESET}"
        fi
    else
        warn "No .git here — skipping the code update, rebuilding what is present."
    fi
    say "${GREEN}Rebuilding and restarting...${RESET}"
    local log_file="update-$(date +%Y%m%d-%H%M%S).log"
    build_and_log "$log_file" || {
        fail "docker compose failed. The stack is still running the previous build."
        say "Everything the build printed is kept in:  $PROJECT_DIR/$log_file"
        return 1
    }
    ok "Updated. A rebuilt image replaces the containers; data lives in volumes."
}

# ---------------------------------------------------------------------------
# 3. Start / restart, 4. Stop
# ---------------------------------------------------------------------------

do_start() {
    rule
    say "${BOLD}▶️  Start / Restart Services${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet — choose [1] Install Bot."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    local running
    running="$(running_services)"
    if [ "${running:-0}" -gt 0 ]; then
        say "${GREEN}Restarting $running running service(s)...${RESET}"
        compose restart || {
            fail "restart failed — 'docker compose ps' shows what state each service is in."
            return 1
        }
    else
        say "${GREEN}Starting the stack...${RESET}"
        compose up -d || {
            fail "start failed — see 'docker compose ps'."
            return 1
        }
    fi
    ok "$(running_services) service(s) running."
}

do_stop() {
    rule
    say "${BOLD}🛑 Stop Services${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    say "Stopping the containers. Volumes, the database and downloads are kept."
    compose down || {
        fail "stop failed — see 'docker compose ps'."
        return 1
    }
    ok "Stopped."
}

# ---------------------------------------------------------------------------
# 5. Status and logs
# ---------------------------------------------------------------------------

do_status() {
    rule
    say "${BOLD}📊 Status & Logs${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    if ! compose ps; then
        # One honest line instead of the daemon's error printed three times: this is
        # the most common dashboard failure (Docker Desktop closed, or no boot start).
        fail "Docker is not answering — start it, then try again:"
        say "  Linux:  sudo systemctl start docker"
        say "  macOS/Windows: open Docker Desktop"
        return 1
    fi
    say ""
    service_status
    [ -f .env ] || warn "No .env here — the bot will refuse to start without BOT_TOKEN."
    if [ -z "$TTY_IN" ]; then
        compose logs --tail=50 bot
        return 0
    fi
    say ""
    pick_service
    say ""
    if ! confirm "Follow $SERVICE_PICK's log now? (Ctrl+C comes back to this menu)"; then
        compose logs --tail=20 "$SERVICE_PICK" || true
        return 0
    fi
    # A trap, so Ctrl+C stops `docker compose logs` and returns here instead of
    # closing the whole script on the operator who only wanted to stop reading.
    trap 'printf "\n"; warn "Log view stopped."' INT
    compose logs --tail=50 -f "$SERVICE_PICK"
    trap - INT
}

#: Ask which service to act on; the answer lands in SERVICE_PICK (default: bot).
#: The choices are the SERVICES list — the same one the dashboard shows.
pick_service() {
    local service index=1
    say "Tail which service's logs?"
    for service in "${SERVICES[@]}"; do
        printf '   %b[%d]%b %s\n' "$BOLD" "$index" "$RESET" "$service"
        index=$((index + 1))
    done
    ask "Service [1-${#SERVICES[@]} or a name] (Enter = bot):" "bot"
    SERVICE_PICK="bot"
    case "$ASK_REPLY" in
    '' | bot) ;;
    *[!0-9]*)
        for service in "${SERVICES[@]}"; do
            [ "$ASK_REPLY" = "$service" ] && SERVICE_PICK="$service"
        done
        [ "$SERVICE_PICK" = "bot" ] && warn "No such service: $ASK_REPLY — using bot."
        ;;
    *)
        if [ "$ASK_REPLY" -ge 1 ] && [ "$ASK_REPLY" -le "${#SERVICES[@]}" ]; then
            SERVICE_PICK="${SERVICES[$((ASK_REPLY - 1))]}"
        else
            warn "Out of range: $ASK_REPLY — using bot."
        fi
        ;;
    esac
}

# ---------------------------------------------------------------------------
# 6. Run diagnostics
# ---------------------------------------------------------------------------

do_diagnostics() {
    rule
    say "${BOLD}🩺 Run Diagnostics${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    say "The bot's own boot checks, inside the bot container:"
    say "${DIM}scripts/boot_check.py — images, cookies, fonts, versions, mounts${RESET}"
    say ""
    if [ -n "$TTY_IN" ]; then
        compose exec bot python scripts/boot_check.py
    else
        # No terminal to allocate when this runs piped (`exec` fails on the
        # missing TTY before a single check gets to run).
        compose exec -T bot python scripts/boot_check.py
    fi || {
        fail "Diagnostics could not run — is the stack up? [3] Start / Restart Services."
        return 1
    }
    ok "Diagnostics finished."
}

# ---------------------------------------------------------------------------
# 7. Uninstall
# ---------------------------------------------------------------------------

do_uninstall() {
    rule
    say "${BOLD}🗑️  Uninstall${RESET}"
    rule
    installed || {
        warn "Nothing installed at $PROJECT_DIR yet."
        return 0
    }
    cd "$PROJECT_DIR" || return 1
    say "This removes the containers, the network, the database volume (users,"
    say "transactions, cache, telemetry) and the downloaded media."
    if ! confirm "Delete the containers and all data?"; then
        say "Cancelled — nothing was touched."
        return 0
    fi
    compose down -v || warn "Some containers may still be running — check 'docker compose ps'."

    if confirm "Also delete the project directory $PROJECT_DIR (code, .env, cookies)?"; then
        cd "$(dirname "$PROJECT_DIR")" || return 1
        # The directory is only *proposed*, never guessed at: PROJECT_DIR was decided
        # at start-up and shown to the operator before this question.
        say "Removing $PROJECT_DIR ..."
        rm -rf "$PROJECT_DIR" || {
            fail "Could not remove it — delete it manually: rm -rf $PROJECT_DIR"
            return 1
        }
        ok "Removed. Re-run this script any time to install from scratch."
        return 0
    fi
    ok "Containers and data removed. $PROJECT_DIR was kept."
}

# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------

show_dashboard() {
    clear 2>/dev/null || true
    # A few lines of ASCII above the box — the name in a form that renders even
    # on a terminal with no emoji font at all (unlike the menu below).
    printf '%b\n' "${BOLD}${CYAN}  .-----------------------------.${RESET}"
    printf '%b\n' "${BOLD}${CYAN}  |   TELEGRAM DOWNLOADER BOT   |${RESET}"
    printf '%b\n' "${BOLD}${CYAN}  '-----------------------------'${RESET}"
    rule
    printf '%b\n' "${BOLD}${BLUE}   Telegram Downloader Bot — control center${RESET}"
    rule
    printf '%b\n' "   ${DIM}project:${RESET} $PROJECT_DIR"
    printf '%b\n' "   ${DIM}version:${RESET} $(project_version)"
    service_status
    rule
    say ""
    printf '%b\n' "   ${BOLD}[1]${RESET} 🚀 Install Bot ${DIM}(already installed — updates instead)${RESET}"
    printf '%b\n' "   ${BOLD}[2]${RESET} 🔄 Update Bot ${DIM}(git pull + rebuild)${RESET}"
    printf '%b\n' "   ${BOLD}[3]${RESET} ▶️  Start / Restart Services"
    printf '%b\n' "   ${BOLD}[4]${RESET} 🛑 Stop Services"
    printf '%b\n' "   ${BOLD}[5]${RESET} 📊 View Status & Logs"
    printf '%b\n' "   ${BOLD}[6]${RESET} 🩺 Run Diagnostics"
    printf '%b\n' "   ${BOLD}[7]${RESET} 🗑️  Uninstall"
    printf '%b\n' "   ${BOLD}[0]${RESET} ❌ Exit ${DIM}(or q)${RESET}"
    say ""
    rule
}

menu_loop() {
    while true; do
        show_dashboard
        ask "Choose an option [0-7]:" "0"
        say ""
        case "$ASK_REPLY" in
        1) do_install ;;
        2) do_update ;;
        3) do_start ;;
        4) do_stop ;;
        5) do_status ;;
        6) do_diagnostics ;;
        7) do_uninstall ;;
        0 | q | quit | exit) ok "Bye."; exit 0 ;;
        *) warn "Unknown option: $ASK_REPLY" ;;
        esac
        # An uninstall that removed the directory cannot keep walking a menu that
        # belongs to it.
        installed || exit 0
        cd "$PROJECT_DIR" 2>/dev/null || exit 0
        pause
    done
}

usage() {
    cat <<EOF
Telegram Downloader Bot — installer and control center.

  ./install.sh              install (first run) or open the dashboard
  ./install.sh install      install
  ./install.sh update       git pull + rebuild
  ./install.sh start        start / restart the stack
  ./install.sh stop         stop the stack
  ./install.sh status       containers and the last log lines
  ./install.sh diagnostics  run the bot's boot checks in the container
  ./install.sh uninstall    stop, delete volumes, optionally delete the directory

  ./install.sh --version    print the revision this installer belongs to
  ./install.sh --help       this text

  curl -fsSL <raw-url>/install.sh | bash     install a fresh server
EOF
}

main() {
    detect_layout

    case "${1:-}" in
    -h | --help)
        usage
        exit 0
        ;;
    -v | --version)
        say "Telegram Downloader Bot — installer and control center (revision $(project_version))"
        exit 0
        ;;
    install | update | start | stop | status | diagnostics | uninstall)
        local action="$1"
        if [ "$action" = "install" ] || installed; then
            "do_$action"
            exit $?
        fi
        fail "Nothing installed at $PROJECT_DIR yet — run 'install' first."
        exit 1
        ;;
    "") ;;
    *)
        fail "Unknown argument: $1"
        usage
        exit 1
        ;;
    esac

    if installed; then
        cd "$PROJECT_DIR" || exit 1
        menu_loop
    elif [ -z "$TTY_IN" ]; then
        # No terminal to offer a menu on (`curl | bash` into a pipeline, CI): install,
        # which is what this script did before it had a menu.
        do_install
    else
        clear 2>/dev/null || true
        rule
        printf '%b\n' "${BOLD}${BLUE}   Telegram Downloader Bot — no installation found here${RESET}"
        rule
        say "   ${DIM}starting the installation flow${RESET}"
        say ""
        do_install
    fi
}

main "$@"
