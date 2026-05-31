#!/usr/bin/env bash
# Деплой на свой VPS. Хост и путь задаются переменными окружения —
# в репозитории НЕТ ничьих адресов.
#
#   VPS_HOST=user@your-server VPS_PATH=/opt/elisoncha-doc ./deploy.sh
#
set -euo pipefail

VPS_HOST="${VPS_HOST:?Задайте VPS_HOST, напр. user@your-server}"
VPS_PATH="${VPS_PATH:-/opt/elisoncha-doc}"

echo ">> Push to git remote"
git push

echo ">> Pull on VPS and rebuild container"
ssh "$VPS_HOST" "cd $VPS_PATH && git pull && docker compose up -d --build"

echo ">> Tail logs (Ctrl+C to exit)"
ssh "$VPS_HOST" "cd $VPS_PATH && docker compose logs --tail=50 -f bot"
