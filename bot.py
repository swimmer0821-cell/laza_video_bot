"""Бот «LAZA Видео»: ролики из фото товара и персонаж Лаза. Генерация — fal.ai, промты — Claude."""
import asyncio
import html
import logging
import math
import re
import shutil
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

import db
import fal_api
import hf_api
import media
import prompts
import voice
from config import (ADMIN_IDS, BOT_TOKEN, DAILY_LIMIT_USD, DATA_DIR, DURATIONS, IMAGE_PRICE_USD, LANGS,
                    LIPSYNC_USD_PER_SEC, LOG_DIR, AZURE_VOICES, REFS_CUSTOM_DIR, REFS_DEFAULT_DIR, SCENE_MODEL, SCENE_PLANS, AVATAR_USD_PER_SEC,
                    SCENE_TEMPLATE, SCENE_TEMPLATES, GEMINI_API_KEY, GEMINI_VOICES, SOUND_TEMPLATES, TTS_USD_PER_1K, VIDEO_MODELS, VOICE_CHOICES,
                    GENJUTSU_MAX_SEC, GENJUTSU_USD_PER_SEC, MOTION_FAL_USD_PER_SEC, TREND_LENGTHS)

# ---------- Лог: в файл (5 МБ × 3) и в консоль ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000,
                                                  backupCount=3, encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("video_bot")

router = Router()
BUSY: set[int] = set()          # у кого сейчас идёт генерация (одна за раз на человека)

BTN_VIDEO, BTN_CHAR, BTN_SPENT, BTN_HELP = "🎬 Видео из фото", "🧸 Персонаж Лаза", "💰 Расходы", "❓ Помощь"
BTN_SCENES = "🎞 Ролик с Лазой (сцены)"
BTN_TREND = "🕺 Повтор тренда"
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_VIDEO), KeyboardButton(text=BTN_CHAR)],
                                     [KeyboardButton(text=BTN_SCENES)]
                                     + [KeyboardButton(text=BTN_TREND)],
                                     [KeyboardButton(text=BTN_SPENT), KeyboardButton(text=BTN_HELP)]],
                           resize_keyboard=True)
NOT_MENU = ~F.text.in_({BTN_VIDEO, BTN_CHAR, BTN_SCENES, BTN_TREND, BTN_SPENT, BTN_HELP})   # кнопки меню работают всегда
LAST_DIR = DATA_DIR / "last"          # последний ролик каждого человека (для озвучки и синхронизации губ)
LAST_DIR.mkdir(parents=True, exist_ok=True)
SENT_DIR = DATA_DIR / "sent"          # копии последних отправленных роликов (смотрит Claude через сервер LAZA)
SENT_DIR.mkdir(parents=True, exist_ok=True)
SCN_DIR = DATA_DIR / "scenes"         # картинки и клипы сцен последнего ролика (для второй языковой версии)
SCN_DIR.mkdir(parents=True, exist_ok=True)
OTHER = {"ru": "uz", "uz": "ru"}


class Flow(StatesGroup):
    photo = State()        # ждём фото товара
    idea = State()         # ждём идею ролика
    char_idea = State()    # ждём идею сцены с персонажем
    char_photo = State()   # ждём фото товара для сцены (или «без фото»)
    pick = State()         # выбор длины / модели / подтверждение
    ref = State()          # админ присылает эталон персонажа
    voice_text = State()   # ждём свой текст для озвучки
    sc_idea = State()      # ждём идею ролика из сцен
    pack = State()         # ждём фото настоящей пачки (или «пропустить»)
    trend_video = State()  # «Повтор тренда»: ждём видео с движением
    trend_photo = State()  # «Повтор тренда»: ждём фото того, кто будет двигаться


# ---------- Доступ ----------
class Allowed(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        ok = db.is_allowed(event.from_user.id)
        if not ok:
            log.warning("Нет доступа: %s %s", event.from_user.id, event.from_user.full_name)
            text = (f"Нет доступа к боту. Ваш Telegram id: {event.from_user.id}\n"
                    "Перешлите его руководителю, чтобы он добавил вас.")
            if isinstance(event, Message):
                await event.answer(text)
            else:
                await event.answer(text, show_alert=True)
        return ok


class Admin(BaseFilter):
    async def __call__(self, event: Message) -> bool:
        return event.from_user.id in ADMIN_IDS


class AntiDoubleClick(BaseMiddleware):
    """Игнорирует одинаковые нажатия одного человека в течение 2 секунд."""
    def __init__(self, ttl: float = 2.0):
        self.ttl, self.recent = ttl, {}

    async def __call__(self, handler, event: CallbackQuery, data):
        key, now = (event.from_user.id, event.data), time.monotonic()
        if now - self.recent.get(key, 0) < self.ttl:
            await event.answer("Уже обрабатываю…")
            return
        self.recent[key] = now
        return await handler(event, data)



async def send_vid(bot: Bot, chat_id: int, file: FSInputFile, **kw):
    """Отправка видео с размером и длительностью — иначе Telegram показывает вертикаль растянутой."""
    path = Path(file.path)
    try:
        w, h = await media.dims(path)
        kw.setdefault("width", w)
        kw.setdefault("height", h)
        kw.setdefault("duration", int(round(await media.duration(path))))
    except Exception:  # noqa: BLE001
        log.warning("Не удалось узнать размер видео")
    res = await bot.send_video(chat_id, file, **kw)
    _keep_sent(path, file.filename or path.name, kw)
    return res


def _keep_sent(path: Path, name: str, kw: dict) -> None:
    """Копия отправленного ролика в data/sent (последние 6) — чтобы Claude мог посмотреть результат."""
    try:
        for old in sorted(SENT_DIR.glob("*.mp4"))[:-5]:
            old.unlink(missing_ok=True)
        dst = SENT_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
        shutil.copy(path, dst)
        log.info("SENT %s %sx%s %s с | %s", dst.name, kw.get("width"), kw.get("height"), kw.get("duration"),
                 (kw.get("caption") or "").replace("\n", " ")[:200])
    except Exception:  # noqa: BLE001
        log.warning("Не сохранил копию ролика")

def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)
                                                  for t, d in row] for row in rows])


def usd(x: float) -> str:
    return f"{x:.2f} $"


def refs() -> list[Path]:
    """Эталоны персонажа: добавленные админом, иначе стандартные. Не больше 6."""
    custom = sorted(REFS_CUSTOM_DIR.glob("*.jpg"))
    return (custom or sorted(REFS_DEFAULT_DIR.glob("*.jpg")))[:6]


async def save_photo(bot: Bot, msg: Message) -> Path:
    """Скачивает самое большое фото (или картинку-файл) из сообщения."""
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    dst = media.new_path("jpg")
    await bot.download(file_id, destination=dst)
    return dst


def is_image(msg: Message) -> bool:
    return bool(msg.photo) or bool(msg.document and (msg.document.mime_type or "").startswith("image/"))


# ---------- Старт и меню ----------
@router.message(CommandStart(), Allowed())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    log.info("start %s %s", msg.from_user.id, msg.from_user.full_name)
    await msg.answer("Привет! Я делаю короткие ролики для Reels и Shorts.\n\n"
                     f"{BTN_VIDEO} — пришлите фото товара, получите видео 5–30 с.\n"
                     f"{BTN_CHAR} — Лаза в любой сцене, потом оживляем.\n"
                     f"{BTN_SCENES} — ролик 15–30 с из 3 сцен с голосом Лазы.\n"
                     + f"{BTN_TREND} — пришлите трендовое видео, и Лаза или человек с фото повторит движения.\n"
                     + "🔊 Любой готовый ролик можно озвучить на русском или узбекском.\n\n"
                     "Перед каждым запуском я покажу цену и спрошу подтверждение.", reply_markup=MENU)


@router.message(F.text == BTN_HELP, Allowed())
@router.message(Command("help"), Allowed())
async def help_(msg: Message):
    lines = ["Как пользоваться:",
             "1. Выберите режим кнопкой внизу.",
             "2. Опишите идею по-русски, простыми словами. Промт на английском напишет Claude.",
             "3. Выберите длину и качество, проверьте цену, нажмите «Запустить».",
             "4. Ролик готов через 2–10 минут (30 с — до 20 минут).",
             "",
             "Модели и цены (за 5 секунд):"]
    lines += [f"• {m.title} — ≈ {usd(m.price(5))} ({m.note})" for m in VIDEO_MODELS.values()]
    lines += [f"• Картинка с персонажем — ≈ {usd(IMAGE_PRICE_USD)}",
              "• Озвучка — ≈ 0,01–0,03 $ за реплику",
              f"• Синхронизация губ — ≈ {usd(LIPSYNC_USD_PER_SEC * 5)} за 5 с"]
    lines += [f"• Повтор тренда (Kling 2.6, fal.ai) — {usd(MOTION_FAL_USD_PER_SEC * 5)} за 5 с"]
    if hf_api.enabled():
        lines += [f"• Повтор тренда (Higgsfield), 480p — до {usd(GENJUTSU_USD_PER_SEC['480p'] * 5)} за 5 с"]
    lines += ["",
              "Советы: фото товара — чёткое, при хорошем свете, товар крупно. "
              "Надписи, цены и логотип добавляйте потом в CapCut: ИИ искажает текст.",
              f"Лимит трат на всех за сутки: {usd(DAILY_LIMIT_USD)}."]
    if msg.from_user.id in ADMIN_IDS:
        lines += ["", "Для руководителя:", "/add id Имя — дать доступ", "/del id — убрать доступ",
                  "/users — список", "/refs — эталоны персонажа", "/ref_add — добавить эталон",
                  "/ref_reset — вернуть стандартные эталоны",
                  f"/voice — голос Лазы (сейчас: {voice.engine('ru')})",
                  "/stress — ударение в имени Лаза (выбрать на слух)"]
    await msg.answer("\n".join(lines))


@router.message(F.text == BTN_SPENT, Allowed())
async def spent(msg: Message):
    await msg.answer("💰 Расходы на генерации\n\n" + db.report())


@router.message(Command("cancel"), Allowed())
async def cancel_cmd(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=MENU)


# ---------- Режим 1: видео из фото товара ----------
@router.message(F.text == BTN_VIDEO, Allowed())
async def video_start(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Flow.photo)
    await msg.answer("Пришлите фото товара (одно). Лучше снимать телефоном вертикально, крупно, при хорошем свете.\n"
                     "Фото станет первым кадром ролика. Горизонтальное фото я дострою до вертикали (≈ 0,15 $).")


