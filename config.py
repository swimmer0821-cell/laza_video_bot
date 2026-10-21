"""Настройки бота «LAZA Видео»: ключи из .env, модели и их цены."""
import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _ids(name: str) -> set[int]:
    """Список Telegram-id из .env через запятую."""
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
FAL_KEY = os.getenv("FAL_KEY", "")            # fal_client читает его сам из окружения
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

ADMIN_IDS = _ids("ADMIN_IDS")                 # могут добавлять людей и менять эталоны
ALLOWED_IDS = _ids("ALLOWED_IDS")             # могут пользоваться ботом (плюс те, кого добавили командой /add)
DAILY_LIMIT_USD = float(os.getenv("DAILY_LIMIT_USD", "20"))   # общий лимит трат за сутки

DATA_DIR = BASE_DIR / "data"
TMP_DIR = DATA_DIR / "tmp"
REFS_DEFAULT_DIR = BASE_DIR / "refs"          # эталоны персонажа, идут вместе с ботом
REFS_CUSTOM_DIR = DATA_DIR / "refs"           # эталоны, добавленные админом через бота
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "video_bot.db"
for d in (DATA_DIR, TMP_DIR, REFS_CUSTOM_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Картинка с персонажем: Nano Banana Pro (редактирование по эталонам), $0.15 за картинку 1K
IMAGE_MODEL = "fal-ai/nano-banana-pro/edit"
IMAGE_PRICE_USD = 0.15


@dataclass(frozen=True)
class VideoModel:
    key: str            # короткий код для кнопок
    title: str          # как называется в боте
    endpoint: str       # id модели на fal.ai
    max_part: int       # максимум секунд за один запуск
    min_part: int       # минимум секунд за один запуск
    fixed_part: int     # если модель делает только одну длину (Wan Turbo), иначе 0
    usd_per_sec: float  # цена за секунду (если 0 — считаем за запуск)
    usd_per_run: float  # цена за запуск (Wan Turbo)
    note: str           # подсказка в кнопке

    def parts(self, total: int) -> list[int]:
        """Делит длину ролика на части, которые модель умеет за один запуск."""
        if self.fixed_part:
            return [self.fixed_part] * max(1, math.ceil(total / self.fixed_part))
        n = max(1, math.ceil(total / self.max_part))
        base, extra = divmod(total, n)
        parts = [base + (1 if i < extra else 0) for i in range(n)]
        return [max(self.min_part, p) for p in parts]

    def price(self, total: int) -> float:
        """Оценка стоимости ролика в долларах."""
        ps = self.parts(total)
        if self.usd_per_run:
            return round(self.usd_per_run * len(ps), 2)
        return round(self.usd_per_sec * sum(ps), 2)


# Цены — с сайта fal.ai на 21.10.2026 (Seedance и Kling — точные, Wan Turbo — по справочникам, ≈)
VIDEO_MODELS: dict[str, VideoModel] = {
    "wan": VideoModel("wan", "Черновик · Wan 2.2 Turbo", "fal-ai/wan/v2.2-a14b/image-to-video/turbo",
                      5, 5, 5, 0, 0.10, "без звука, 720p"),
    "kling": VideoModel("kling", "Хорошо · Kling 3.0", "fal-ai/kling-video/o3/standard/image-to-video",
                        15, 3, 0, 0.084, 0, "без звука"),
    "seed": VideoModel("seed", "Отлично · Seedance 2.0", "bytedance/seedance-2.0/fast/image-to-video",
                       15, 4, 0, 0.2419, 0, "со звуком, 720p"),
    "seed1080": VideoModel("seed1080", "Максимум · Seedance 2.0 1080p", "bytedance/seedance-2.0/image-to-video",
                           15, 4, 0, 0.682, 0, "со звуком, 1080p"),
}

DURATIONS = [5, 10, 15, 30]

# Озвучка: ElevenLabs v3 (русский и узбекский), $0.10 за 1000 символов
TTS_MODEL = "fal-ai/elevenlabs/tts/eleven-v3"
TTS_USD_PER_1K = 0.10
DEFAULT_VOICE = os.getenv("LAZA_VOICE", "Liam")
VOICE_CHOICES = ["Liam", "Charlie", "Will", "Callum", "Lily", "Jessica"]   # из встроенных голосов ElevenLabs
LANGS = {"ru": "🇷🇺 Русский", "uz": "🇺🇿 O'zbekcha"}

# Узбекский без акцента: Microsoft Azure, голос носителя языка (бесплатно до 500 000 символов в месяц).
# Пока ключа нет — узбекский озвучивает ElevenLabs тем же голосом, что и русский.
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "northeurope")
AZURE_VOICES = {   # голоса-носители языка: код языка → {название: голос Azure}
    "ru": {"Dmitry (муж.)": "ru-RU-DmitryNeural", "Dariya (жен.)": "ru-RU-DariyaNeural",
           "Svetlana (жен.)": "ru-RU-SvetlanaNeural"},
    "uz": {"Sardor (муж.)": "uz-UZ-SardorNeural", "Madina (жен.)": "uz-UZ-MadinaNeural"},
}
AZURE_DEFAULT = {"ru": "ru-RU-DmitryNeural", "uz": "uz-UZ-SardorNeural"}
AZURE_PITCH = os.getenv("AZURE_PITCH", "+6%")   # чуть выше — голос моложе, как у мультперсонажа
AZURE_RATE = os.getenv("AZURE_RATE", "+4%")

# Говорящая сцена: Kling AI Avatar v2 — картинка + голос → видео, губы точно по звуку. $0.0562 за секунду
AVATAR_MODEL = "fal-ai/kling-video/ai-avatar/v2/standard"
AVATAR_USD_PER_SEC = 0.0562

# Синхронизация губ: sync.so lipsync-2, $3 за минуту
LIPSYNC_MODEL = "fal-ai/sync-lipsync/v2"
LIPSYNC_USD_PER_SEC = 0.05

# Ролик с Лазой из нескольких сцен: всегда Kling 3.0 (держит персонажа, без своего звука)
SCENE_MODEL = "kling"
SCENE_PLANS = {15: (3, 5), 30: (3, 10)}      # длина ролика → (сцен, секунд в сцене)
SCENE_TEMPLATE = ("Лаза приветствует зрителя и представляется → показывает цех, где из натуральных сливок "
                  "делают сублимированное мороженое → угощает гостей на дегустации в офисе и зовёт заказать")
SETTINGS_PATH = DATA_DIR / "settings.json"
