"""Озвучка Лазы.
Есть ключ Azure → оба языка голосами-носителями Azure (русский Dmitry, узбекский Sardor; меняются в /voice).
Нет ключа → ElevenLabs v3 (голос из /voice, по умолчанию Liam; узбекский с акцентом)."""
import logging
import re
from html import escape
from pathlib import Path

import httpx

import db
import fal_api
import media
from config import AZURE_DEFAULT, AZURE_PITCH, AZURE_RATE, AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, AZURE_VOICES

# Ударение в имени на последнюю гласную: Лаза́, Лазу́, Лазы́, Лазе́, Лазо́й
_NAME_RU = re.compile(r"\b(Лаз)([аеуыо])(й?)\b", re.IGNORECASE)
_LATIN_IN_RU = re.compile(r"\b(LAZA|Laza)\b")
_IPA = {"а": "lɐˈza", "у": "lɐˈzu", "ы": "lɐˈzɨ", "е": "lɐˈzʲe", "о": "lɐˈzoj"}   # произношение для Azure


def use_azure() -> bool:
    return bool(AZURE_SPEECH_KEY)


def stress_ru(text: str) -> str:
    text = _LATIN_IN_RU.sub("Лаза", text)
    return _NAME_RU.sub(lambda m: m.group(1) + m.group(2) + "́" + m.group(3), text)


def strip_tags(text: str) -> str:
    """Убирает метки интонации [excited] — Azure их не понимает."""
    return re.sub(r"\s*\[[^\]]*\]\s*", " ", text).strip()


def azure_voice(lang: str) -> str:
    return db.get_setting(f"azure_{lang}", AZURE_DEFAULT[lang])


def azure_title(lang: str) -> str:
    v = azure_voice(lang)
    return next((t for t, n in AZURE_VOICES[lang].items() if n == v), v)


def engine(lang: str) -> str:
    if use_azure():
        return f"Azure · {azure_title(lang)}"
    return f"ElevenLabs · {db.get_voice()}" + (" (с акцентом)" if lang == "uz" else "")


def _ssml(text: str, lang: str, voice_name: str, phonemes: bool) -> str:
    body = escape(strip_tags(text))
    if lang == "ru":
        if phonemes:   # точное ударение через произношение
            def ph(m: re.Match) -> str:
                return f"<phoneme alphabet='ipa' ph='{_IPA[m.group(2).lower()]}'>{m.group(0)}</phoneme>"
            body = _NAME_RU.sub(ph, _LATIN_IN_RU.sub("Лаза", body))
        else:
            body = stress_ru(body)
    loc = "ru-RU" if lang == "ru" else "uz-UZ"
    return (f"<speak version='1.0' xml:lang='{loc}'><voice name='{voice_name}'>"
            f"<prosody rate='{AZURE_RATE}' pitch='{AZURE_PITCH}'>{body}</prosody></voice></speak>")


async def _azure(text: str, lang: str, voice_name: str | None = None) -> Path:
    voice_name = voice_name or azure_voice(lang)
    url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3", "User-Agent": "laza-video-bot"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, content=_ssml(text, lang, voice_name, True).encode("utf-8"), headers=headers)
            if r.status_code == 400:   # если произношение не поддержано — ударение знаком
                logging.warning("Azure отклонил phoneme, повтор без него")
                r = await c.post(url, content=_ssml(text, lang, voice_name, False).encode("utf-8"), headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        logging.exception("Azure TTS ошибка")
        code = getattr(getattr(e, "response", None), "status_code", 0)
        hint = "Проверьте AZURE_SPEECH_KEY и AZURE_SPEECH_REGION в .env." if code in (401, 403) else \
            "Попробуйте ещё раз."
        raise fal_api.GenError(f"Озвучка Azure не удалась. {hint}") from e
    dst = media.new_path("mp3")
    dst.write_bytes(r.content)
    return dst


async def speak(text: str, lang: str, voice_name: str | None = None) -> Path:
    """Готовый mp3 с репликой Лазы."""
    if use_azure():
        return await _azure(text, lang, voice_name)
    if lang == "ru":
        text = stress_ru(text)
    url = await fal_api.tts(text, lang, voice_name or db.get_voice())
    return await media.download(url, "mp3")
