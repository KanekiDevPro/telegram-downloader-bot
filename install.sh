#!/bin/bash
set -e
clear
echo -e "\e[34m=======================================\e[0m"
echo -e "\e[34m  Telegram Downloader Bot Installer    \e[0m"
echo -e "\e[34m=======================================\e[0m"

# ۱. کلون کردن مستقیم بدون نیاز به رمز (چون ریپو پابلیک است)
echo -e "\e[32m⏳ در حال دانلود سورس ربات...\e[0m"
rm -rf telegram-downloader-bot
git clone https://github.com/KanekiDevPro/telegram-downloader-bot.git
cd telegram-downloader-bot

# ۲. نصب داکر اگر وجود نداشت
if ! command -v docker &> /dev/null; then
    echo -e "\e[32m🐳 در حال نصب داکر...\e[0m"
    apt update -y && apt install -y git docker.io docker-compose-v2
    systemctl enable --now docker
fi

# ۳. ساخت فایل .env به صورت تعاملی
echo -e "\e[33m⚙️ ساخت فایل .env\e[0m"
read -p "توکن ربات (BOT_TOKEN): " bot_token
read -p "آیدی ادمین‌ها (مثال: 1234,5678): " admin_ids
read -p "شماره کارت برای خرید VIP: " card_num

cat <<EOF > .env
BOT_TOKEN=$bot_token
ADMIN_IDS=$admin_ids
MANUAL_CARD_NUMBER=$card_num
EOF

# ۴. دریافت کوکی از کاربر
echo -e "\e[33m🍪 محتویات فایل cookies.txt را اینجا پیست کنید\e[0m"
echo -e "(بعد از پیست کردن، دکمه ENTER و سپس CTRL+D را بزنید):"
cat > cookies.txt

# ۵. استارت نهایی کانتینرها
echo -e "\e[34m🚀 در حال راه‌اندازی کانتینرها...\e[0m"
docker compose up -d --build

echo -e "\e[32m=======================================\e[0m"
echo -e "\e[32m🔥 ربات با موفقیت نصب و روشن شد!\e[0m"
echo -e "\e[32m=======================================\e[0m"