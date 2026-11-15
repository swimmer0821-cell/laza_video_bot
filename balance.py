"""Остаток денег: fal.ai — настоящий баланс по API, Higgsfield — оценка.

fal.ai: GET https://api.fal.ai/v1/account/billing?expand=credits (ключ FAL_KEY должен иметь право
billing:usage:read — у ключей с набором прав FULL, READONLY или BILLING оно есть).
Higgsfield API не отдаёт баланс. Руководитель пишет боту /hf_balance 12.50 (сумму из console.higgsfield.ai),
бот запоминает её и время, а потом вычитает свои траты через Higgsfield с этого момента.
"""
import logging
import time
from datetime import datetime

import httpx

import db
from config import DAILY_LIMIT_USD, FAL_KEY, KLING_VIA_HF

FAL_URL = "https://api.fal.ai/v1/account/billing"
HF_MODELS = {"genjutsu-swap", "genjutsu-light", "kling3-motion", "wan3", "k4"}   # что идёт через Higgsfield
_cache: dict[str, tuple[float, float | None]] = {}   # кэш баланса fal.ai на минуту, чтобы не дёргать API


async def fal_balance() -> float | None:
    """Настоящий остаток fal.ai в $ или None, если узнать не получилось."""
    hit = _cache.get("fal")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    val = None
    if FAL_KEY:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(FAL_URL, params={"expand": "credits"}, headers={"Authorization": f"Key {FAL_KEY}"})
            if r.status_code == 200:
                val = float(r.json()["credits"]["current_balance"])
            else:
                logging.warning("fal.ai баланс: ответ %s", r.status_code)
        except Exception:  # noqa: BLE001 — баланс не главное, бот работает и без него
            logging.exception("fal.ai баланс: ошибка")
    _cache["fal"] = (time.time(), val)
    return val


def _is_hf(model: str) -> bool:
    return model in HF_MODELS or (model == "kling" and KLING_VIA_HF)


def hf_estimate() -> tuple[float, str] | None:
    """(примерный остаток Higgsfield в $, дата ввода суммы) или None, если сумму ещё не вводили."""
    raw = db.get_setting("hf_balance", "")
    if not raw or "|" not in raw:
        return None
    amount, ts = raw.split("|", 1)
    spent = db.spent_since_where(datetime.fromisoformat(ts), _is_hf)
    return round(float(amount) - spent, 2), ts[:16].replace("T", " ")


def set_hf(amount: float) -> None:
    db.set_setting("hf_balance", f"{amount}|{datetime.now(db.TZ).isoformat(timespec='seconds')}")


async def text(short: bool = False) -> str:
    """Блок «Остаток денег». short — одна-две строки для сообщения с ценой."""
    fal = await fal_balance()
    hf = hf_estimate()
    left_today = max(0.0, DAILY_LIMIT_USD - db.spent_today())
    fal_s = f"{fal:.2f} $" if fal is not None else "не удалось узнать"
    hf_s = f"≈ {hf[0]:.2f} $" if hf else "не задан"
    if short:
        return f"Остаток: fal.ai {fal_s} · Higgsfield {hf_s} · лимит на сегодня {left_today:.2f} $"
    lines = ["💵 Остаток денег",
             f"• fal.ai: {fal_s}" + ("" if fal is not None else " (нужен ключ с правом читать баланс)"),
             f"• Higgsfield API: {hf_s}" + (f" (введено {hf[1]}, минус траты бота)" if hf else
                                             " — руководитель: /hf_balance сумма из console.higgsfield.ai"),
             f"• Дневной лимит бота: осталось {left_today:.2f} из {DAILY_LIMIT_USD:.2f} $"]
    return "\n".join(lines)
