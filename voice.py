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

# Ударение в имени на последнюю гласную: Лаза́, Лазу́, Лазы́, Лазе́, Лазо́й.
# Как именно «подсказать» ударение движку — выбирается на слух командой /stress (разные способы).
_NAME_RU = re.compile(r"\b(Лаз)([аеуыо])(й?)\b", re.IGNORECASE)
_LATIN_IN_RU = re.compile(r"\b(LAZA|Laza)\b")
_NAME_UZ = re.compile(r"\b(Laz)(a)(?=\b|')", re.IGNORECASE)
ACC = "\u0301"
_IPA_RU = {"а": "lɐˈza", "у": "lɐˈzu", "ы": "lɐˈzɨ", "е": "lɐˈzʲe", "о": "lɐˈzoj"}
_IPA_RU2 = {"а": "laˈza", "у": "laˈzu", "ы": "laˈzɨ", "е": "laˈzʲe", "о": "laˈzoj"}

# способ → подпись. ipa* работают только в Azure.
STRESS_WAYS = {
    "ru": {"ipa": "произношение IPA (lɐˈza)", "ipa2": "произношение IPA (laˈza)", "mark": "знак ударения Лаза́",
           "dbl": "двойная буква Лазаа", "hyph": "через дефис Ла-за́", "plain": "как есть (без подсказки)"},
    "uz": {"plain": "как есть", "ipa": "произношение IPA (laˈza)", "mark": "знак ударения Lazá",
           "dbl": "двойная буква Lazaa"},
}
STRESS_DEFAULT = {"ru": "ipa", "uz": "plain"}


def stress_way(lang: str) -> str:
    return db.get_setting(f"stress_{lang}", STRESS_DEFAULT[lang])


def _mark_name(text: str, lang: str, way: str, azure: bool) -> str:
    """Расставляет подсказку ударения в имени выбранным способом."""
    if not azure and way.startswith("ipa"):
        way = "mark"
    if lang == "ru":
        text = _LATIN_IN_RU.sub("Лаза", text)

        def f(m: re.Match) -> str:
            base, v, tail = m.group(1), m.group(2), m.group(3)
            if way == "ipa" or way == "ipa2":
                ipa = (_IPA_RU if way == "ipa" else _IPA_RU2)[v.lower()]
                return f"<phoneme alphabet='ipa' ph='{ipa}'>{m.group(0)}</phoneme>"
            if way == "mark":
                return base + v + ACC + tail
            if way == "dbl":
                return base + v + v.lower() + tail
            if way == "hyph":
                return base[:2] + "-" + base[2:] + v + ACC + tail
            return m.group(0)
        return _NAME_RU.sub(f, text)

    def g(m: re.Match) -> str:
        if way == "ipa":
            end = m.end()
            if end < len(m.string) and m.string[end] == "'":   # Laza'ni — внутри слова IPA нельзя
                return m.group(1) + "á"
            return f"<phoneme alphabet='ipa' ph='laˈza'>{m.group(0)}</phoneme>"
        if way == "mark":
            return m.group(1) + "á"
        if way == "dbl":
            return m.group(1) + "aa"
        return m.group(0)
    return _NAME_UZ.sub(g, text)


def use_azure() -> bool:
    return bool(AZURE_SPEECH_KEY)


def stress_ru(text: str) -> str:
    return _mark_name(text, "ru", "mark", False)


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


def _ssml(text: str, lang: str, voice_name: str, way: str) -> str:
    body = _mark_name(escape(strip_tags(text), quote=False), lang, way, True)
    loc = "ru-RU" if lang == "ru" else "uz-UZ"
    return (f"<speak version='1.0' xml:lang='{loc}'><voice name='{voice_name}'>"
            f"<prosody rate='{AZURE_RATE}' pitch='{AZURE_PITCH}'>{body}</prosody></voice></speak>")


async def _azure(text: str, lang: str, voice_name: str | None = None, way: str | None = None) -> Path:
    voice_name = voice_name or azure_voice(lang)
    way = way or stress_way(lang)
    url = f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {"Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY, "Content-Type": "application/ssml+xml",
               "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3", "User-Agent": "laza-video-bot"}
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(url, content=_ssml(text, lang, voice_name, way).encode("utf-8"), headers=headers)
            if r.status_code == 400 and way.startswith("ipa"):   # произношение не поддержано — знаком
                logging.warning("Azure отклонил phoneme, повтор со знаком ударения")
                r = await c.post(url, content=_ssml(text, lang, voice_name, "mark").encode("utf-8"), headers=headers)
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


async def speak(text: str, lang: str, voice_name: str | None = None, way: str | None = None) -> Path:
    """Готовый mp3 с репликой Лазы."""
    if use_azure():
        return await _azure(text, lang, voice_name, way)
    text = _mark_name(text, lang, way or stress_way(lang), False)
    url = await fal_api.tts(text, lang, voice_name or db.get_voice())
    return await media.download(url, "mp3")
