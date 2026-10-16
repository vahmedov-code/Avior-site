#!/bin/bash
# Установка бота как системной службы (без Docker). Запускать от root из папки price-bot.
set -euo pipefail
cd "$(dirname "$0")"

python3 -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'
apt-get install -y python3-venv >/dev/null

id avior-bot >/dev/null 2>&1 || useradd --system --home /opt/avior --shell /usr/sbin/nologin avior-bot
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
mkdir -p data
chown -R avior-bot:avior-bot data .env
chmod 600 .env

cp avior-bot.service /etc/systemd/system/avior-bot.service
systemctl daemon-reload
systemctl enable avior-bot >/dev/null
echo "Готово. Впишите BOT_TOKEN в $(pwd)/.env и запустите: systemctl restart avior-bot"
