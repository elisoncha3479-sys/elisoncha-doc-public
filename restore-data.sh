#!/usr/bin/env bash
# Восстановление личных данных из приватного кармана обратно в
# контейнер. Зеркало backup-data.sh. Личных данных в скрипте нет.
#
#   BACKUP_REMOTE=git@github.com:you/your-data.git \
#   CONTAINER=elisoncha-doc-bot-1 \
#   ./restore-data.sh
#
# ВНИМАНИЕ: перезаписывает данные в контейнере содержимым кармана.
# Запускать осознанно (после потери VPS / пересоздания контейнера).
#
set -euo pipefail

BACKUP_REMOTE="${BACKUP_REMOTE:?Задайте BACKUP_REMOTE — git-url приватного репо данных}"
CONTAINER="${CONTAINER:?Задайте CONTAINER — имя docker-контейнера бота}"
APP_DIR="${APP_DIR:-/app}"
WORKDIR="${WORKDIR:-$HOME/.doc-data-backup}"
DATA_DIRS="${DATA_DIRS:-data specialists history}"

if [ ! -d "$WORKDIR/.git" ]; then
  echo ">> Клонирую приватный карман в $WORKDIR"
  git clone "$BACKUP_REMOTE" "$WORKDIR"
else
  cd "$WORKDIR"
  git pull --ff-only origin "$(git symbolic-ref --short HEAD)"
fi

read -r -p "Перезаписать данные в контейнере $CONTAINER из кармана? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "Отменено."; exit 0; }

for d in $DATA_DIRS; do
  if [ -d "$WORKDIR/$d" ]; then
    echo ">> Возвращаю '$d' в контейнер"
    docker cp "$WORKDIR/$d/." "$CONTAINER:$APP_DIR/$d/"
  fi
done
echo ">> Готово. Перезапусти контейнер, если требуется: docker compose restart"
