#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=== Elisoncha Doc: установка бота ==="

# 1. Виртуальное окружение
if [ ! -d "venv" ]; then
    echo "Создаю виртуальное окружение..."
    python3 -m venv venv
fi

echo "Устанавливаю зависимости..."
source venv/bin/activate
pip install -q -r requirements.txt

# 2. Inbox
mkdir -p ../inbox

# 3. Logs
mkdir -p logs

# 4. Делаю start.sh исполняемым
chmod +x start.sh

# 5. launchd
PLIST_NAME="com.elisoncha-doc.bot"
PLIST_SRC="$DIR/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

# Подставляем реальный путь в plist
sed "s|__BOT_DIR__|$DIR|g" "$PLIST_SRC" > "$PLIST_DST"

# Перезагружаем сервис
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "=== Готово! ==="
echo "Бот запущен как фоновый сервис."
echo "Логи: $DIR/logs/"
echo "Inbox: $DIR/../inbox/"
echo ""
echo "Управление:"
echo "  Остановить: launchctl unload ~/Library/LaunchAgents/$PLIST_NAME.plist"
echo "  Запустить:  launchctl load ~/Library/LaunchAgents/$PLIST_NAME.plist"
