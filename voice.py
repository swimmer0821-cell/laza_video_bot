"""Озвучка Лазы: русский — ElevenLabs v3 (голос из /voice), узбекский — Azure (носитель языка) или ElevenLabs."""
import logging
import re
from html import escape
from pathlib import Path

import httpx

import db
import fal_api
import media
from config import AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, AZURE_UZ_VOICE

# Ударение в имени на последнюю гласную: Лаза́, Лазу́, Лазы́, Лазе́, Лазо́й (знак ударения U+0301)
_NAME_RU = re.compile(r"\b(Лаз)([аеуыо])(й?)\b", re.IGNORECASE)
_LATIN_IN_RU = re.compile(r"\b(LAZA|Laza)\b")


def stress_ru(text: str) -> str:
    text = _LATIN_IN_RU.sub("Лаза", text)
    return _NAME_RU.sub(lambda m: m.group(1) + m.group(2) + "́" + m.group(3), text)


def strip_tags(text: str) -> str:
    """Убирает метки интонации [excited] — Azure их не понимает."""
    return re.sub(r"\s*\[[^\]]*\]\s*", " ", text).strip()


def uz_engine() -> str:
    return "Azure · Sardor" if AZURE_SPEECH_KEY else f"ElevenLabs · {db.get_voice()} (с акцентом)"


def engine(lang: str) -> str:
    return uz_engine() if lang == "uz" else f"ElevenLabs · {db.get_voice()}"


async def _azure(text: str) -> Path:
    ssml = (f"<speak version='1.0' xml:lang='uz-UZ'><voice name='{AZURE_UZ_VOICE}'>"
            f"<prosody rate='+4%'>{escape(strip_tags(text))}</prosody></voice></speak>")
    url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3", "User-Agent": "laza-video-bot"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, content=ssml.encode("utf-8"), headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        logging.exception("Azure TTS ошибка")
        raise fal_api.GenError("Узбекская озвучка (Azure) не ответила. Проверьте ключ AZURE_SPEECH_KEY.") from e
    dst = media.new_path("mp3")
    dst.write_bytes(r.content)
    return dst


async def speak(text: str, lang: str) -> Path:
    """Готовый mp3 с репликой Лазы."""
    if lang == "uz" and AZURE_SPEECH_KEY:
        return await _azure(text)
    if lang == "ru":
        text = stress_ru(text)
    url = await fal_api.tts(text, lang, db.get_voice())
    return await media.download(url, "mp3")
