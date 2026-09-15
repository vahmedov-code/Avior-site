#!/bin/bash
# Деплой сайта AVIOR: бампает версию (REV) на +0.1 во всех html-страницах,
# коммитит и пушит в git, затем синхронизирует файлы на боевой сервер.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

SSH_KEY="$HOME/.ssh/avior_deploy"
SERVER="root@77.221.151.131"
REMOTE_PATH="/var/www/avior"

CURRENT=$(grep -oE 'REV [0-9]+\.[0-9]+' index.html | head -1 | sed 's/REV //')
if [ -z "$CURRENT" ]; then
  echo "Не удалось найти текущую версию (REV x.x) в index.html" >&2
  exit 1
fi
NEW=$(awk -v v="$CURRENT" 'BEGIN{printf "%.1f", v+0.1}')

echo "Версия: $CURRENT -> $NEW"

for f in $(grep -rl "REV $CURRENT ·" --include="*.html" .); do
  sed -i "s/REV $CURRENT ·/REV $NEW ·/" "$f"
done

git add -A -- '*.html'
if ! git diff --cached --quiet; then
  git commit -m "chore: bump version to REV $NEW

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
fi

git push origin main

echo "Бэкап текущей версии на сервере..."
ssh -i "$SSH_KEY" "$SERVER" "mkdir -p /root/backups && tar czf /root/backups/avior-\$(date +%Y%m%d-%H%M%S).tar.gz -C /var/www avior"

echo "Синхронизация файлов на сервер..."
FILES=$(git ls-files | grep -v '^\.github/')
git archive HEAD -- $FILES | ssh -i "$SSH_KEY" "$SERVER" "rm -rf /root/deploy/staging && mkdir -p /root/deploy/staging && tar -x -C /root/deploy/staging && rsync -a /root/deploy/staging/ $REMOTE_PATH/ && chown -R www-data:www-data $REMOTE_PATH"

echo "Готово. Задеплоена версия REV $NEW на avior.moscow"
