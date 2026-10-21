"""Запросы к fal.ai: загрузка файлов, генерация картинок и видео."""
import logging
import random
from pathlib import Path

import fal_client

from config import IMAGE_MODEL, LIPSYNC_MODEL, TTS_MODEL, VideoModel


class GenError(Exception):
    """Ошибка генерации с понятным текстом для пользователя."""


def _human(e: Exception) -> str:
    t = str(e).lower()
    if "balance" in t or "insufficient" in t or "payment" in t or "402" in t:
        return "На fal.ai закончились деньги. Пополните баланс: fal.ai → Billing."
    if "401" in t or "unauthorized" in t or "key" in t and "invalid" in t:
        return "Ключ fal.ai не подходит. Проверьте FAL_KEY в настройках бота."
    if "content" in t and ("policy" in t or "safety" in t or "nsfw" in t):
        return "Модель отклонила запрос фильтром безопасности. Измените идею или фото."
    return "Сервис генерации вернул ошибку. Попробуйте ещё раз или другую модель."


async def upload(path: Path) -> str:
    return await fal_client.upload_file_async(path)


async def _call(endpoint: str, args: dict) -> dict:
    logging.info("fal.ai → %s %s", endpoint, {k: v for k, v in args.items() if k != "prompt"})
    try:
        return await fal_client.subscribe_async(endpoint, arguments=args, interval=3,
                                                client_timeout=1200)
    except Exception as e:  # noqa: BLE001 — переводим любую ошибку в понятный текст
        logging.exception("fal.ai ошибка")
        raise GenError(_human(e)) from e


async def video_part(model: VideoModel, image_url: str, prompt: str, seconds: int) -> str:
    """Одна часть ролика: стартовый кадр + промт → ссылка на mp4."""
    if model.key == "wan":
        args = {"image_url": image_url, "prompt": prompt, "resolution": "720p", "aspect_ratio": "9:16",
                "video_quality": "high"}
    elif model.key == "kling":
        args = {"image_url": image_url, "prompt": prompt, "duration": str(seconds), "generate_audio": False}
    else:  # seedance
        args = {"image_url": image_url, "prompt": prompt, "duration": str(seconds),
                "resolution": "1080p" if model.key == "seed1080" else "720p",
                "aspect_ratio": "9:16", "generate_audio": True, "seed": random.randint(1, 2**31 - 1)}
    res = await _call(model.endpoint, args)
    try:
        return res["video"]["url"]
    except (KeyError, TypeError) as e:
        raise GenError("Сервис не вернул видео. Попробуйте ещё раз.") from e


async def character_image(ref_urls: list[str], prompt: str) -> str:
    """Картинка с персонажем по эталонам → ссылка на png."""
    args = {"prompt": prompt, "image_urls": ref_urls, "aspect_ratio": "9:16",
            "resolution": "1K", "num_images": 1, "output_format": "jpeg"}
    res = await _call(IMAGE_MODEL, args)
    try:
        return res["images"][0]["url"]
    except (KeyError, IndexError, TypeError) as e:
        raise GenError("Сервис не вернул картинку. Попробуйте ещё раз.") from e


OUTPAINT_PROMPT = ("Extend this photo to a vertical 9:16 frame. Keep the original photo content exactly as it is "
                   "(same products, colors, lighting, angle, texture) and naturally continue the same surface and "
                   "background above and below. Do not add new objects, text, logos or people. Photorealistic.")


async def outpaint_vertical(image_url: str) -> str:
    """Достраивает горизонтальное фото до вертикали 9:16 (Nano Banana Pro, $0.15)."""
    args = {"prompt": OUTPAINT_PROMPT, "image_urls": [image_url], "aspect_ratio": "9:16",
            "resolution": "2K", "num_images": 1, "output_format": "jpeg"}
    res = await _call(IMAGE_MODEL, args)
    try:
        return res["images"][0]["url"]
    except (KeyError, IndexError, TypeError) as e:
        raise GenError("Не получилось достроить фото.") from e


async def tts(text: str, lang: str, voice: str) -> str:
    """Озвучка ElevenLabs v3 → ссылка на mp3."""
    args = {"text": text, "voice": voice, "language_code": lang, "stability": 0.5}
    res = await _call(TTS_MODEL, args)
    try:
        return res["audio"]["url"]
    except (KeyError, TypeError) as e:
        raise GenError("Сервис озвучки не вернул звук. Попробуйте ещё раз.") from e


async def lipsync(video_url: str, audio_url: str) -> str:
    """Синхронизация губ персонажа с голосом (sync.so lipsync-2) → ссылка на mp4."""
    res = await _call(LIPSYNC_MODEL, {"video_url": video_url, "audio_url": audio_url, "sync_mode": "cut_off"})
    try:
        return res["video"]["url"]
    except (KeyError, TypeError) as e:
        raise GenError("Синхронизация губ не удалась. Попробуйте ещё раз.") from e
