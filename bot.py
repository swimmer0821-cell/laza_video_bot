"""Бот «LAZA Видео»: ролики из фото товара и персонаж Лаза. Генерация — fal.ai, промты — Claude."""
import asyncio
import logging
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
import media
import prompts
import voice
from config import (ADMIN_IDS, BOT_TOKEN, DAILY_LIMIT_USD, DATA_DIR, DURATIONS, IMAGE_PRICE_USD, LANGS,
                    LIPSYNC_USD_PER_SEC, LOG_DIR, AZURE_VOICES, REFS_CUSTOM_DIR, REFS_DEFAULT_DIR, SCENE_MODEL, SCENE_PLANS,
                    SCENE_TEMPLATE, TTS_USD_PER_1K, VIDEO_MODELS, VOICE_CHOICES)

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
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_VIDEO), KeyboardButton(text=BTN_CHAR)],
                                     [KeyboardButton(text=BTN_SCENES)],
                                     [KeyboardButton(text=BTN_SPENT), KeyboardButton(text=BTN_HELP)]],
                           resize_keyboard=True)
NOT_MENU = ~F.text.in_({BTN_VIDEO, BTN_CHAR, BTN_SCENES, BTN_SPENT, BTN_HELP})   # кнопки меню работают всегда
LAST_DIR = DATA_DIR / "last"          # последний ролик каждого человека (для озвучки и синхронизации губ)
LAST_DIR.mkdir(parents=True, exist_ok=True)


class Flow(StatesGroup):
    photo = State()        # ждём фото товара
    idea = State()         # ждём идею ролика
    char_idea = State()    # ждём идею сцены с персонажем
    char_photo = State()   # ждём фото товара для сцены (или «без фото»)
    pick = State()         # выбор длины / модели / подтверждение
    ref = State()          # админ присылает эталон персонажа
    voice_text = State()   # ждём свой текст для озвучки
    sc_idea = State()      # ждём идею ролика из сцен


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
                     "🔊 Любой готовый ролик можно озвучить на русском или узбекском.\n\n"
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
              f"• Синхронизация губ — ≈ {usd(LIPSYNC_USD_PER_SEC * 5)} за 5 с", "",
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


