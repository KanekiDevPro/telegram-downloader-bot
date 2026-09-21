#!/bin/bash
#
# Telegram Downloader Bot — one-shot installer for a fresh Linux VPS.
#
# Everything a human reads here is plain English on purpose: this runs on a bare
# terminal over SSH, where a Persian/emoji line is one missing font or one wrong
# locale away from arriving as boxes. The *bot* still speaks both languages — that
# is decided per user inside Telegram (see DEFAULT_LANGUAGE in .env.example), not by
# what the terminal can render.
#
set -e

BLUE="\e[34m"
GREEN="\e[32m"
YELLOW="\e[33m"
RESET="\e[0m"

line() { echo -e "${BLUE}=======================================${RESET}"; }

clear
line
echo -e "${BLUE}  Telegram Downloader Bot — installer  ${RESET}"
line

# 1. Source. Public repository, so no credentials are needed.
echo -e "${GREEN}[1/5] Downloading the bot's source...${RESET}"
rm -rf telegram-downloader-bot
git clone https://github.com/KanekiDevPro/telegram-downloader-bot.git
cd telegram-downloader-bot

# 2. Docker, if this machine does not have it yet.
if ! command -v docker &> /dev/null; then
    echo -e "${GREEN}[2/5] Installing Docker...${RESET}"
    apt update -y && apt install -y git docker.io docker-compose-v2
    systemctl enable --now docker
else
    echo -e "${GREEN}[2/5] Docker is already installed.${RESET}"
fi

# 3. The settings only the operator has.
echo -e "${YELLOW}[3/5] Creating .env${RESET}"
read -r -p "Bot token from @BotFather (BOT_TOKEN): " bot_token
read -r -p "Admin Telegram IDs, comma separated (e.g. 1234,5678): " admin_ids
read -r -p "Card number shown on the VIP payment screen: " card_num
read -r -p "Default language for new users [en/fa] (Enter = en): " default_language

cat <<EOF > .env
BOT_TOKEN=$bot_token
ADMIN_IDS=$admin_ids
MANUAL_CARD_NUMBER=$card_num
DEFAULT_LANGUAGE=${default_language:-en}
EOF

# 4. The cookie jar. Optional: everything that does not need a login works without
#    it, and the bot says so in its log instead of failing quietly.
echo -e "${YELLOW}[4/5] Cookie jar (optional — needed for most YouTube links)${RESET}"
echo "Paste the contents of cookies.txt, then press ENTER and CTRL+D."
echo "Leave it empty (just press CTRL+D) to skip: a fresh export can be dropped in"
echo "later and the running bot picks it up without a restart."
cat > cookies.txt

# 5. Up.
echo -e "${BLUE}[5/5] Starting the containers (bot, PostgreSQL, Redis, local Bot API,"
echo -e "      Cobalt fallback, PO-token provider, YouTube session server)...${RESET}"
docker compose up -d --build

line
echo -e "${GREEN}The bot is installed and running.${RESET}"
line
echo ""
echo "Next:"
echo "  • Send /start to your bot in Telegram."
echo "  • Open the admin panel with /admin (stats, health, queue, tools)."
echo "  • If YouTube refuses downloads, run /doctor — it names the cause and the fix."
echo "  • Logs:  docker compose logs -f bot"
echo "  • Stop:  docker compose down"
