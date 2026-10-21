"""Бот «LAZA Видео»: ролики из фото товара и персонаж Лаза. Генерация — fal.ai, промты — Claude."""
import asyncio
import logging
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
from config import (ADMIN_IDS, BOT_TOKEN, DAILY_LIMIT_USD, DURATIONS, IMAGE_PRICE_USD, LOG_DIR,
                    REFS_CUSTOM_DIR, REFS_DEFAULT_DIR, VIDEO_MODELS)

# ---------- Лог: в файл (5 МБ × 3) и в консоль ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5_000_000,
                                                  backupCount=3, encoding="utf-8"),
                              logging.StreamHandler()])
log = logging.getLogger("video_bot")

router = Router()
BUSY: set[int] = set()          # у кого сейчас идёт генерация (одна за раз на человека)

BTN_VIDEO, BTN_CHAR, BTN_SPENT, BTN_HELP = "🎬 Видео из фото", "🧸 Персонаж Лаза", "💰 Расходы", "❓ Помощь"
MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_VIDEO), KeyboardButton(text=BTN_CHAR)],
                                     [KeyboardButton(text=BTN_SPENT), KeyboardButton(text=BTN_HELP)]],
                           resize_keyboard=True)
NOT_MENU = ~F.text.in_({BTN_VIDEO, BTN_CHAR, BTN_SPENT, BTN_HELP})   # кнопки меню работают в любой момент


class Flow(StatesGroup):
    photo = State()        # ждём фото товара
    idea = State()         # ждём идею ролика
    char_idea = State()    # ждём идею сцены с персонажем
    char_photo = State()   # ждём фото товара для сцены (или «без фото»)
    pick = State()         # выбор длины / модели / подтверждение
    ref = State()          # админ присылает эталон персонажа


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
                     f"{BTN_CHAR} — Лаза в любой сцене, потом оживляем.\n\n"
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
    lines += [f"• Картинка с персонажем — ≈ {usd(IMAGE_PRICE_USD)}", "",
              "Советы: фото товара — чёткое, при хорошем свете, товар крупно. "
              "Надписи, цены и логотип добавляйте потом в CapCut: ИИ искажает текст.",
              f"Лимит трат на всех за сутки: {usd(DAILY_LIMIT_USD)}."]
    if msg.from_user.id in ADMIN_IDS:
        lines += ["", "Для руководителя:", "/add id Имя — дать доступ", "/del id — убрать доступ",
                  "/users — список", "/refs — эталоны персонажа", "/ref_add — добавить эталон",
                  "/ref_reset — вернуть стандартные эталоны"]
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
    asyncio.create_task(run_video(bot, cb.message.chat.id, cb.from_user, data))


async def run_video(bot: Bot, chat_id: int, user, data: dict):
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
        await bot.send_video(chat_id, FSInputFile(final, filename="laza.mp4"), supports_streaming=True,
                             caption=f"✅ {m.title}, {data['sec']} с, ≈ {usd(data['price'])}",
                             reply_markup=kb([("🔁 Ещё вариант", "again"), ("✏️ Другая идея", "newidea")]))
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
