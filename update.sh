#!/usr/bin/env bash
# Обновление бота «LAZA Видео» с GitHub. На сервере запускается командой: laza-video-update
# Файл .env, база расходов data/, логи logs/ и библиотеки .venv не трогаются.
set -euo pipefail
APP=/opt/laza_video_bot
cd "$APP"
echo "== Скачиваю новую версию с GitHub"
git fetch -q origin main
git reset -q --hard origin/main
echo "== Версия: $(git log -1 --format='%h %ad %s' --date=format:'%d.%m.%Y %H:%M')"
echo "== Библиотеки"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "== Перезапуск"
systemctl restart laza_video_bot
sleep 5
systemctl is-active laza_video_bot >/dev/null && echo "✅ Бот работает" || echo "⚠️ Бот не запустился"
tail -3 logs/bot.log 2>/dev/null || true
