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
HF_KEY = os.getenv("HF_KEY", "")              # Higgsfield API «id:секрет»; библиотека читает его сама из окружения
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
    even: bool = False  # модель принимает только чётные секунды (LTX-2.3: 6, 8, 10)

    def parts(self, total: int) -> list[int]:
        """Делит длину ролика на части, которые модель умеет за один запуск."""
        if self.fixed_part:
            return [self.fixed_part] * max(1, math.ceil(total / self.fixed_part))
        n = max(1, math.ceil(total / self.max_part))
        base, extra = divmod(total, n)
        parts = [base + (1 if i < extra else 0) for i in range(n)]
        parts = [max(self.min_part, p) for p in parts]
        if self.even:
            parts = [min(self.max_part, p + p % 2) for p in parts]
        return parts

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
    # Тест с 27.10.2026: LTX-2.3 Fast — 1080p, со звуком, $0.06/с, длина только 6/8/10 с за запуск
    "ltx": VideoModel("ltx", "Дёшево · LTX-2.3 со звуком", "fal-ai/ltx-2.3/image-to-video/fast",
                      10, 6, 0, 0.06, 0, "со звуком, 1080p", even=True),
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

# Естественные голоса Google Gemini TTS (ключ из Google AI Studio). Модель можно сменить в .env.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TTS_MODELS = [m for m in (os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview"),
                                 "gemini-2.5-flash-preview-tts") if m]
GEMINI_VOICES = {"Puck": "бодрый муж.", "Fenrir": "азартный муж.", "Orus": "уверенный муж.",
                 "Zephyr": "яркий жен.", "Leda": "юный жен.", "Aoede": "лёгкий жен."}
GEMINI_DEFAULT = "Puck"

# Синхронизация губ: sync.so lipsync-2, $3 за минуту
LIPSYNC_MODEL = "fal-ai/sync-lipsync/v2"
LIPSYNC_USD_PER_SEC = 0.05

# Ролик с Лазой из нескольких сцен: всегда Kling 3.0 (держит персонажа, без своего звука)
SCENE_MODEL = "kling"
SCENE_PLANS = {15: (3, 5), 30: (6, 5)}      # длина ролика → (сцен, секунд в сцене); смена картинки каждые 5 с
SCENE_TEMPLATES = {   # сюжеты по ядру бренда (эмоции → удовольствие → вирусность): код → (кнопка, сюжет)
    "heat": ("🥵 Лаза против жары",
             "Комедия: Ташкент, чилля, +42. Всё вокруг плавится — люди, асфальт, обычное мороженое, даже кондиционер "
             "сдался. И только Лаза бодрый, в кепке, хрустит кубиком и подбадривает всех. Финал-панчлайн: «Жара? "
             "Не слышал». Без объяснений технологии."),
    "crunch": ("🎧 Хруст", "ASMR: крупным планом кубики Laza, Лаза медленно ломает кубик — громкий сухой хруст, "
                          "крошки, довольное лицо. Минимум слов, главный герой — звук и удовольствие. Финал — пачка Laza."),
    "guess": ("🤔 Угадай вкус", "Лаза с завязанными глазами пробует кубик и пытается угадать вкус (крем-брюле? фисташка? "
                               "фруктовый микс?), смешно ошибается, интрига до конца — в последней сцене раскрывает "
                               "правильный ответ с пачкой этого вкуса."),
    "moments": ("🎒 Вместо конфет", "Три жизненных момента, где вместо конфет и шоколадки — хрустящие кубики Laza: "
                                    "пара в университете, пробка в машине, дорога/путешествие (или мама с ребёнком). "
                                    "В каждом Лаза появляется неожиданно и угощает. Лёгкий юмор, узнаваемая жизнь."),
    "plov": ("🍽 После плова", "Семейный дастархан, все объелись плова, на сладкое «места нет». Лаза ставит на стол "
                              "лёгкие хрустящие кубики — и все тянутся ещё. Тёплый семейный юмор, узнаваемый Ташкент."),
    "magic": ("✨ Мир Лазы (анимация)", "Сказочная мультяшная анимация вместо цеха: волшебная морозная страна, где "
                                       "Лаза превращает облака сливок и ягоды в хрустящие кубики — красиво, без "
                                       "технологии и цифр. Финал: кубики падают в пачку Laza в реальном мире."),
}
SOUND_TEMPLATES = {"crunch"}   # сцены без голоса — моделью со звуком (Seedance), чтобы был слышен хруст
SCENE_TEMPLATE = SCENE_TEMPLATES["heat"][1]
SETTINGS_PATH = DATA_DIR / "settings.json"


# «Повтор тренда»: Higgsfield Genjutsu Light — движения из чужого видео переносятся на персонажа с фото.
# Цены с open.higgsfield.ai на 27.10.2026: сейчас скидка 50%, без скидки вдвое дороже.
# Для лимита и показа берём цену БЕЗ скидки, чтобы не превысить дневной лимит, если скидка закончится.
GENJUTSU_MODEL = "higgsfield/genjutsu-light/motion-transfer/v1.0"
GENJUTSU_USD_PER_SEC = {"480p": 0.05, "720p": 0.10, "1080p": 0.20}
GENJUTSU_MAX_SEC = 15                         # модель берёт только первые 15 секунд видео
