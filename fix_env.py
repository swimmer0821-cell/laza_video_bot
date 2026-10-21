"""Чинит файл .env после ручной правки: убирает отступы, пробелы вокруг «=»,
строки-заглушки («ваш_ключ…») и повторы (остаётся последнее настоящее значение).
Если nano аварийно закрылся и оставил .env.save — берёт ключи оттуда.
Запускается из update.sh. Значения ключей на экран не выводит."""
from pathlib import Path

APP = Path(__file__).resolve().parent
ENV = APP / ".env"


def is_stub(value: str) -> bool:
    v = value.strip().strip('"').strip("'").lower()
    return not v or "ваш" in v or "your" in v or v.startswith("<") or v in {"...", "xxx"}


def parse(path: Path) -> list[tuple[str, str]]:
    out = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def main() -> None:
    if not ENV.exists():
        print("== .env не найден — пропускаю")
        return
    pairs = parse(ENV)
    saves = sorted(APP.glob(".env.save*")) + sorted(APP.glob(".env*.save"))
    for s in saves:
        pairs += parse(s)
    values: dict[str, str] = {}
    for k, v in pairs:
        if not is_stub(v) or k not in values:
            values[k] = v
    values = {k: v for k, v in values.items() if not is_stub(v) or k in {"ALLOWED_IDS"}}
    new = "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
    if new != ENV.read_text(encoding="utf-8", errors="ignore"):
        bak = APP / ".env.bak"
        bak.write_text(ENV.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        bak.chmod(0o600)
        ENV.write_text(new, encoding="utf-8")
        ENV.chmod(0o600)
        print("== .env исправлен (копия старого — .env.bak)")
    for s in saves:
        s.unlink(missing_ok=True)
    az = values.get("AZURE_SPEECH_KEY", "")
    print(f"== Azure: ключ {'есть' if az else 'НЕТ'}, регион {values.get('AZURE_SPEECH_REGION', 'не указан')}")


if __name__ == "__main__":
    main()
