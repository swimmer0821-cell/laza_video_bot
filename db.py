"""SQLite: кто имеет доступ и сколько потрачено на генерации."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import ALLOWED_IDS, ADMIN_IDS, DB_PATH, DEFAULT_VOICE, SETTINGS_PATH

TZ = ZoneInfo("Asia/Tashkent")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init() -> None:
    """Создаёт таблицы при первом запуске."""
    with closing(_conn()) as c, c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, added_by INTEGER, added_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS gens(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, user_name TEXT,
            kind TEXT, model TEXT, seconds INTEGER, cost REAL, status TEXT, result_url TEXT)""")


def is_allowed(uid: int) -> bool:
    if uid in ADMIN_IDS or uid in ALLOWED_IDS:
        return True
    with closing(_conn()) as c:
        return c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None


def add_user(uid: int, name: str, by: int) -> None:
    with closing(_conn()) as c, c:
        c.execute("INSERT OR REPLACE INTO users VALUES(?,?,?,?)",
                  (uid, name, by, datetime.now(TZ).isoformat(timespec="seconds")))


def del_user(uid: int) -> bool:
    with closing(_conn()) as c, c:
        return c.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount > 0


def list_users() -> list[tuple[int, str]]:
    with closing(_conn()) as c:
        return c.execute("SELECT id, name FROM users ORDER BY added_at").fetchall()


def log_gen(uid: int, uname: str, kind: str, model: str, seconds: int, cost: float,
            status: str, url: str = "") -> None:
    """Записывает генерацию. Статус ok — деньги списаны; error — обычно не списаны."""
    with closing(_conn()) as c, c:
        c.execute("INSERT INTO gens(ts,user_id,user_name,kind,model,seconds,cost,status,result_url) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (datetime.now(TZ).isoformat(timespec="seconds"), uid, uname, kind, model,
                   seconds, cost, status, url))


def spent_since(start: datetime) -> float:
    with closing(_conn()) as c:
        row = c.execute("SELECT COALESCE(SUM(cost),0) FROM gens WHERE status='ok' AND ts>=?",
                        (start.isoformat(timespec="seconds"),)).fetchone()
        return round(row[0], 2)


def spent_today() -> float:
    now = datetime.now(TZ)
    return spent_since(now.replace(hour=0, minute=0, second=0, microsecond=0))


def report() -> str:
    """Текст отчёта: сегодня, 7 дней, месяц и по людям за месяц."""
    now = datetime.now(TZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    lines = [f"Сегодня: {spent_since(day):.2f} $",
             f"7 дней: {spent_since(day - timedelta(days=6)):.2f} $",
             f"С 1-го числа: {spent_since(month):.2f} $"]
    with closing(_conn()) as c:
        rows = c.execute("SELECT user_name, COUNT(*), SUM(cost) FROM gens WHERE status='ok' AND ts>=? "
                         "GROUP BY user_id ORDER BY SUM(cost) DESC",
                         (month.isoformat(timespec="seconds"),)).fetchall()
    if rows:
        lines.append("\nЗа месяц по людям:")
        lines += [f"• {n}: {k} шт., {s:.2f} $" for n, k, s in rows]
    lines.append("\nСуммы — оценка по ценам fal.ai. Точный остаток — на fal.ai → Billing.")
    return "\n".join(lines)


def _settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get_voice() -> str:
    """Голос Лазы (выбирает руководитель командой /voice)."""
    return _settings().get("voice2", DEFAULT_VOICE)   # voice2: с 21.10.2026 по умолчанию Liam


def set_voice(name: str) -> None:
    s = _settings()
    s["voice2"] = name
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def get_setting(key: str, default: str) -> str:
    return _settings().get(key, default)


def set_setting(key: str, value: str) -> None:
    s = _settings()
    s[key] = value
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
