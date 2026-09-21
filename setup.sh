#!/bin/bash
set -e

# تنظیم رنگ‌ها برای خروجی جذاب‌تر ترمینال
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' 

echo -e "${BLUE}=======================================${NC}"
echo -e "${BLUE}  Telegram Downloader Bot - Installer  ${NC}"
echo -e "${BLUE}=======================================${NC}"

# ۱. بررسی و نصب داکر
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}🐳 Installing Docker and Git...${NC}"
    sudo apt update -y
    sudo apt install -y git docker.io docker-compose-v2
    sudo systemctl enable --now docker
else
    echo -e "${GREEN}✓ Docker is already installed.${NC}"
fi

# ۲. ساخت فایل .env به صورت تعاملی
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚙️ Setting up .env file...${NC}"
    read -p "Enter BOT_TOKEN (from BotFather): " bot_token
    read -p "Enter ADMIN_IDS (comma separated, e.g. 12345678): " admin_ids
    read -p "Enter MANUAL_CARD_NUMBER (for VIP): " card_num
    
    cat <<EOF > .env
BOT_TOKEN=$bot_token
ADMIN_IDS=$admin_ids
MANUAL_CARD_NUMBER=$card_num
EOF
    echo -e "${GREEN}✓ .env file generated successfully.${NC}"
else
    echo -e "${GREEN}✓ .env file already exists.${NC}"
fi

# ۳. دریافت هوشمند کوکی‌ها
if [ ! -f cookies.txt ]; then
    echo -e "${YELLOW}🍪 cookies.txt is missing!${NC}"
    echo "Please paste the contents of your cookies.txt below."
    echo -e "${YELLOW}(After pasting, press ENTER, then press CTRL+D to save):${NC}"
    cat > cookies.txt
    echo -e "${GREEN}✓ cookies.txt saved securely.${NC}"
else
    echo -e "${GREEN}✓ cookies.txt already exists.${NC}"
fi

# ۴. پرتاب نهایی کانتینرها
echo -e "${BLUE}🚀 Building and starting containers...${NC}"
docker compose up -d --build

echo -e "${GREEN}=======================================${NC}"
echo -e "${GREEN}🔥 BOOM! Your bot is live and running!${NC}"
echo -e "${GREEN}Type 'docker compose logs -f bot' to see the logs.${NC}"
echo -e "${GREEN}=======================================${NC}"