ALBUMS: dict[str, list] = {}   # фото, пришедшие одним альбомом: первое — кадр, второе — пачка


@router.message(Flow.photo, NOT_MENU, Allowed())
async def video_photo(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Нужна фотография. Пришлите фото товара или /cancel для отмены.")
        return
    raw = await save_photo(bot, msg)
    pack = None
    if msg.media_group_id:   # альбом: Telegram присылает фото отдельными сообщениями почти одновременно
        gid = str(msg.media_group_id)
        first = gid not in ALBUMS
        ALBUMS.setdefault(gid, []).append((msg.message_id, raw))
        if not first:
            return
        await asyncio.sleep(2.5)
        items = [p for _, p in sorted(ALBUMS.pop(gid, []))]
        raw, pack = items[0], (items[1] if len(items) > 1 else None)
        for extra in items[2:]:
            media.cleanup(extra)
    if await asyncio.to_thread(media.is_vertical, raw):
        vert = await asyncio.to_thread(media.crop_vertical, raw)
    else:
        vert = await extend_photo(msg, raw)
    media.cleanup(raw)
    await state.update_data(src=str(vert), kind="product")
    if pack:
        await msg.answer("📦 Второе фото альбома — это пачка, ставлю её в кадр.")
        await apply_pack(msg, state, msg.from_user, pack)
        return
    await state.set_state(Flow.pack)
    await msg.answer("📦 Теперь пришлите фото НАСТОЯЩЕЙ пачки Laza отдельным сообщением (крупно, этикетка "
                     f"читается) — поставлю её в кадр (≈ {usd(IMAGE_PRICE_USD)}). ИИ сам рисует пачки неправильно.\n"
                     "Если на фото уже настоящая пачка — нажмите «Пачка уже настоящая».",
                     reply_markup=kb([("✅ Пачка уже настоящая", "pack:skip")]))


async def ask_idea(msg: Message, state: FSMContext):
    await state.set_state(Flow.idea)
    await msg.answer("Что должно происходить в ролике? Напишите по-русски, например:\n"
                     "«кубики мороженого падают на стол и хрустят, камера медленно приближается»\n"
                     "Люди в кадре не говорят — голос Лазы добавим отдельно кнопкой «🔊 Озвучить».",
                     reply_markup=kb([("🎲 Придумай сам", "idea:auto")]))


@router.callback_query(F.data == "pack:skip", Allowed())
async def pack_skip(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await ask_idea(cb.message, state)


@router.message(Flow.pack, NOT_MENU, Allowed())
async def pack_photo(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Пришлите фото пачки или нажмите «Пачка уже настоящая».")
        return
    await apply_pack(msg, state, msg.from_user, await save_photo(bot, msg))


async def apply_pack(msg: Message, state: FSMContext, user, pack: Path):
    """Ставит настоящую пачку в первый кадр и показывает результат на проверку."""
    data = await state.get_data()
    if db.spent_today() + IMAGE_PRICE_USD > DAILY_LIMIT_USD:
        await msg.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} исчерпан.")
        return
    status = await msg.answer("📦 Ставлю настоящую пачку в кадр…")
    try:
        url = await fal_api.put_real_pack(await fal_api.upload(Path(data["src"])), await fal_api.upload(pack))
        img = await media.download(url, "jpg")
        db.log_gen(user.id, user.full_name, "pack", "nano-banana-pro", 0, IMAGE_PRICE_USD, "ok", url)
        out = await asyncio.to_thread(media.crop_vertical, img)
        media.cleanup(img)
        await state.update_data(src=str(out), pack=str(pack))
        await state.set_state(Flow.pack)
        await msg.answer_photo(FSInputFile(out), caption="Первый кадр с настоящей пачкой. Проверьте этикетку — "
                               "видео начнётся с этого кадра.",
                               reply_markup=kb([("👍 Дальше", "pack:ok")], [("🔁 Ещё раз", "pack:redo")]))
    except fal_api.GenError as e:
        await msg.answer(f"⚠️ {e}", reply_markup=kb([("Продолжить без замены", "pack:skip")]))
    finally:
        await status.delete()


@router.callback_query(F.data.in_({"pack:ok", "pack:redo"}), Allowed())
async def pack_decide(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    if cb.data == "pack:ok":
        await ask_idea(cb.message, state)
    else:
        await state.set_state(Flow.pack)
        await cb.message.answer("Пришлите фото пачки ещё раз (можно другое, крупнее).")


async def extend_photo(msg: Message, raw: Path) -> Path:
    """Горизонтальное фото: ИИ достраивает верх и низ до 9:16. Не вышло — обрезаем по центру."""
    user = msg.from_user
    if db.spent_today() + IMAGE_PRICE_USD > DAILY_LIMIT_USD:
        return await asyncio.to_thread(media.crop_vertical, raw)
    status = await msg.answer(f"📐 Фото горизонтальное — достраиваю до вертикали (≈ {usd(IMAGE_PRICE_USD)})…")
    try:
        url = await fal_api.outpaint_vertical(await fal_api.upload(raw))
        img = await media.download(url, "jpg")
        db.log_gen(user.id, user.full_name, "outpaint", "nano-banana-pro", 0, IMAGE_PRICE_USD, "ok", url)
        out = await asyncio.to_thread(media.crop_vertical, img)   # ровно 1080×1920
        media.cleanup(img)
        await msg.answer_photo(FSInputFile(out), caption="Первый кадр ролика. Если не нравится — /cancel "
                               "и пришлите вертикальное фото (снятое телефоном вертикально).")
        return out
    except Exception:
        log.exception("Не удалось достроить фото")
        await msg.answer("Не получилось достроить фото — обрезал его по центру до вертикали.")
        return await asyncio.to_thread(media.crop_vertical, raw)
    finally:
        await status.delete()


# ---------- Режим 2: персонаж ----------
@router.message(F.text == BTN_CHAR, Allowed())
async def char_start(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Flow.char_idea)
    await msg.answer("Опишите сцену с Лазой, например:\n"
                     "«Лаза в белом халате на производстве показывает на кубики мороженого»\n"
                     "«Лаза держит пачку фисташкового мороженого и подмигивает»")


@router.message(Flow.char_idea, F.text, NOT_MENU, Allowed())
async def char_idea(msg: Message, state: FSMContext):
    await state.update_data(char_idea=msg.text.strip())
    await state.set_state(Flow.char_photo)
    await msg.answer("Нужен в сцене конкретный товар? Пришлите его фото — Лаза будет с ним.\n"
                     "Если не нужен — нажмите кнопку.", reply_markup=kb([("Без товара", "cphoto:none")]))


@router.message(Flow.char_photo, NOT_MENU, Allowed())
async def char_photo(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Пришлите фото товара или нажмите «Без товара».")
        return
    p = await save_photo(bot, msg)
    await state.update_data(char_product=str(p))
    await make_char_image(msg, state, msg.from_user)


@router.callback_query(F.data == "cphoto:none", Allowed())
async def char_nophoto(cb: CallbackQuery, state: FSMContext):
    await cb.answer()   # отвечаем сразу: картинка рисуется дольше, чем Telegram ждёт ответа на кнопку
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(char_product="")
    await make_char_image(cb.message, state, cb.from_user)


async def make_char_image(msg: Message, state: FSMContext, user):
    """Claude пишет промт → Nano Banana Pro рисует Лазу в сцене."""
    uid = user.id
    if uid in BUSY:
        await msg.answer("Подождите, предыдущая генерация ещё идёт.")
        return
    if db.spent_today() + IMAGE_PRICE_USD > DAILY_LIMIT_USD:
        await msg.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} исчерпан. Попробуйте завтра.")
        return
    data = await state.get_data()
    product = Path(data["char_product"]) if data.get("char_product") else None
    BUSY.add(uid)
    status = await msg.answer(f"🎨 Рисую Лазу… (≈ {usd(IMAGE_PRICE_USD)}, обычно до минуты)")
    try:
        r = refs()
        plan = await prompts.image_prompt(r, product, data["char_idea"])
        urls = [await fal_api.upload(p) for p in r + ([product] if product else [])]
        img_url = await fal_api.character_image(urls, plan["prompt"])
        img = await media.download(img_url, "jpg")
        db.log_gen(uid, user.full_name, "image", "nano-banana-pro", 0, IMAGE_PRICE_USD, "ok", img_url)
        log.info("Картинка готова %s: %s", uid, data["char_idea"])
        await state.update_data(src=str(img), kind="character", char_prompt=plan["prompt"])
        await state.set_state(Flow.pick)
        await msg.answer_photo(FSInputFile(img), caption=f"🧸 {plan.get('summary_ru', '')}",
                               reply_markup=kb([("🎬 Оживить", "anim")],
                                               [("🔁 Другой вариант", "cimg:again"),
                                                ("✏️ Другая сцена", "cimg:new")]))
    except fal_api.GenError as e:
        db.log_gen(uid, user.full_name, "image", "nano-banana-pro", 0, 0, "error")
        await msg.answer(f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка картинки")
        await msg.answer("⚠️ Не получилось нарисовать. Попробуйте ещё раз или опишите сцену иначе.")
    finally:
        BUSY.discard(uid)
        await status.delete()


@router.callback_query(F.data == "cimg:again", Allowed())
async def char_again(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await make_char_image(cb.message, state, cb.from_user)


@router.callback_query(F.data == "cimg:new", Allowed())
async def char_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Flow.char_idea)
    await cb.message.answer("Опишите новую сцену с Лазой.")


@router.callback_query(F.data == "anim", Allowed())
async def char_animate(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if not data.get("src") or not Path(data["src"]).exists():
        await cb.message.answer("Картинка устарела. Начните заново: " + BTN_CHAR)
        return
    await state.set_state(Flow.idea)
    await cb.message.answer("Что Лаза делает в ролике? Например: «машет рукой и говорит привет, "
                            "потом подпрыгивает от радости»", reply_markup=kb([("🎲 Придумай сам", "idea:auto")]))


# ---------- Общее: идея → длина → модель → подтверждение ----------
async def ask_duration(msg: Message, state: FSMContext):
    await state.set_state(Flow.pick)
    await msg.answer("Какой длины ролик?",
                     reply_markup=kb([(f"{d} с", f"dur:{d}") for d in DURATIONS]))


@router.message(Flow.idea, F.text, NOT_MENU, Allowed())
async def idea_text(msg: Message, state: FSMContext):
    await state.update_data(idea=msg.text.strip())
    await ask_duration(msg, state)


@router.callback_query(F.data == "idea:auto", Allowed())
async def idea_auto(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(idea="")
    await ask_duration(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("dur:"), Allowed())
async def pick_duration(cb: CallbackQuery, state: FSMContext):
    sec = int(cb.data.split(":")[1])
    await state.update_data(sec=sec)
    rows = [[(f"{m.title} — {usd(m.price(sec))}", f"mdl:{m.key}")] for m in VIDEO_MODELS.values()
            if not (m.fixed_part and sec > m.fixed_part)]   # склейка из кусков у черновой модели «плывёт»
    await cb.message.edit_text(f"Длина: {sec} с. Выберите качество (цена за весь ролик):",
                               reply_markup=kb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("mdl:"), Allowed())
async def pick_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    data = await state.get_data()
    if "sec" not in data or not data.get("src"):
        await cb.answer("Начните заново через меню", show_alert=True)
        return
    m = VIDEO_MODELS[key]
    sec = data["sec"]
    parts = m.parts(sec)
    await cb.message.edit_text("✍️ Claude пишет сценарий…")
    try:
        plan = await prompts.video_plan(Path(data["src"]), data.get("idea", ""), parts, data.get("kind", "product"))
    except Exception:
        log.exception("Ошибка плана")
        await cb.message.edit_text("⚠️ Не получилось написать сценарий. Попробуйте ещё раз.",
                                   reply_markup=kb([("🔁 Ещё раз", cb.data)]))
        await cb.answer()
        return
    price = m.price(sec)
    await state.update_data(model=key, plan=plan, price=price)
    real = sum(parts)
    extra = f" (выйдет {real} с: модель делает части по {parts[0]} с)" if real != sec else ""
    split = f"\nЧастей: {len(parts)}, склею в один ролик." if len(parts) > 1 else ""
    await cb.message.edit_text(
        f"🎬 {plan.get('summary_ru', '')}\n\n"
        f"Модель: {m.title} ({m.note})\nДлина: {sec} с{extra}{split}\n"
        f"Цена: ≈ {usd(price)}\nСегодня уже потрачено: {usd(db.spent_today())} из {usd(DAILY_LIMIT_USD)}",
        reply_markup=kb([("✅ Запустить", "go")], [("✏️ Другая идея", "newidea"), ("❌ Отмена", "cancel")]))
    await cb.answer()


@router.callback_query(F.data == "newidea", Allowed())
async def new_idea(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Flow.idea)
    await cb.message.answer("Напишите новую идею по-русски.", reply_markup=kb([("🎲 Придумай сам", "idea:auto")]))
    await cb.answer()


@router.callback_query(F.data == "cancel", Allowed())
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await cb.message.answer("Отменено. Деньги не списаны.", reply_markup=MENU)
    await cb.answer()


@router.callback_query(F.data.in_({"go", "again"}), Allowed())
async def go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    data = await state.get_data()
    if not data.get("plan") or not data.get("src") or not Path(data["src"]).exists():
        await cb.answer("Данные устарели — начните заново через меню", show_alert=True)
        return
    if uid in BUSY:
        await cb.answer("Предыдущий ролик ещё готовится", show_alert=True)
        return
    if db.spent_today() + data["price"] > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)   # кнопку нельзя нажать дважды
    await cb.answer("Запускаю")
    BUSY.add(uid)
    asyncio.create_task(run_video(bot, cb.message.chat.id, cb.from_user, data, state))


async def run_video(bot: Bot, chat_id: int, user, data: dict, state: FSMContext):
    """Генерирует части по очереди (каждая следующая — с последнего кадра), склеивает, отправляет."""
    m = VIDEO_MODELS[data["model"]]
    parts = data["plan"]["parts"]
    temp: list[Path] = []
    cleanup_old_tmp()
    status = await bot.send_message(chat_id, "⏳ Начинаю…")
    log.info("Видео старт %s %s %s с: %s", user.id, m.key, data["sec"], data.get("idea", ""))
    try:
        frame = Path(data["src"])
        for i, part in enumerate(parts, 1):
            await status.edit_text(f"⏳ Часть {i} из {len(parts)} ({part['duration']} с) готовится… "
                                   f"Обычно 2–6 минут. Можно пользоваться ботом дальше.")
            url = await fal_api.video_part(m, await fal_api.upload(frame), part["prompt"], part["duration"])
            clip = await media.download(url, "mp4")
            temp.append(clip)
            if i < len(parts):
                frame = await media.last_frame(clip)
                temp.append(frame)
        await status.edit_text("🎞 Собираю ролик…")
        final = await media.concat([p for p in temp if p.suffix == ".mp4"])
        if final not in temp:
            temp.append(final)
        final = await media.fit_telegram(final)
        if final not in temp:
            temp.append(final)
        db.log_gen(user.id, user.full_name, data.get("kind", "product"), m.key, data["sec"], data["price"], "ok")
        await remember(state, user.id, final, data.get("kind", "product"),
                       data["plan"].get("summary_ru", "") + " " + data.get("idea", ""))
        await send_vid(bot, chat_id, FSInputFile(final, filename="laza.mp4"), supports_streaming=True,
                             caption=f"✅ {m.title}, {data['sec']} с, ≈ {usd(data['price'])}",
                             reply_markup=kb([("🔊 Озвучить", "voice")],
                                             [("🔁 Ещё вариант", "again"), ("✏️ Другая идея", "newidea")]))
        log.info("Видео готово %s", user.id)
    except fal_api.GenError as e:
        db.log_gen(user.id, user.full_name, data.get("kind", "product"), m.key, data["sec"], 0, "error")
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка видео")
        db.log_gen(user.id, user.full_name, data.get("kind", "product"), m.key, data["sec"], 0, "error")
        await bot.send_message(chat_id, "⚠️ Что-то пошло не так при создании ролика. Попробуйте ещё раз.")
    finally:
        BUSY.discard(user.id)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        media.cleanup(*temp)


# ---------- Режим 4: повтор тренда (Higgsfield Genjutsu Light) ----------
@router.message(F.text == BTN_TREND, Allowed())
async def trend_start(msg: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Flow.trend_video)
    await msg.answer("Пришлите видео с движением, которое нужно повторить: танец, жест, трендовый ролик.\n"
                     f"Берутся первые {GENJUTSU_MAX_SEC} секунд, звук из видео сохраняется.\n"
                     "Размер — до 20 МБ (ограничение Telegram для ботов). Чужую музыку в рекламе "
                     "используйте только если она разрешена.")


@router.message(Flow.trend_video, NOT_MENU, Allowed())
async def trend_video(msg: Message, state: FSMContext, bot: Bot):
    f = msg.video or msg.animation or (msg.document if msg.document and
                                      (msg.document.mime_type or "").startswith("video/") else None)
    if not f:
        await msg.answer("Пришлите видео (файлом или как видео).")
        return
    if (f.file_size or 0) > 20 * 1024 * 1024:
        await msg.answer("Видео больше 20 МБ — Telegram не даёт боту его скачать. Обрежьте или сожмите и пришлите снова.")
        return
    dst = media.new_path("mp4")
    await bot.download(f.file_id, destination=dst)
    dur = await media.duration(dst)
    full = max(1, min(GENJUTSU_MAX_SEC, math.ceil(dur)))
    await state.update_data(trend_video=str(dst), trend_sec=full, trend_full=full)
    lens = [n for n in TREND_LENGTHS if n < full] + [full]
    rows = [[(f"{n} с" + (" (всё видео)" if n == full else ""), f"tlen:{n}")] for n in lens]
    await msg.answer(f"Видео принято ({full} с). Какой длины делать ролик? Короче — дешевле.",
                     reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("tlen:"), Allowed())
async def trend_len(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        n = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer()
        return
    if not data.get("trend_video") or not Path(data["trend_video"]).exists():
        await cb.answer("Данные устарели — начните заново через меню", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    full = data.get("trend_full", GENJUTSU_MAX_SEC)
    n = max(1, min(n, full))
    if n < full:
        cut = await media.trim(Path(data["trend_video"]), n)
        await state.update_data(trend_video=str(cut))
    await state.update_data(trend_sec=n)
    await state.set_state(Flow.trend_photo)
    await cb.message.answer(f"Берём {n} с.\n"
                            "Теперь фото того, кто будет повторять движения: человек целиком в кадре, хорошо видно лицо. "
                            "Или нажмите кнопку — возьму Лазу.", reply_markup=kb([("🧸 Лаза", "trend:laza")]))


@router.message(Flow.trend_photo, NOT_MENU, Allowed())
async def trend_photo(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Пришлите фото или нажмите «Лаза».")
        return
    p = await save_photo(bot, msg)
    await state.update_data(trend_image=str(p), trend_who="фото")
    await trend_ask_res(msg, state)


@router.callback_query(F.data == "trend:laza", Allowed())
async def trend_laza(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    r = [p for p in refs() if "main" in p.name] or refs()
    if not r:
        await cb.message.answer("Нет эталона Лазы. Пришлите фото.")
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(trend_image=str(r[0]), trend_who="Лаза")
    await trend_ask_res(cb.message, state)


async def trend_ask_res(msg: Message, state: FSMContext):
    data = await state.get_data()
    sec = data.get("trend_sec", GENJUTSU_MAX_SEC)
    rows = []
    if hf_api.enabled():
        rows += [[(f"Higgsfield {res} — до {usd(rate * sec)}", f"tres:{res}")]
                 for res, rate in GENJUTSU_USD_PER_SEC.items()]
    rows.append([(f"Kling 2.6 (fal.ai) — {usd(MOTION_FAL_USD_PER_SEC * sec)}", "tres:kling")])
    rows.append([("Отмена", "cancel")])
    await state.set_state(Flow.pick)
    await msg.answer(f"Чем делать? Ролик {sec} с.\n"
                     "• Higgsfield — платится с баланса Higgsfield API; цены в боте примерные, могут быть выше.\n"
                     "• Kling 2.6 — платится с баланса fal.ai (как остальные ролики бота), цена точная.",
                     reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("tres:"), Allowed())
async def trend_res(cb: CallbackQuery, state: FSMContext):
    res = cb.data.split(":", 1)[1]
    if res != "kling" and res not in GENJUTSU_USD_PER_SEC:
        await cb.answer()
        return
    data = await state.get_data()
    rate = MOTION_FAL_USD_PER_SEC if res == "kling" else GENJUTSU_USD_PER_SEC[res]
    price = round(rate * data.get("trend_sec", GENJUTSU_MAX_SEC), 2)
    await state.update_data(trend_res=res, trend_price=price)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"🕺 Повтор тренда: {data.get('trend_who', 'фото')}, "
                            f"{'Kling 2.6 (fal.ai)' if res == 'kling' else 'Higgsfield ' + res}, {data.get('trend_sec')} с.\n"
                            f"Цена — до {usd(price)}. Запускаем?",
                            reply_markup=kb([("▶️ Запустить", "tgo")], [("Отмена", "cancel")]))
    await cb.answer()


@router.callback_query(F.data == "tgo", Allowed())
async def trend_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    data = await state.get_data()
    if not (data.get("trend_video") and data.get("trend_image") and data.get("trend_res")
            and Path(data["trend_video"]).exists() and Path(data["trend_image"]).exists()):
        await cb.answer("Данные устарели — начните заново через меню", show_alert=True)
        return
    if uid in BUSY:
        await cb.answer("Предыдущий ролик ещё готовится", show_alert=True)
        return
    if db.spent_today() + data["trend_price"] > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)   # кнопку нельзя нажать дважды
    await cb.answer("Запускаю")
    BUSY.add(uid)
    asyncio.create_task(run_trend(bot, cb.message.chat.id, cb.from_user, data, state))


async def run_trend(bot: Bot, chat_id: int, user, data: dict, state: FSMContext):
    temp: list[Path] = []
    sec, res, price = data["trend_sec"], data["trend_res"], data["trend_price"]
    svc = "Kling 2.6" if res == "kling" else "Higgsfield"
    model_name = "kling2.6-motion" if res == "kling" else "genjutsu-light"
    status = await bot.send_message(chat_id, f"⏳ {svc} переносит движения… Обычно 2–6 минут. "
                                             "Можно пользоваться ботом дальше.")
    log.info("Тренд старт %s %s %s с", user.id, res, sec)
    try:
        if res == "kling":
            v_url = await fal_api.upload(Path(data["trend_video"]))
            i_url = await fal_api.upload(Path(data["trend_image"]))
            url = await fal_api.motion_control(v_url, i_url)
        else:
            url = await hf_api.motion_transfer(Path(data["trend_video"]), Path(data["trend_image"]), res)
        clip = await media.download(url, "mp4")
        temp.append(clip)
        if res == "kling":
            clip = await media.add_audio_from(clip, Path(data["trend_video"]))
            if clip not in temp:
                temp.append(clip)
        final = await media.fit_telegram(clip)
        if final not in temp:
            temp.append(final)
        db.log_gen(user.id, user.full_name, "trend", model_name, sec, price, "ok", url)
        await remember(state, user.id, final, "trend", f"Повтор тренда: {data.get('trend_who', '')}")
        await send_vid(bot, chat_id, FSInputFile(final, filename="laza_trend.mp4"), supports_streaming=True,
                       caption=f"✅ Повтор тренда, {svc}{'' if res == 'kling' else ' ' + res}, {sec} с, до {usd(price)}",
                       reply_markup=kb([("🔁 Ещё вариант", "tgo")]))
        log.info("Тренд готов %s", user.id)
    except fal_api.GenError as e:
        db.log_gen(user.id, user.full_name, "trend", model_name, sec, 0, "error")
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка тренда")
        db.log_gen(user.id, user.full_name, "trend", model_name, sec, 0, "error")
        await bot.send_message(chat_id, "⚠️ Что-то пошло не так. Попробуйте ещё раз.")
    finally:
        BUSY.discard(user.id)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        media.cleanup(*temp)


# ---------- Последний ролик: запоминаем для озвучки ----------
async def remember(state: FSMContext, uid: int, video: Path, kind: str, context: str,
                   track: Path | None = None) -> None:
    """Копирует готовый ролик в data/last и пишет его данные в состояние человека."""
    for old in LAST_DIR.glob(f"{uid}_*"):
        old.unlink(missing_ok=True)
    stamp = int(time.time())
    keep = LAST_DIR / f"{uid}_{stamp}.mp4"
    shutil.copy(video, keep)
    upd = {"last_video": str(keep), "last_sec": round(await media.duration(keep), 2),
           "last_kind": kind, "last_ctx": context.strip()[:600], "voice_track": ""}
    if track:
        tkeep = LAST_DIR / f"{uid}_{stamp}_voice.m4a"
        shutil.copy(track, tkeep)
        upd["voice_track"] = str(tkeep)
    await state.update_data(**upd)


def speech_sec(text: str) -> float:
    """Примерная длительность реплики (теги вроде [excited] не звучат)."""
    return len(re.sub(r"\[[^\]]*\]", "", text).strip()) / 13


def shown(text: str) -> str:
    """Текст реплики без служебных меток интонации."""
    return re.sub(r"\s*\[[^\]]*\]\s*", " ", text).strip()


def tts_price(text: str) -> float:
    return max(0.01, round(len(text) / 1000 * TTS_USD_PER_1K, 2))


async def last_ok(cb: CallbackQuery, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if not data.get("last_video") or not Path(data["last_video"]).exists():
        await cb.answer("Ролик устарел — сделайте новый через меню", show_alert=True)
        return None
    return data


# ---------- 🔊 Озвучка ----------
@router.callback_query(F.data == "voice", Allowed())
async def voice_start(cb: CallbackQuery, state: FSMContext):
    if not await last_ok(cb, state):
        return
    await cb.answer()
    await cb.message.answer("На каком языке говорит Лаза?",
                            reply_markup=kb([(t, f"vlang:{k}") for k, t in LANGS.items()]))


@router.callback_query(F.data.startswith("vlang:"), Allowed())
async def voice_lang(cb: CallbackQuery, state: FSMContext):
    await state.update_data(vlang=cb.data.split(":")[1])
    await cb.message.edit_text("Текст реплики:", reply_markup=kb([("🎲 Напиши сам", "vtext:auto")],
                                                                 [("✍️ Свой текст", "vtext:own")]))
    await cb.answer()


@router.callback_query(F.data == "vtext:own", Allowed())
async def voice_own(cb: CallbackQuery, state: FSMContext):
    data = await last_ok(cb, state)
    if not data:
        return
    await cb.answer()
    await state.set_state(Flow.voice_text)
    lang = data.get("vlang", "ru")
    await cb.message.answer(f"Напишите, что скажет Лаза ({LANGS[lang]}). Для ролика {data['last_sec']:.0f} с — "
                            f"до {int(data['last_sec'] * 13)} символов. Узбекский — латиницей.")


@router.message(Flow.voice_text, F.text, NOT_MENU, Allowed())
async def voice_own_text(msg: Message, state: FSMContext):
    await state.set_state(None)
    await voice_confirm(msg, state, msg.text.strip(), "")


@router.callback_query(F.data == "vtext:auto", Allowed())
async def voice_auto(cb: CallbackQuery, state: FSMContext):
    data = await last_ok(cb, state)
    if not data:
        return
    await cb.answer()
    status = await cb.message.answer("✍️ Claude пишет реплику…")
    frame = None
    try:
        frame = await media.last_frame(Path(data["last_video"]))
        res = await prompts.voice_line(data.get("last_ctx", ""), data["last_sec"], data.get("vlang", "ru"), frame)
        await voice_confirm(cb.message, state, res["text"], res.get("ru", ""))
    except Exception:
        log.exception("Ошибка реплики")
        await cb.message.answer("⚠️ Не получилось написать реплику. Попробуйте ещё раз или напишите свой текст.",
                                reply_markup=kb([("🎲 Ещё раз", "vtext:auto"), ("✍️ Свой текст", "vtext:own")]))
    finally:
        await status.delete()
        if frame:
            media.cleanup(frame)


async def voice_confirm(msg: Message, state: FSMContext, text: str, ru: str):
    data = await state.get_data()
    lang, sec = data.get("vlang", "ru"), data.get("last_sec", 5)
    await state.update_data(vtext=text)
    est = speech_sec(text)
    warn = (f"\n⚠️ Реплика (~{est:.0f} с) длиннее ролика ({sec:.0f} с): последний кадр продлится."
            if est > sec + 0.5 else "")
    tr = f"\nПеревод: {ru}" if ru and lang != "ru" and ru != text else ""
    await msg.answer(f"🔊 {LANGS[lang]}, голос {voice.engine(lang)}:\n«{shown(text)}»{tr}\n\n"
                     f"Звучит ≈ {est:.0f} с. Цена ≈ {usd(tts_price(text))}{warn}",
                     reply_markup=kb([("✅ Озвучить", "vgo")],
                                     [("🎲 Другой текст", "vtext:auto"), ("✍️ Свой текст", "vtext:own")]))


@router.callback_query(F.data == "vgo", Allowed())
async def voice_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await last_ok(cb, state)
    if not data or not data.get("vtext"):
        return
    uid = cb.from_user.id
    if uid in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Озвучиваю")
    BUSY.add(uid)
    asyncio.create_task(run_voice(bot, cb.message.chat.id, cb.from_user, data, state))


async def make_voice(text: str, lang: str) -> Path:
    return await voice.speak(text, lang)


def after_voice_kb(kind: str, seconds: float) -> InlineKeyboardMarkup:
    rows = []
    if kind in ("character", "scenes"):
        rows.append([(f"👄 Синхронизировать губы — ≈ {usd(LIPSYNC_USD_PER_SEC * seconds)}", "lips")])
    if kind == "product":
        rows.append([(f"🗣 Говорит в кадре (Kling Avatar) — ≈ {usd(AVATAR_USD_PER_SEC * seconds)}", "avatar")])
    rows.append([("🔊 Другая озвучка", "voice")])
    return kb(*rows)


async def run_voice(bot: Bot, chat_id: int, user, data: dict, state: FSMContext):
    temp: list[Path] = []
    status = await bot.send_message(chat_id, "🔊 Озвучиваю… (до минуты)")
    text, lang = data["vtext"], data.get("vlang", "ru")
    try:
        mp3 = await make_voice(text, lang)
        temp.append(mp3)
        db.log_gen(user.id, user.full_name, "voice", "eleven-v3", 0, tts_price(text), "ok")
        track = await media.build_track([(mp3, 0.0, None)], data["last_sec"])
        temp.append(track)
        out = await media.mux_voice(Path(data["last_video"]), track)
        temp.append(out)
        final = await media.fit_telegram(out)
        temp.append(final)
        await state.update_data(voice_track=str(_keep_track(user.id, track)))
        sec = await media.duration(final)
        await send_vid(bot, chat_id, FSInputFile(final, filename="laza_voice.mp4"), supports_streaming=True,
                             caption=f"✅ С голосом Лазы ({LANGS[lang]})",
                             reply_markup=after_voice_kb(data.get("last_kind", ""), sec))
        log.info("Озвучка готова %s", user.id)
    except fal_api.GenError as e:
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка озвучки")
        await bot.send_message(chat_id, "⚠️ Не получилось озвучить. Попробуйте ещё раз.")
    finally:
        BUSY.discard(user.id)
        await status.delete()
        media.cleanup(*temp)


def _keep_track(uid: int, track: Path) -> Path:
    for old in LAST_DIR.glob(f"{uid}_*_voice.m4a"):
        old.unlink(missing_ok=True)
    keep = LAST_DIR / f"{uid}_{int(time.time())}_voice.m4a"
    shutil.copy(track, keep)
    return keep


# ---------- 👄 Синхронизация губ ----------
@router.callback_query(F.data == "lips", Allowed())
async def lips(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await last_ok(cb, state)
    if not data:
        return
    if not data.get("voice_track") or not Path(data["voice_track"]).exists():
        await cb.answer("Сначала озвучьте ролик", show_alert=True)
        return
    uid = cb.from_user.id
    if uid in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    sec = max(data["last_sec"], await media.duration(Path(data["voice_track"])))
    price = round(LIPSYNC_USD_PER_SEC * sec, 2)
    if db.spent_today() + price > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Запускаю")
    BUSY.add(uid)
    asyncio.create_task(run_lips(bot, cb.message.chat.id, cb.from_user, data, sec, price))


async def run_lips(bot: Bot, chat_id: int, user, data: dict, sec: float, price: float):
    temp: list[Path] = []
    status = await bot.send_message(chat_id, "👄 Синхронизирую губы… (2–5 минут)")
    try:
        video = await media.pad_video(Path(data["last_video"]), sec)
        if video != Path(data["last_video"]):
            temp.append(video)
        url = await fal_api.lipsync(await fal_api.upload(video), await fal_api.upload(Path(data["voice_track"])))
        out = await media.download(url, "mp4")
        temp.append(out)
        final = await media.fit_telegram(out)
        temp.append(final)
        db.log_gen(user.id, user.full_name, "lipsync", "sync-lipsync-2", int(sec), price, "ok")
        await send_vid(bot, chat_id, FSInputFile(final, filename="laza_lips.mp4"), supports_streaming=True,
                             caption=f"✅ Губы синхронизированы, ≈ {usd(price)}. На мультперсонаже результат "
                                     "бывает разным — если хуже, используйте версию без синхронизации.")
    except fal_api.GenError as e:
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка синхронизации губ")
        await bot.send_message(chat_id, "⚠️ Не получилось синхронизировать губы.")
    finally:
        BUSY.discard(user.id)
        await status.delete()
        media.cleanup(*temp)


# ---------- 🗣 Говорит в кадре: Kling AI Avatar по первому кадру ролика ----------
AVATAR_PROMPT = ("The person in the image speaks naturally to the camera with accurate lip sync, small natural head "
                 "movements, friendly expression. Hands and any product they hold stay still: the product keeps "
                 "exactly the same size, shape and label and never moves toward the camera. Background stays the "
                 "same. Realistic, natural skin, no beauty filter.")


@router.callback_query(F.data == "avatar", Allowed())
async def avatar_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await last_ok(cb, state)
    if not data:
        return
    if not data.get("voice_track") or not Path(data["voice_track"]).exists():
        await cb.answer("Сначала озвучьте ролик", show_alert=True)
        return
    uid = cb.from_user.id
    if uid in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    sec = await media.duration(Path(data["voice_track"]))
    price = round(AVATAR_USD_PER_SEC * sec, 2)
    if db.spent_today() + price > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Запускаю")
    BUSY.add(uid)
    asyncio.create_task(run_avatar(bot, cb.message.chat.id, cb.from_user, data, sec, price))


async def run_avatar(bot: Bot, chat_id: int, user, data: dict, sec: float, price: float):
    temp: list[Path] = []
    status = await bot.send_message(chat_id, "🗣 Делаю говорящую версию… (3–8 минут)")
    try:
        src = data.get("src")
        if src and Path(src).exists():
            frame = Path(src)
        else:
            frame = await media.first_frame(Path(data["last_video"]))
            temp.append(frame)
        url = await fal_api.avatar(await fal_api.upload(frame), await fal_api.upload(Path(data["voice_track"])),
                                   AVATAR_PROMPT)
        out = await media.download(url, "mp4")
        temp.append(out)
        final = await media.fit_telegram(out)
        if final not in temp:
            temp.append(final)
        db.log_gen(user.id, user.full_name, "avatar", "kling-ai-avatar-v2", int(sec), price, "ok")
        await send_vid(bot, chat_id, FSInputFile(final, filename="laza_talk.mp4"), supports_streaming=True,
                       caption=f"✅ Говорит в кадре, губы по голосу · Kling Avatar, ≈ {usd(price)}")
        log.info("Говорящая версия готова %s", user.id)
    except fal_api.GenError as e:
        db.log_gen(user.id, user.full_name, "avatar", "kling-ai-avatar-v2", int(sec), 0, "error")
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка говорящей версии")
        db.log_gen(user.id, user.full_name, "avatar", "kling-ai-avatar-v2", int(sec), 0, "error")
        await bot.send_message(chat_id, "⚠️ Не получилось сделать говорящую версию. Попробуйте ещё раз.")
    finally:
        BUSY.discard(user.id)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        media.cleanup(*temp)


# ---------- 🎞 Ролик с Лазой из нескольких сцен ----------
def scenes_price(total: int, lang: str | None, sound: bool = False) -> tuple[float, float, float]:
    """(картинки, видео, голос) в долларах. sound — сцены без голоса со звуком (Seedance, дороже)."""
    n, sec = SCENE_PLANS[total]
    m = VIDEO_MODELS[SCENE_MODEL]
    rate = AVATAR_USD_PER_SEC if lang else m.usd_per_sec   # с голосом — говорящие сцены (губы по звуку)
    if sound:
        rate = max(rate, VIDEO_MODELS["seed"].usd_per_sec)
    return (round(IMAGE_PRICE_USD * n, 2), round(rate * sec * n, 2), 0.03 if lang else 0.0)


@router.message(F.text == BTN_SCENES, Allowed())
async def sc_start(msg: Message, state: FSMContext):
    await state.set_state(Flow.sc_idea)
    await state.update_data(sc_product="", sc_tpl="")
    await msg.answer("О чём ролик? Опишите идею по-русски, например:\n"
                     "«Лаза в пробке на Амира Темура, все злые, а он угощает соседей кубиками»\n\n"
                     "📦 Можно сначала прислать фото пачки Laza — она появится в сценах.\n\n"
                     "Или возьмите готовый сюжет:",
                     reply_markup=kb(*[[(t, f"sctpl:{k}") for k, (t, _) in list(SCENE_TEMPLATES.items())[i:i + 2]]
                                       for i in range(0, len(SCENE_TEMPLATES), 2)]))


@router.message(Flow.sc_idea, F.photo, Allowed())
async def sc_product(msg: Message, state: FSMContext, bot: Bot):
    path = await save_photo(bot, msg)
    await state.update_data(sc_product=str(path))
    await msg.answer("📦 Фото продукта принято — пачка будет в сценах. Теперь напишите идею или выберите сюжет выше.")


@router.message(Flow.sc_idea, F.text, NOT_MENU, Allowed())
async def sc_idea(msg: Message, state: FSMContext):
    await state.update_data(sc_idea=msg.text.strip(), sc_tpl="")
    await sc_ask_len(msg, state)


@router.callback_query(F.data.startswith("sctpl:") | (F.data == "sc:tpl"), Allowed())
async def sc_tpl(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1] if cb.data.startswith("sctpl:") else "intro"
    title, plot = SCENE_TEMPLATES.get(key, SCENE_TEMPLATES["intro"])
    await cb.message.edit_text(f"Сюжет: {title}\n{plot}")
    await state.update_data(sc_idea=plot, sc_tpl=key)
    await sc_ask_len(cb.message, state)
    await cb.answer()


async def sc_ask_len(msg: Message, state: FSMContext):
    await state.set_state(None)
    rows = []
    for total, (n, sec) in SCENE_PLANS.items():
        img, vid, _ = scenes_price(total, "ru")
        rows.append([(f"{total} с · {n} сцен по {sec} с — ≈ {usd(img + vid)}", f"sclen:{total}")])
    await msg.answer("Длина ролика? (сцены с голосом — Kling Avatar, губы точно по звуку; цена с картинками)", reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("sclen:"), Allowed())
async def sc_len(cb: CallbackQuery, state: FSMContext):
    await state.update_data(sc_total=int(cb.data.split(":")[1]))
    rows = [[(t, f"sclang:{k}") for k, t in LANGS.items()], [("🔇 Без голоса", "sclang:none")]]
    await cb.message.edit_text("Голос Лазы:", reply_markup=kb(*rows))
    await cb.answer()


@router.callback_query(F.data.startswith("sclang:"), Allowed())
async def sc_lang(cb: CallbackQuery, state: FSMContext):
    lang = cb.data.split(":")[1]
    await state.update_data(sc_lang="" if lang == "none" else lang)
    await cb.answer()
    await sc_plan(cb.message, state)


async def sc_plan(msg: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("sc_idea") or not data.get("sc_total"):
        await msg.answer("Начните заново: " + BTN_SCENES)
        return
    total, lang = data["sc_total"], data.get("sc_lang") or None
    n, sec = SCENE_PLANS[total]
    await msg.edit_text("✍️ Claude пишет сценарий и реплики…")
    try:
        prod = Path(data["sc_product"]) if data.get("sc_product") and Path(data["sc_product"]).exists() else None
        sb = await prompts.storyboard(refs(), data["sc_idea"], n, sec, lang, prod)
    except Exception:
        log.exception("Ошибка раскадровки")
        await msg.edit_text("⚠️ Не получилось написать сценарий.", reply_markup=kb([("🔁 Ещё раз", "scplan")]))
        return
    sb["hook_i"] = 0
    await state.update_data(sc_board=sb, sc_imgs=[])
    await show_plan(msg, state)


def plan_text(sb: dict, total: int, lang: str | None, sound: bool = False) -> str:
    n, sec = SCENE_PLANS[total]
    img, vid, vo = scenes_price(total, lang, sound)
    hooks, hi = sb.get("hooks") or [], sb.get("hook_i", 0)
    lines = [f"🎞 {sb.get('summary_ru', '')}", ""]
    if hooks and hooks[hi].get("overlay"):
        h = hooks[hi]
        lines.append(f"🎣 Хук (первые 2 с, {h.get('mechanism_ru', '')}): «{h['overlay']}»")
        others = [f"  {j + 1}) {o.get('mechanism_ru', '')}: «{o['overlay']}»" for j, o in enumerate(hooks) if j != hi]
        if others:
            lines += ["Другие хуки:"] + others
        lines.append("")
    for i, s in enumerate(sb["scenes"], 1):
        line = f"{i}. {s.get('title_ru', '')} ({sec} с)"
        if s.get("voice"):
            line += f"\n   🗣 «{shown(s['voice'])}»"
            if lang != "ru" and s.get("voice_ru"):
                line += f"\n   ({s['voice_ru']})"
        lines.append(line)
    if sb.get("sends_ru"):
        lines += ["", f"📨 Зачем перешлют: {sb['sends_ru']}"]
    lines += ["", f"Шаг 1 — картинки сцен: ≈ {usd(img)}",
              f"Шаг 2 — оживление и монтаж: ≈ {usd(vid + vo)}",
              "После картинок можно перерисовать любую сцену до оживления."]
    return "\n".join(lines)


async def show_plan(msg: Message, state: FSMContext):
    data = await state.get_data()
    sb, total, lang = data["sc_board"], data["sc_total"], data.get("sc_lang") or None
    img, _, _ = scenes_price(total, lang, data.get("sc_tpl") in SOUND_TEMPLATES)
    hooks, hi = sb.get("hooks") or [], sb.get("hook_i", 0)
    hook_row = [(f"🎣 Хук {j + 1}", f"schook:{j}") for j in range(len(hooks)) if j != hi and hooks[j].get("overlay")]
    rows = [[(f"🎨 Нарисовать сцены — ≈ {usd(img)}", "scdraw")]]
    if hook_row:
        rows.append(hook_row)
    rows.append([("🔁 Другой сценарий", "scplan"), ("❌ Отмена", "cancel")])
    await msg.edit_text(plan_text(sb, total, lang, data.get("sc_tpl") in SOUND_TEMPLATES), reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("schook:"), Allowed())
async def sc_hook(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sb = data.get("sc_board")
    j = int(cb.data.split(":")[1])
    if not sb or j >= len(sb.get("hooks") or []):
        await cb.answer("Начните заново через меню", show_alert=True)
        return
    h = sb["hooks"][j]
    sb["hook_i"] = j
    if h.get("voice"):
        sb["scenes"][0]["voice"] = h["voice"]
        sb["scenes"][0]["voice_ru"] = h.get("voice_ru", "")
    await state.update_data(sc_board=sb)
    await cb.answer(f"Хук {j + 1}")
    await show_plan(cb.message, state)


@router.callback_query(F.data == "scplan", Allowed())
async def sc_replan(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await sc_plan(cb.message, state)


@router.callback_query(F.data == "scdraw", Allowed())
async def sc_draw(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("sc_board"):
        await cb.answer("Начните заново через меню", show_alert=True)
        return
    n = len(data["sc_board"]["scenes"])
    if db.spent_today() + IMAGE_PRICE_USD * n > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Рисую")
    BUSY.add(cb.from_user.id)
    asyncio.create_task(draw_scenes(bot, cb.message.chat.id, cb.from_user, state, list(range(n))))


async def draw_scenes(bot: Bot, chat_id: int, user, state: FSMContext, which: list[int]):
    """Рисует сцены с Лазой (все или выбранные) и показывает кнопки оживления."""
    data = await state.get_data()
    scenes = data["sc_board"]["scenes"]
    imgs = list(data.get("sc_imgs") or [""] * len(scenes))
    status = await bot.send_message(chat_id, f"🎨 Рисую сцены: {len(which)} шт. (до минуты на каждую)…")
    try:
        ref_urls = [await fal_api.upload(p) for p in refs()]
        if data.get("sc_product") and Path(data["sc_product"]).exists():   # пачка Laza — последней картинкой
            ref_urls.append(await fal_api.upload(Path(data["sc_product"])))
        for i in which:
            url = await fal_api.character_image(ref_urls, scenes[i]["image_prompt"])
            img = await media.download(url, "jpg")
            db.log_gen(user.id, user.full_name, "scene_image", "nano-banana-pro", 0, IMAGE_PRICE_USD, "ok", url)
            if imgs[i]:
                media.cleanup(Path(imgs[i]))
            imgs[i] = str(img)
            await bot.send_photo(chat_id, FSInputFile(img), caption=f"Сцена {i + 1}: {scenes[i].get('title_ru', '')}")
        await state.update_data(sc_imgs=imgs)
        total, lang = data["sc_total"], data.get("sc_lang") or None
        _, vid, vo = scenes_price(total, lang, data.get("sc_tpl") in SOUND_TEMPLATES)
        await bot.send_message(chat_id, "Всё нравится? Тогда оживляем и монтируем. "
                                        "Не нравится сцена — перерисуйте её (≈ 0,15 $).",
                               reply_markup=kb([(f"🎬 Оживить и смонтировать — ≈ {usd(vid + vo)}", "scgo")],
                                               [(f"🔁 Сцена {i + 1}", f"scredo:{i}") for i in range(len(scenes))],
                                               [("❌ Отмена", "cancel")]))
    except fal_api.GenError as e:
        await bot.send_message(chat_id, f"⚠️ {e}", reply_markup=kb([("🔁 Ещё раз", "scdraw")]))
    except Exception:
        log.exception("Ошибка сцен")
        await bot.send_message(chat_id, "⚠️ Не получилось нарисовать сцены.", reply_markup=kb([("🔁 Ещё раз", "scdraw")]))
    finally:
        BUSY.discard(user.id)
        await status.delete()


@router.callback_query(F.data.startswith("scredo:"), Allowed())
async def sc_redo(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    i = int(cb.data.split(":")[1])
    if not data.get("sc_board") or i >= len(data["sc_board"]["scenes"]):
        await cb.answer("Начните заново через меню", show_alert=True)
        return
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer(f"Перерисовываю сцену {i + 1}")
    BUSY.add(cb.from_user.id)
    asyncio.create_task(draw_scenes(bot, cb.message.chat.id, cb.from_user, state, [i]))


@router.callback_query(F.data == "scgo", Allowed())
async def sc_go(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Шаг перед оплатой видео: озвучка всех реплик и прослушивание (видео ещё не генерируется)."""
    data = await state.get_data()
    imgs = data.get("sc_imgs") or []
    if not imgs or not all(p and Path(p).exists() for p in imgs):
        await cb.answer("Картинки устарели — начните заново", show_alert=True)
        return
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    lang = data.get("sc_lang") or None
    if not lang or not any(s.get("voice") for s in data["sc_board"]["scenes"]):
        await sc_launch(cb, state, bot)   # без голоса — сразу видео
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Озвучиваю")
    BUSY.add(cb.from_user.id)
    try:
        await preview_voices(bot, cb.message.chat.id, cb.from_user, state)
    finally:
        BUSY.discard(cb.from_user.id)


async def preview_voices(bot: Bot, chat_id: int, user, state: FSMContext):
    data = await state.get_data()
    sb, lang = data["sc_board"], data["sc_lang"]
    status = await bot.send_message(chat_id, "🔊 Озвучиваю реплики — послушайте до оплаты видео…")
    try:
        for old in SCN_DIR.glob(f"{user.id}_voice*"):
            old.unlink(missing_ok=True)
        mp3s, parts, t = [], [], 0.0
        for i, s in enumerate(sb["scenes"]):
            if not s.get("voice"):
                mp3s.append("")
                continue
            mp3 = await make_voice(s["voice"], lang)
            keep = SCN_DIR / f"{user.id}_voice{i}.mp3"
            shutil.move(str(mp3), keep)
            db.log_gen(user.id, user.full_name, "voice", "tts", 0, tts_price(s["voice"]), "ok")
            mp3s.append(str(keep))
            d = await media.duration(keep)
            parts.append((keep, t, None))
            t += d + 0.6
        track = await media.build_track(parts, t)
        await state.update_data(sc_voice_mp3=mp3s)
        _, vid, vo = scenes_price(data["sc_total"], lang, data.get("sc_tpl") in SOUND_TEMPLATES)
        lines = [f"{i + 1}. «{shown(s['voice'])}»" for i, s in enumerate(sb["scenes"]) if s.get("voice")]
        await bot.send_audio(chat_id, FSInputFile(track, filename="laza_voice_preview.m4a"),
                             title=f"Озвучка ролика ({LANGS[lang]})", caption="\n".join(lines))
        media.cleanup(track)
        await bot.send_message(chat_id, "Послушайте: реплики целиком, ударение в «Лаза́», голос. "
                                        "Если всё хорошо — оживляем. Сцены будут такой длины, чтобы реплика "
                                        "не обрывалась.",
                               reply_markup=kb([(f"🎬 Оживить и смонтировать — ≈ {usd(vid + vo)}", "scgo2")],
                                               [("🔁 Озвучить заново", "scvoice"), ("❌ Отмена", "cancel")]))
    except fal_api.GenError as e:
        await bot.send_message(chat_id, f"⚠️ {e}", reply_markup=kb([("🔁 Ещё раз", "scvoice")]))
    except Exception:
        log.exception("Ошибка предпрослушки")
        await bot.send_message(chat_id, "⚠️ Не получилось озвучить.", reply_markup=kb([("🔁 Ещё раз", "scvoice")]))
    finally:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data == "scvoice", Allowed())
async def sc_revoice(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Озвучиваю заново")
    BUSY.add(cb.from_user.id)
    try:
        await preview_voices(bot, cb.message.chat.id, cb.from_user, state)
    finally:
        BUSY.discard(cb.from_user.id)


@router.callback_query(F.data == "scgo2", Allowed())
async def sc_launch(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    imgs = data.get("sc_imgs") or []
    if not imgs or not all(p and Path(p).exists() for p in imgs):
        await cb.answer("Картинки устарели — начните заново", show_alert=True)
        return
    _, vid, vo = scenes_price(data["sc_total"], data.get("sc_lang") or None, data.get("sc_tpl") in SOUND_TEMPLATES)
    if db.spent_today() + vid + vo > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Запускаю")
    BUSY.add(cb.from_user.id)
    asyncio.create_task(run_scenes(bot, cb.message.chat.id, cb.from_user, data, state, vid))


async def run_scenes(bot: Bot, chat_id: int, user, data: dict, state: FSMContext, vid_price: float):
    """Оживляет сцены по очереди, склеивает, накладывает голос по сценам."""
    m = VIDEO_MODELS[SCENE_MODEL]
    scenes, imgs = data["sc_board"]["scenes"], data["sc_imgs"]
    total, lang = data["sc_total"], data.get("sc_lang") or None
    n, sec = SCENE_PLANS[total]
    temp: list[Path] = []
    cleanup_old_tmp()
    status = await bot.send_message(chat_id, "⏳ Начинаю…")
    log.info("Сцены старт %s: %s с, язык %s", user.id, total, lang)
    try:
        voices: list = []
        sound = data.get("sc_tpl") in SOUND_TEMPLATES
        quiet_m = VIDEO_MODELS["seed"] if sound else m   # без голоса: со звуком (хруст) или Kling без звука
        await status.edit_text(f"🎬 Оживляю {n} сцен сразу (обычно 3–8 минут). Можно пользоваться ботом.")

        reuse = data.get("sc_reuse") or {}

        ready = data.get("sc_voice_mp3") or []
        scene_aud: dict[int, Path] = {}

        async def one(i: int, s: dict, img: str) -> Path:
            old = reuse.get(str(i))
            if old and Path(old).exists() and not (lang and s.get("voice")):
                return Path(old)   # сцена без голоса — берём готовый клип, без новой генерации
            img_url = await fal_api.upload(Path(img))
            if lang and s.get("voice"):
                pre = ready[i] if i < len(ready) and ready[i] and Path(ready[i]).exists() else ""
                if pre:
                    mp3 = Path(pre)
                else:
                    mp3 = await make_voice(s["voice"], lang)
                    temp.append(mp3)
                    db.log_gen(user.id, user.full_name, "voice", "tts", 0, tts_price(s["voice"]), "ok")
                aud = await media.scene_audio(mp3, 0.2)   # длина сцены = длина реплики, без обрезки
                temp.append(aud)
                scene_aud[i] = aud
                prompt = (s["motion_prompt"] + " Only the mascot talks to the camera, lips precisely synced to the "
                          "speech, lively facial expressions and natural hand gestures. Any other person in the "
                          "frame keeps the mouth closed and does not speak. Keep the character design, body "
                          "proportions and the scene unchanged.")[:2400]
                url = await fal_api.avatar(img_url, await fal_api.upload(aud), prompt)
            else:
                url = await fal_api.video_part(quiet_m, img_url, s["motion_prompt"], sec)
            clip = await media.download(url, "mp4")
            temp.append(clip)
            return clip

        clips = list(await asyncio.gather(*(one(i, s, img) for i, (s, img) in enumerate(zip(scenes, imgs)))))
        t = 0.0
        for i, c in enumerate(clips):   # голос — на реальное начало каждой сцены
            d = await media.duration(c)
            if i in scene_aud:
                voices.append((scene_aud[i], t, None))
            t += d
        kept = _keep_scenes(user.id, imgs, clips, scenes, lang)
        await status.edit_text("🎞 Монтирую…")
        joined = await media.concat(clips, keep_audio=sound)
        temp.append(joined)
        db.log_gen(user.id, user.full_name, "scenes", "avatar" if voices else quiet_m.key, total, vid_price, "ok")
        final, track = joined, None
        if sound and await media.has_audio(joined):
            voices = []   # звук уже в сценах: голос (в говорящих) и хруст (в остальных)
        if voices:
            track = await media.build_track(voices, await media.duration(joined))
            temp.append(track)
            final = await media.mux_voice(joined, track)
            temp.append(final)
        sb = data["sc_board"]
        hook = (sb.get("hooks") or [{}])[sb.get("hook_i", 0)].get("overlay", "")
        hooked = await media.overlay_hook(final, hook)
        if hooked != final:
            temp.append(hooked)
            final = hooked
        await remember(state, user.id, joined, "scenes", sb.get("summary_ru", ""), track)
        out = await media.fit_telegram(final)
        temp.append(out)
        sec_total = await media.duration(out)
        markup = after_voice_kb("scenes", sec_total) if track else kb([("🔊 Озвучить", "voice")])
        if lang and track and kept:
            other = OTHER[lang]
            nv = sum(1 for s in scenes if s.get("voice"))
            price = AVATAR_USD_PER_SEC * sec * nv + 0.01 * nv
            await state.update_data(sc_kept=kept)
            markup.inline_keyboard.append([InlineKeyboardButton(
                text=f"{LANGS[other][:2]} {'Узбекская' if other == 'uz' else 'Русская'} версия — ≈ {usd(price)}",
                callback_data=f"sclang2:{other}")])
        await send_vid(bot, chat_id, FSInputFile(out, filename="laza_scenes.mp4"), supports_streaming=True,
                             caption=f"✅ Ролик с Лазой, {sec_total:.0f} с, {n} сцен", reply_markup=markup)
        if sb.get("caption"):
            tags = " ".join(t if t.startswith("#") else "#" + t for t in (sb.get("hashtags") or []))
            await bot.send_message(chat_id, "📝 Подпись для Instagram (нажмите, чтобы скопировать):\n\n"
                                            f"<code>{html.escape(sb['caption'] + (chr(10) * 2 + tags if tags else ''))}</code>",
                                   parse_mode="HTML")
        log.info("Сцены готовы %s", user.id)
    except fal_api.GenError as e:
        await bot.send_message(chat_id, f"⚠️ {e}")
    except Exception:
        log.exception("Ошибка сцен")
        await bot.send_message(chat_id, "⚠️ Что-то пошло не так при монтаже. Попробуйте ещё раз.")
    finally:
        BUSY.discard(user.id)
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        media.cleanup(*temp)



def _keep_scenes(uid: int, imgs: list, clips: list, scenes: list, lang: str | None) -> dict:
    """Сохраняет картинки и клипы сцен без голоса — для второй языковой версии без повторной оплаты."""
    try:
        keep = {Path(c) for c in clips} | {Path(p) for p in imgs}
        for old in SCN_DIR.glob(f"{uid}_*"):
            if old not in keep:
                old.unlink(missing_ok=True)
        kept = {"imgs": [], "clips": {}}
        for i, (img, clip, s) in enumerate(zip(imgs, clips, scenes)):
            dst_img = SCN_DIR / f"{uid}_img{i}.jpg"
            if Path(img) != dst_img:
                shutil.copy(img, dst_img)
            kept["imgs"].append(str(dst_img))
            if not (lang and s.get("voice")):
                dst = SCN_DIR / f"{uid}_clip{i}.mp4"
                if Path(clip) != dst:
                    shutil.copy(clip, dst)
                kept["clips"][str(i)] = str(dst)
        return kept
    except Exception:  # noqa: BLE001
        log.warning("Не сохранил сцены для второй версии")
        return {}


@router.callback_query(F.data.startswith("sclang2:"), Allowed())
async def sc_second_lang(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Вторая языковая версия: те же картинки, клипы без голоса — повторно; говорящие сцены — заново."""
    lang = cb.data.split(":")[1]
    data = await state.get_data()
    kept, sb = data.get("sc_kept") or {}, data.get("sc_board")
    if not sb or not kept or not all(Path(p).exists() for p in kept.get("imgs", [])):
        await cb.answer("Сцены устарели — сделайте ролик заново", show_alert=True)
        return
    if cb.from_user.id in BUSY:
        await cb.answer("Предыдущая генерация ещё идёт", show_alert=True)
        return
    n, sec = SCENE_PLANS[data["sc_total"]]
    nv = sum(1 for s in sb["scenes"] if s.get("voice"))
    price = AVATAR_USD_PER_SEC * sec * nv
    if db.spent_today() + price > DAILY_LIMIT_USD:
        await cb.answer(f"Дневной лимит {usd(DAILY_LIMIT_USD)} будет превышен", show_alert=True)
        return
    await cb.answer("Перевожу и запускаю")
    BUSY.add(cb.from_user.id)
    msg = await cb.message.answer(f"✍️ Перевожу реплики на {LANGS[lang]}…")
    try:
        tr = await prompts.translate_board(sb, lang, sec)
    except Exception:
        log.exception("Ошибка перевода")
        BUSY.discard(cb.from_user.id)
        await msg.edit_text("⚠️ Не получилось перевести. Попробуйте ещё раз.")
        return
    import copy
    sb2 = copy.deepcopy(sb)
    for s, v, vr in zip(sb2["scenes"], tr["voices"], tr.get("voices_ru") or tr["voices"]):
        if s.get("voice"):
            s["voice"], s["voice_ru"] = v, vr
    sb2["hooks"] = [{"mechanism_ru": "", "overlay": tr.get("overlay", ""), "voice": "", "voice_ru": ""}]
    sb2["hook_i"] = 0
    sb2["caption"], sb2["hashtags"] = tr.get("caption", ""), tr.get("hashtags", [])
    lines = [f"{i + 1}. «{shown(s['voice'])}»" for i, s in enumerate(sb2["scenes"]) if s.get("voice")]
    await msg.edit_text(f"{LANGS[lang]} — реплики:\n" + "\n".join(lines) + "\n\n🎬 Делаю говорящие сцены…")
    data2 = dict(data, sc_board=sb2, sc_lang=lang, sc_imgs=kept["imgs"], sc_reuse=kept.get("clips", {}),
                 sc_voice_mp3=[])
    await state.update_data(sc_board=sb2, sc_lang=lang)
    asyncio.create_task(run_scenes(bot, cb.message.chat.id, cb.from_user, data2, state, price))

# ---------- Руководитель: голос Лазы ----------
SAMPLES = {"ru": "Слушай, я сам не поверил... Хрустит! Как чипсы, только сладкое. Попробуй — и поймёшь, о чём я.",
           "uz": "Eshit, o'zim ham ishonmadim... Qarsillaydi! Chipsdek, faqat shirin. Tatib ko'r — tushunasan."}


@router.message(Command("voice"), Admin())
async def voice_choose(msg: Message):
    if GEMINI_API_KEY:
        rows = []
        for lang in ("ru", "uz"):
            cur = voice.gemini_voice(lang) if voice.use_gemini(lang) else ""
            names = list(GEMINI_VOICES)
            for i in range(0, len(names), 3):
                rows.append([(("✅ " if v == cur else "") + f"{LANGS[lang][:2]} {v}", f"vsample:gm:{lang}:{v}")
                             for v in names[i:i + 3]])
        if voice.use_azure():
            rows.append([(f"{LANGS[l][:2]} Azure {voice.azure_title(l)}", f"vsample:az:{l}:{voice.azure_voice(l)}")
                         for l in ("ru", "uz")])
        await msg.answer("Голос Лазы. Google — живые голоса, Azure — дикторские.\n"
                         f"Сейчас: русский — {voice.engine('ru')}, узбекский — {voice.engine('uz')}.\n\n"
                         "Нажмите на голос — пришлю пример, затем кнопкой выберите его.\n"
                         + "\n".join(f"{v} — {d}" for v, d in GEMINI_VOICES.items()), reply_markup=kb(*rows))
        return
    if voice.use_azure():
        rows = []
        for lang in ("ru", "uz"):
            cur = voice.azure_voice(lang)
            rows.append([(("✅ " if v == cur else "") + f"{LANGS[lang][:2]} {t}", f"vsample:az:{lang}:{v}")
                         for t, v in AZURE_VOICES[lang].items()])
        await msg.answer(f"Голос Лазы (Azure, носители языка):\nрусский — {voice.azure_title('ru')}\n"
                         f"узбекский — {voice.azure_title('uz')}\n\nНажмите на голос — пришлю пример "
                         "(бесплатно), затем кнопкой выберите его.", reply_markup=kb(*rows))
        return
    rows = [[(("✅ " if v == db.get_voice() else "") + v, f"vsample:el:ru:{v}")] for v in VOICE_CHOICES]
    await msg.answer(f"Голос Лазы: ElevenLabs · {db.get_voice()} (узбекский с акцентом).\n"
                     "Чтобы говорил без акцента, добавьте ключ Azure (AZURE_SPEECH_KEY в .env).\n"
                     "Нажмите на голос — пришлю пример (≈ 0,01 $).", reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("vsample:"), Allowed())
async def voice_sample(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для руководителя", show_alert=True)
        return
    _, eng, lang, name = cb.data.split(":", 3)
    await cb.answer("Готовлю пример")
    try:
        mp3 = await voice.speak(SAMPLES[lang], lang, name,
                                force={"gm": "gemini", "az": "azure"}.get(eng))
        if eng == "el":
            db.log_gen(cb.from_user.id, cb.from_user.full_name, "voice", "eleven-v3", 0, 0.01, "ok")
        title = next((t for t, v in AZURE_VOICES[lang].items() if v == name), name) if eng == "az" else name
        await cb.message.answer_audio(FSInputFile(mp3, filename=f"{lang}.mp3"), title=f"{LANGS[lang]} · {title}",
                                      reply_markup=kb([(f"Выбрать {title}", f"vset:{eng}:{lang}:{name}")]))
        media.cleanup(mp3)
    except fal_api.GenError as e:
        await cb.message.answer(f"⚠️ {e}")


@router.callback_query(F.data.startswith("vset:"), Allowed())
async def voice_set(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для руководителя", show_alert=True)
        return
    _, eng, lang, name = cb.data.split(":", 3)
    if eng == "gm":
        db.set_setting(f"gemini_{lang}", name)
        db.set_setting(f"engine_{lang}", "gemini")
    elif eng == "az":
        db.set_setting(f"azure_{lang}", name)
        db.set_setting(f"engine_{lang}", "azure")
    else:
        db.set_voice(name)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await cb.message.answer(f"✅ Голос Лазы ({LANGS[lang]}): {voice.engine(lang)}")


STRESS_SAMPLES = {"ru": "Привет, я Лаза! Попробуй Лазу. Вкусная Лаза.",
                  "uz": "Salom, men Laza! Laza'ni tatib ko'r. Mazali Laza."}


@router.message(Command("stress"), Admin())
async def stress_choose(msg: Message):
    await msg.answer("Ударение в имени Лаза. Выберите язык — пришлю несколько вариантов произношения, "
                     "выберите тот, где ударение на последнюю А.",
                     reply_markup=kb([(LANGS["ru"], "stl:ru"), (LANGS["uz"], "stl:uz")]))


@router.callback_query(F.data.startswith("stl:"), Allowed())
async def stress_samples(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для руководителя", show_alert=True)
        return
    lang = cb.data.split(":")[1]
    await cb.answer("Готовлю варианты")
    cur = voice.stress_way(lang)
    for way, title in voice.STRESS_WAYS[lang].items():
        if way.startswith("ipa") and (voice.use_gemini(lang) or not voice.use_azure()):
            continue
        try:
            mp3 = await voice.speak(STRESS_SAMPLES[lang], lang, way=way)
        except fal_api.GenError as e:
            await cb.message.answer(f"⚠️ {title}: {e}")
            continue
        mark = "✅ " if way == cur else ""
        await cb.message.answer_audio(FSInputFile(mp3, filename=f"{lang}_{way}.mp3"),
                                      title=f"{mark}{title}", caption=f"{mark}{title} · {voice.engine(lang)}",
                                      reply_markup=kb([("Выбрать этот", f"sts:{lang}:{way}")]))
        media.cleanup(mp3)


@router.callback_query(F.data.startswith("sts:"), Allowed())
async def stress_set(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для руководителя", show_alert=True)
        return
    _, lang, way = cb.data.split(":")
    db.set_setting(voice.stress_key(lang), way)
    await cb.answer("Сохранено")
    await cb.message.answer(f"✅ {LANGS[lang]}: ударение — {voice.STRESS_WAYS[lang][way]}. "
                            "Так будет во всех озвучках.")


# ---------- Руководитель: доступ и эталоны ----------
@router.message(Command("add"), Admin())
async def add_user(msg: Message, command: CommandObject):
    args = (command.args or "").split(maxsplit=1)
    if not args or not args[0].isdigit():
        await msg.answer("Формат: /add 123456789 Имя")
        return
    db.add_user(int(args[0]), args[1] if len(args) > 1 else args[0], msg.from_user.id)
    log.info("Доступ выдан %s админом %s", args[0], msg.from_user.id)
    await msg.answer(f"✅ Доступ выдан: {args[0]}. Пусть нажмёт /start в боте.")


@router.message(Command("del"), Admin())
async def del_user(msg: Message, command: CommandObject):
    uid = (command.args or "").strip()
    ok = uid.isdigit() and db.del_user(int(uid))
    await msg.answer("✅ Доступ убран." if ok else "Такого id нет в списке. Формат: /del 123456789")


@router.message(Command("users"), Admin())
async def users(msg: Message):
    rows = db.list_users()
    text = "\n".join(f"• {n} — {i}" for i, n in rows) or "Пока никого не добавляли."
    await msg.answer("Доступ к боту (кроме руководителей из настроек):\n" + text)


@router.message(Command("refs"), Admin())
async def show_refs(msg: Message):
    r = refs()
    src = "добавленные вами" if sorted(REFS_CUSTOM_DIR.glob("*.jpg")) else "стандартные"
    await msg.answer(f"Эталоны персонажа ({src}): {len(r)} шт.")
    for p in r:
        await msg.answer_photo(FSInputFile(p))


@router.message(Command("ref_add"), Admin())
async def ref_add(msg: Message, state: FSMContext):
    await state.set_state(Flow.ref)
    await msg.answer("Пришлите картинку с Лазой (хорошо видно целиком, без лишнего текста).\n"
                     "Пока вы не добавите свои эталоны, используются стандартные. Не больше 6 штук.")


@router.message(Flow.ref, NOT_MENU, Admin())
async def ref_save(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Нужна картинка. /cancel — отмена.")
        return
    if len(list(REFS_CUSTOM_DIR.glob("*.jpg"))) >= 6:
        await msg.answer("Уже 6 эталонов. Сначала /ref_reset.")
        await state.clear()
        return
    tmp = await save_photo(bot, msg)
    dst = REFS_CUSTOM_DIR / f"ref_{int(time.time())}.jpg"
    tmp.replace(dst)
    await state.clear()
    await msg.answer(f"✅ Эталон сохранён. Своих эталонов: {len(list(REFS_CUSTOM_DIR.glob('*.jpg')))}.",
                     reply_markup=MENU)


@router.message(Command("ref_reset"), Admin())
async def ref_reset(msg: Message):
    for p in REFS_CUSTOM_DIR.glob("*.jpg"):
        p.unlink()
    await msg.answer("✅ Вернул стандартные эталоны.")


# ---------- Прочее ----------
@router.message(Allowed())
async def fallback(msg: Message):
    await msg.answer("Выберите действие кнопкой внизу.", reply_markup=MENU)


@router.errors()
async def on_error(event: ErrorEvent):
    if "query is too old" in str(event.exception):   # поздний ответ на кнопку — безвреден, работа уже сделана
        log.warning("Поздний ответ на кнопку: %s", event.exception)
        return
    log.exception("Ошибка при обработке: %s", event.exception)
    upd = event.update
    m = upd.message or (upd.callback_query.message if upd.callback_query else None)
    if m:
        await m.answer("⚠️ Что-то пошло не так. Попробуйте ещё раз или нажмите /start.")


def cleanup_old_tmp(hours: int = 24) -> None:
    """Удаляет временные файлы старше суток."""
    from config import TMP_DIR
    limit = time.time() - hours * 3600
    for p in TMP_DIR.glob("*"):
        if p.stat().st_mtime < limit:
            p.unlink(missing_ok=True)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в .env")
    db.init()
    cleanup_old_tmp()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.callback_query.middleware(AntiDoubleClick())
    dp.include_router(router)
    log.info("Бот LAZA Видео запущен. Админы: %s", ADMIN_IDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
