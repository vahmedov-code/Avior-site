#!/bin/bash
# Установка бота как системной службы (без Docker). Запускать от root из папки price-bot.
# Скрипт только создаёт новое: пользователя avior-bot, файлы внутри этой папки и службу avior-bot.
# Системные пакеты не устанавливает и ничего существующего не удаляет и не перезаписывает.
set -euo pipefail
cd "$(dirname "$0")"

stop() { echo "СТОП: $*"; echo "Ничего не изменено в системе."; exit 1; }

python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null \
  || stop "нужен Python 3.10+, сейчас $(python3 --version 2>&1)"
python3 -c 'import ensurepip, venv' 2>/dev/null \
  || stop "нет модуля venv. Установите его сами: apt install python3-venv — и запустите скрипт снова"
[ -e /etc/systemd/system/avior-bot.service ] && stop "служба avior-bot уже существует"
[ -e .venv ] && stop "папка .venv уже есть — похоже, бот уже установлен"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
id avior-bot >/dev/null 2>&1 || useradd --system --home /opt/avior --shell /usr/sbin/nologin avior-bot
[ -f .env ] || cp .env.example .env
mkdir -p data
chown -R avior-bot:avior-bot data .env
chmod 600 .env

cp avior-bot.service /etc/systemd/system/avior-bot.service
systemctl daemon-reload
systemctl enable avior-bot >/dev/null 2>&1
echo "Готово. Впишите BOT_TOKEN в $(pwd)/.env и запустите: systemctl restart avior-bot"
