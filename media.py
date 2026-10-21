"""Работа с файлами: подготовка фото 9:16, последний кадр, склейка частей (ffmpeg)."""
import asyncio
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageFilter, ImageOps

from config import TMP_DIR

W, H = 1080, 1920  # вертикаль 9:16


def new_path(ext: str) -> Path:
    return TMP_DIR / f"{uuid.uuid4().hex}.{ext}"


def is_vertical(src: Path) -> bool:
    """Фото уже почти 9:16 (допуск ~10%)."""
    img = ImageOps.exif_transpose(Image.open(src))
    return abs(img.width / img.height - W / H) < 0.06


def crop_vertical(src: Path) -> Path:
    """Запасной вариант: обрезать фото по центру до 9:16 (без размытых полос)."""
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    dst = new_path("jpg")
    ImageOps.fit(img, (W, H), Image.LANCZOS).save(dst, quality=93)
    return dst


def to_vertical(src: Path) -> Path:
    """Вписывает фото в кадр 9:16: товар целиком, по бокам — размытый фон из того же фото."""
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    if abs(img.width / img.height - W / H) < 0.02:
        out = img.resize((W, H), Image.LANCZOS)
    else:
        bg = ImageOps.fit(img, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
        fg = ImageOps.contain(img, (W, H), Image.LANCZOS)
        bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        out = bg
    dst = new_path("jpg")
    out.save(dst, quality=93)
    return dst


async def download(url: str, ext: str) -> Path:
    """Скачивает результат с fal.ai в папку tmp."""
    dst = new_path(ext)
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        dst.write_bytes(r.content)
    return dst


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} ошибка: {err.decode(errors='ignore')[-400:]}")
    return out.decode(errors="ignore")


async def last_frame(video: Path) -> Path:
    """Последний кадр ролика — стартовый кадр для следующей части."""
    dst = new_path("jpg")
    await _run("ffmpeg", "-y", "-loglevel", "error", "-sseof", "-0.2", "-i", str(video),
               "-frames:v", "1", "-q:v", "2", str(dst))
    return dst


async def has_audio(video: Path) -> bool:
    out = await _run("ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                     "stream=index", "-of", "csv=p=0", str(video))
    return bool(out.strip())


async def concat(videos: list[Path]) -> Path:
    """Склеивает части в один ролик 1080×1920 (звук сохраняется, если он есть во всех частях)."""
    if len(videos) == 1:
        return videos[0]
    audio = all([await has_audio(v) for v in videos])
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for v in videos:
        args += ["-i", str(v)]
    chains, labels = [], ""
    for i in range(len(videos)):
        chains.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24[v{i}]")
        labels += f"[v{i}]" + (f"[{i}:a]" if audio else "")
    a = 1 if audio else 0
    chains.append(f"{labels}concat=n={len(videos)}:v=1:a={a}[v]" + ("[a]" if audio else ""))
    dst = new_path("mp4")
    args += ["-filter_complex", ";".join(chains), "-map", "[v]"]
    if audio:
        args += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(dst)]
    await _run(*args)
    return dst


async def fit_telegram(video: Path, limit_mb: int = 48) -> Path:
    """Telegram-бот отправляет файлы до 50 МБ: если больше — пережимаем."""
    if video.stat().st_size <= limit_mb * 1024 * 1024:
        return video
    dst = new_path("mp4")
    await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "27", "-maxrate", "10M", "-bufsize", "20M", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-b:a", "128k", "-movflags", "+faststart", str(dst))
    return dst


def cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
