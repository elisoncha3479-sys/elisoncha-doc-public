#!/usr/bin/env bash
# Бэкап личных данных в отдельный ПРИВАТНЫЙ git-репозиторий —
# третье место хранения (компьютер/код : VPS : этот карман).
#
# В этом скрипте НЕТ ничьих адресов и НЕТ личных данных — только
# механизм. Всё задаётся переменными окружения (как deploy.sh).
#
# Данные живут внутри контейнера под root, поэтому вынимаются через
# `docker cp` на хост, затем коммитятся и пушатся в приватный репо.
#
#   BACKUP_REMOTE=git@github.com:you/your-data.git \
#   CONTAINER=elisoncha-doc-bot-1 \
#   ./backup-data.sh
#
set -euo pipefail

BACKUP_REMOTE="${BACKUP_REMOTE:?Задайте BACKUP_REMOTE — git-url приватного репо данных}"
CONTAINER="${CONTAINER:?Задайте CONTAINER — имя docker-контейнера бота}"
APP_DIR="${APP_DIR:-/app}"
WORKDIR="${WORKDIR:-$HOME/.doc-data-backup}"
# Что бэкапим: персона+реестр, память врачей, медкарта.
DATA_DIRS="${DATA_DIRS:-data specialists history}"

if [ ! -d "$WORKDIR/.git" ]; then
  echo ">> Первый запуск: клонирую приватный карман в $WORKDIR"
  git clone "$BACKUP_REMOTE" "$WORKDIR"
fi

cd "$WORKDIR"
git pull --ff-only origin "$(git symbolic-ref --short HEAD)" 2>/dev/null || true

for d in $DATA_DIRS; do
  echo ">> Вынимаю '$d' из контейнера $CONTAINER"
  rm -rf "${WORKDIR:?}/$d"
  mkdir -p "$WORKDIR/$d"
  # точка в конце пути копирует содержимое каталога
  docker cp "$CONTAINER:$APP_DIR/$d/." "$WORKDIR/$d/" 2>/dev/null \
    || echo "   (в контейнере нет $APP_DIR/$d — пропуск, это нормально пока пусто)"
done

git add -A
if git diff --cached --quiet; then
  echo ">> Изменений нет — карман уже актуален."
  exit 0
fi

STAMP="$(date '+%Y-%m-%d %H:%M')"
git commit -q -m "backup $STAMP"
git push -q
echo ">> Готово: карта здоровья сохранена в приватный карман ($STAMP)."