@router.message(Flow.photo, NOT_MENU, Allowed())
async def video_photo(msg: Message, state: FSMContext, bot: Bot):
    if not is_image(msg):
        await msg.answer("Нужна фотография. Пришлите фото товара или /cancel для отмены.")
        return
    raw = await save_photo(bot, msg)
    if await asyncio.to_thread(media.is_vertical, raw):
        vert = await asyncio.to_thread(media.crop_vertical, raw)
    else:
        vert = await extend_photo(msg, raw)
    media.cleanup(raw)
    await state.update_data(src=str(vert), kind="product")
    await state.set_state(Flow.idea)
    await msg.answer("Что должно происходить в ролике? Напишите по-русски, например:\n"
                     "«кубики мороженого падают на стол и хрустят, камера медленно приближается»",
                     reply_markup=kb([("🎲 Придумай сам", "idea:auto")]))


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
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(char_product="")
    await make_char_image(cb.message, state, cb.from_user)
    await cb.answer()


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
    rows = [[(f"{m.title} — {usd(m.price(sec))}", f"mdl:{m.key}")] for m in VIDEO_MODELS.values()]
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
        await bot.send_video(chat_id, FSInputFile(final, filename="laza.mp4"), supports_streaming=True,
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
        await bot.send_video(chat_id, FSInputFile(final, filename="laza_voice.mp4"), supports_streaming=True,
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
        await bot.send_video(chat_id, FSInputFile(final, filename="laza_lips.mp4"), supports_streaming=True,
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


# ---------- 🎞 Ролик с Лазой из нескольких сцен ----------
def scenes_price(total: int, lang: str | None) -> tuple[float, float, float]:
    """(картинки, видео, голос) в долларах."""
    n, sec = SCENE_PLANS[total]
    m = VIDEO_MODELS[SCENE_MODEL]
    return (round(IMAGE_PRICE_USD * n, 2), round(m.usd_per_sec * sec * n, 2),
            0.03 if lang else 0.0)


@router.message(F.text == BTN_SCENES, Allowed())
async def sc_start(msg: Message, state: FSMContext):
    await state.set_state(Flow.sc_idea)
    await msg.answer("О чём ролик? Опишите сцены по-русски через стрелки, например:\n"
                     "«Лаза здоровается → показывает фисташковое мороженое → зовёт на дегустацию»\n\n"
                     "Или возьмите готовый сюжет:", reply_markup=kb([("🎲 Знакомство с Laza", "sc:tpl")]))


@router.message(Flow.sc_idea, F.text, NOT_MENU, Allowed())
async def sc_idea(msg: Message, state: FSMContext):
    await state.update_data(sc_idea=msg.text.strip())
    await sc_ask_len(msg, state)


@router.callback_query(F.data == "sc:tpl", Allowed())
async def sc_tpl(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(sc_idea=SCENE_TEMPLATE)
    await sc_ask_len(cb.message, state)
    await cb.answer()


async def sc_ask_len(msg: Message, state: FSMContext):
    await state.set_state(None)
    rows = []
    for total, (n, sec) in SCENE_PLANS.items():
        img, vid, _ = scenes_price(total, "ru")
        rows.append([(f"{total} с · {n} сцены по {sec} с — ≈ {usd(img + vid)}", f"sclen:{total}")])
    await msg.answer("Длина ролика? (видео — Kling 3.0, цена с картинками)", reply_markup=kb(*rows))


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
        sb = await prompts.storyboard(refs(), data["sc_idea"], n, sec, lang)
    except Exception:
        log.exception("Ошибка раскадровки")
        await msg.edit_text("⚠️ Не получилось написать сценарий.", reply_markup=kb([("🔁 Ещё раз", "scplan")]))
        return
    img, vid, vo = scenes_price(total, lang)
    lines = [f"🎞 {sb.get('summary_ru', '')}", ""]
    for i, s in enumerate(sb["scenes"], 1):
        line = f"{i}. {s.get('title_ru', '')} ({sec} с)"
        if s.get("voice"):
            line += f"\n   🗣 «{shown(s['voice'])}»"
            if lang != "ru" and s.get("voice_ru"):
                line += f"\n   ({s['voice_ru']})"
        lines.append(line)
    lines += ["", f"Шаг 1 — картинки сцен: ≈ {usd(img)}",
              f"Шаг 2 — оживление и монтаж: ≈ {usd(vid + vo)}",
              "После картинок можно перерисовать любую сцену до оживления."]
    await state.update_data(sc_board=sb, sc_imgs=[])
    await msg.edit_text("\n".join(lines), reply_markup=kb([(f"🎨 Нарисовать сцены — ≈ {usd(img)}", "scdraw")],
                                                           [("🔁 Другой сценарий", "scplan"),
                                                            ("❌ Отмена", "cancel")]))


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
        _, vid, vo = scenes_price(total, lang)
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
    data = await state.get_data()
    imgs = data.get("sc_imgs") or []
    if not imgs or not all(p and Path(p).exists() for p in imgs):
        await cb.answer("Картинки устарели — начните заново", show_alert=True)
        return
    _, vid, vo = scenes_price(data["sc_total"], data.get("sc_lang") or None)
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
        clips = []
        for i, (s, img) in enumerate(zip(scenes, imgs), 1):
            await status.edit_text(f"⏳ Оживляю сцену {i} из {n}… (2–5 минут каждая). Можно пользоваться ботом.")
            url = await fal_api.video_part(m, await fal_api.upload(Path(img)), s["motion_prompt"], sec)
            clip = await media.download(url, "mp4")
            clips.append(clip)
            temp.append(clip)
        await status.edit_text("🎞 Монтирую…")
        joined = await media.concat(clips)
        temp.append(joined)
        db.log_gen(user.id, user.full_name, "scenes", m.key, total, vid_price, "ok")
        final, track = joined, None
        if lang and any(s.get("voice") for s in scenes):
            await status.edit_text("🔊 Озвучиваю…")
            voices = []
            for i, s in enumerate(scenes):
                if s.get("voice"):
                    mp3 = await make_voice(s["voice"], lang)
                    temp.append(mp3)
                    voices.append((mp3, i * sec + 0.2, sec - 0.2))
                    db.log_gen(user.id, user.full_name, "voice", "eleven-v3", 0, tts_price(s["voice"]), "ok")
            track = await media.build_track(voices, await media.duration(joined))
            temp.append(track)
            final = await media.mux_voice(joined, track)
            temp.append(final)
        await remember(state, user.id, joined, "scenes", data["sc_board"].get("summary_ru", ""), track)
        out = await media.fit_telegram(final)
        temp.append(out)
        sec_total = await media.duration(out)
        markup = after_voice_kb("scenes", sec_total) if track else kb([("🔊 Озвучить", "voice")])
        await bot.send_video(chat_id, FSInputFile(out, filename="laza_scenes.mp4"), supports_streaming=True,
                             caption=f"✅ Ролик с Лазой, {total} с, {n} сцены", reply_markup=markup)
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


# ---------- Руководитель: голос Лазы ----------
SAMPLES = {"ru": "Привет! Я Лаза — настоящее сублимированное мороженое без посредников. Попробуй Лазу!",
           "uz": "Salom! Men Laza — vositachilarsiz haqiqiy sublimatsiya qilingan muzqaymoq. Laza'ni tatib ko'r!"}


@router.message(Command("voice"), Admin())
async def voice_choose(msg: Message):
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
        mp3 = await voice.speak(SAMPLES[lang], lang, name)
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
    if eng == "az":
        db.set_setting(f"azure_{lang}", name)
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
        if way.startswith("ipa") and not voice.use_azure():
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
    db.set_setting(f"stress_{lang}", way)
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
