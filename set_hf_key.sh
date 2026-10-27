#!/usr/bin/env bash
# Записать ключ Higgsfield API в .env бота «LAZA Видео».
# Запуск на сервере:  bash /opt/laza_video_bot/set_hf_key.sh
# Спрашивает ID и Secret (Secret при вставке не показывается на экране), проверяет их вид,
# заменяет строку HF_KEY в .env и перезапускает бота. Сам ключ на экран не выводится.
set -uo pipefail
ENV=/opt/laza_video_bot/.env

echo "Вставьте ID ключа Higgsfield (вида xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx) и нажмите Enter:"
read -r ID
echo "Вставьте Secret (на экране он не появится — это нормально) и нажмите Enter:"
read -rs SECRET
echo

# Убираем пробелы, кавычки и переносы, которые мог добавить iPad
clean() { printf '%s' "$1" | tr -d " \t\r\n'\"‘’“”«»"; }
ID=$(clean "$ID")
SECRET=$(clean "$SECRET")

if ! [[ "$ID" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
  echo "⚠️ ID не похож на ключ (должно быть 5 групп через дефис, всего 36 знаков; сейчас знаков: ${#ID})."
  echo "Ничего не записано. Скопируйте ID ещё раз и запустите команду снова."
  exit 1
fi
if [[ ${#SECRET} -lt 20 || "$SECRET" == *:* ]]; then
  echo "⚠️ Secret слишком короткий или с лишними знаками (знаков: ${#SECRET}). Ничего не записано."
  exit 1
fi

cp "$ENV" "$ENV.bak_$(date +%Y%m%d_%H%M%S)"
grep -v '^HF_KEY=' "$ENV" > "$ENV.tmp" || true
printf 'HF_KEY=%s:%s\n' "$ID" "$SECRET" >> "$ENV.tmp"
mv "$ENV.tmp" "$ENV"
chmod 600 "$ENV"
unset ID SECRET
echo "✅ Ключ записан (ID 36 знаков, Secret принят). Обновляю бота…"
command -v laza-video-update >/dev/null && laza-video-update || bash /opt/laza_video_bot/update.sh
