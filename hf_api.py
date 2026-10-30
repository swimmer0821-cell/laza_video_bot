"""Запросы к Higgsfield API (оплата за каждую генерацию, без подписки).

Ключ — HF_KEY в .env в виде «id_ключа:секрет» (open.higgsfield.ai → API Keys).
Сейчас отсюда работает «Повтор тренда»: Genjutsu Light переносит движения из чужого видео
на нашего персонажа или человека с фото. Звук исходного видео сохраняется.
Ещё через Higgsfield идёт кнопка «Хорошо · Kling 3.0» (вдвое дешевле, чем на fal.ai), если задан HF_KEY.
"""
import logging
from pathlib import Path

from config import GENJUTSU_MODEL, HF_KEY, VIDEO_MODELS
from fal_api import GenError


def enabled() -> bool:
    return bool(HF_KEY and ":" in HF_KEY)


def _human(e: Exception) -> str:
    t = str(e).lower()
    if "credit" in t or "balance" in t or "insufficient" in t or "402" in t:
        return "На балансе Higgsfield API закончились деньги. Пополните: open.higgsfield.ai → Billing."
    if "401" in t or "403" in t or "unauthorized" in t or "credential" in t:
        return "Ключ Higgsfield не подходит. Проверьте HF_KEY в настройках бота."
    if "nsfw" in t or "safety" in t or "moderation" in t:
        return "Higgsfield отклонил видео или фото фильтром безопасности. Попробуйте другое."
    return "Higgsfield вернул ошибку. Попробуйте ещё раз или другое видео."


def _video_url(res) -> str:
    """Ссылка на готовое видео: в ответе бывает video{url}, videos[{url}] или url."""
    st = str(res.get("status", "")).lower() if isinstance(res, dict) else ""
    if st == "nsfw":
        raise GenError("Higgsfield отклонил видео фильтром безопасности (слишком откровенно или похоже на "
                       "реального человека/знаменитость). Деньги за отказ не списываются. "
                       "Попробуйте другое видео: одетый человек, без крупных планов тела.")
    if st in ("failed", "cancelled", "canceled", "error"):
        err = str(res.get("error") or "")
        if err:
            msg = _human(Exception(err))
            if "закончились деньги" in msg:
                msg = ("На балансе Higgsfield API не хватает денег на этот ролик (цены в боте могут быть "
                       "ниже настоящих). Пополните: open.higgsfield.ai → Billing, или выберите качество "
                       "пониже / видео короче. Деньги за отказ не списываются.")
            raise GenError(msg)
        raise GenError("Higgsfield не смог сделать видео. Попробуйте ещё раз или другое видео.")
    if isinstance(res, dict):
        v = res.get("video")
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
        vs = res.get("videos")
        if isinstance(vs, list) and vs and isinstance(vs[0], dict) and vs[0].get("url"):
            return vs[0]["url"]
        if isinstance(res.get("url"), str):
            return res["url"]
        for key in ("output", "result", "data"):
            if key in res:
                return _video_url(res[key])
    raise GenError("Higgsfield не вернул видео. Попробуйте ещё раз.")


async def motion_transfer(video: Path, image: Path, resolution: str, prompt: str = "") -> str:
    """Движение из video → персонаж с image. Возвращает ссылку на mp4."""
    import higgsfield_client  # библиотека ставится вместе с ботом (requirements.txt)
    try:
        video_url = await higgsfield_client.upload_file_async(video)
        image_url = await higgsfield_client.upload_file_async(image)
        args = {"prompt": prompt, "video_url": video_url, "image_urls": [image_url], "resolution": resolution}
        logging.info("Higgsfield → %s %s", GENJUTSU_MODEL, {"resolution": resolution})
        res = await higgsfield_client.subscribe_async(GENJUTSU_MODEL, arguments=args)
    except GenError:
        raise
    except Exception as e:  # noqa: BLE001 — переводим любую ошибку в понятный текст
        logging.exception("Higgsfield ошибка")
        raise GenError(_human(e)) from e
    logging.info("Higgsfield ответ: %s", str(res)[:500])
    return _video_url(res)


async def kling(image_url: str, prompt: str, seconds: int) -> str:
    """Kling 3.0 Standard через Higgsfield: картинка (публичная ссылка) + промт → mp4 без звука."""
    import higgsfield_client
    endpoint = VIDEO_MODELS["kling"].endpoint
    args = {"image_url": image_url, "prompt": prompt, "duration": max(3, min(15, int(seconds))), "sound": "off"}
    try:
        logging.info("Higgsfield → %s %s", endpoint, {"duration": args["duration"]})
        res = await higgsfield_client.subscribe_async(endpoint, arguments=args)
    except Exception as e:  # noqa: BLE001
        logging.exception("Higgsfield ошибка")
        raise GenError(_human(e)) from e
    logging.info("Higgsfield ответ: %s", str(res)[:500])
    return _video_url(res)
