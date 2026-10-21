# LAZA Видео — Telegram-бот

Ролики для Reels/Shorts из фото товара и анимированный персонаж Лаза.
Генерация — fal.ai (Kling 3.0, Seedance 2.0, Wan 2.2, Nano Banana Pro), голос — ElevenLabs v3 (русский, узбекский),
синхронизация губ — sync.so lipsync-2, сценарии и реплики пишет Claude.

- Сервер: `/opt/laza_video_bot`, служба `laza_video_bot`.
- Обновить после изменений на GitHub: в Termius на сервере выполнить `laza-video-update`.
- Настройки и ключи — только в `/opt/laza_video_bot/.env` на сервере (в GitHub их нет).
- Лог: `tail -25 /opt/laza_video_bot/logs/bot.log`.
