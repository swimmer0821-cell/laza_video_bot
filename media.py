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


async def concat(videos: list[Path], keep_audio: bool = True) -> Path:
    """Склеивает части в один ролик 1080×1920 (звук сохраняется, если он есть во всех частях)."""
    audio = keep_audio and all([await has_audio(v) for v in videos])
    if len(videos) == 1 and (audio or not await has_audio(videos[0])):
        return videos[0]
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


async def duration(path: Path) -> float:
    """Длительность файла в секундах."""
    out = await _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path))
    return float(out.strip() or 0)


async def build_track(voices: list[tuple[Path, float, float | None]], total: float) -> Path:
    """Собирает дорожку голоса: каждая реплика (файл, начало в с, макс. длина в с или None)
    ставится на своё место. Длина дорожки — не меньше total."""
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for p, _, _ in voices:
        args += ["-i", str(p)]
    chains, labels = [], ""
    for i, (_, start, maxlen) in enumerate(voices):
        f = f"[{i}:a]aresample=44100,aformat=channel_layouts=stereo"
        if maxlen:
            f += f",atrim=0:{maxlen:.2f},afade=t=out:st={max(0.0, maxlen - 0.3):.2f}:d=0.3"
        ms = int(start * 1000)
        f += f",adelay={ms}|{ms}[a{i}]"
        chains.append(f)
        labels += f"[a{i}]"
    chains.append(f"{labels}amix=inputs={len(voices)}:duration=longest:normalize=0,apad=whole_dur={total:.2f}[out]")
    dst = new_path("m4a")
    args += ["-filter_complex", ";".join(chains), "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(dst)]
    await _run(*args)
    return dst


async def mux_voice(video: Path, track: Path) -> Path:
    """Накладывает голос на видео. Звук самого видео (если есть) делает тише.
    Если голос длиннее видео — последний кадр продлевается."""
    vdur, adur = await duration(video), await duration(track)
    extra = max(0.0, adur - vdur + 0.3)
    has_a = await has_audio(video)
    vf = f"[0:v]tpad=stop_mode=clone:stop_duration={extra:.2f}[v]" if extra > 0.05 else "[0:v]null[v]"
    af = ("[0:a]volume=0.25[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=longest:normalize=0[a]"
          if has_a else "[1:a]anull[a]")
    dst = new_path("mp4")
    await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(track),
               "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-t", f"{max(vdur, adur) + 0.3:.2f}",
               "-movflags", "+faststart", str(dst))
    return dst


async def pad_video(video: Path, seconds: float) -> Path:
    """Продлевает видео последним кадром до нужной длины (для синхронизации губ)."""
    vdur = await duration(video)
    if seconds <= vdur + 0.05:
        return video
    dst = new_path("mp4")
    await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
               f"tpad=stop_mode=clone:stop_duration={seconds - vdur:.2f}", "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(dst))
    return dst


def cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


async def scene_audio(src: Path, delay: float, total: float) -> Path:
    """Голос сцены ровно на total секунд: пауза delay в начале, тишина в конце."""
    dst = new_path("m4a")
    ms = int(delay * 1000)
    await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af",
               f"aresample=44100,aformat=channel_layouts=stereo,adelay={ms}|{ms},apad=whole_dur={total:.2f},"
               f"atrim=0:{total:.2f},afade=t=out:st={max(0.0, total - 0.3):.2f}:d=0.3",
               "-c:a", "aac", "-b:a", "192k", str(dst))
    return dst


async def dims(video: Path) -> tuple[int, int]:
    """Ширина и высота видео (для Telegram, иначе он может показать ролик растянутым)."""
    out = await _run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                     "-of", "csv=p=0", str(video))
    w, h = (int(x) for x in out.strip().split(",")[:2])
    return w, h


def _font() -> str | None:
    """Жирный шрифт с кириллицей (для текста-хука на видео)."""
    import shutil as _sh
    import subprocess
    for name in ("DejaVu Sans:bold", "Liberation Sans:bold", "Noto Sans:bold", "sans:bold"):
        if not _sh.which("fc-match"):
            break
        try:
            f = subprocess.run(["fc-match", "-f", "%{file}", name], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            continue
        if f and Path(f).exists():
            return f
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if Path(f).exists():
            return f
    return None


def _wrap(text: str, width: int = 18) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines[:3])


async def overlay_hook(video: Path, text: str, until: float = 2.8) -> Path:
    """Крупный текст-хук вверху кадра в первые секунды (смотрят без звука). Нет шрифта — видео как есть."""
    text = text.strip()
    font = _font()
    if not text or not font:
        return video
    tf = new_path("txt")
    tf.write_text(_wrap(text), encoding="utf-8")
    alpha = f"if(lt(t\\,{until - 0.3:.2f})\\,1\\,max(0\\,({until:.2f}-t)/0.3))"
    vf = (f"drawtext=fontfile='{font}':textfile='{tf}':fontsize=74:fontcolor=white:line_spacing=12:"
          f"box=1:boxcolor=black@0.55:boxborderw=26:x=(w-text_w)/2:y=h*0.11:"
          f"enable='lt(t\\,{until:.2f})':alpha='{alpha}'")
    dst = new_path("mp4")
    try:
        await _run("ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", vf, "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy",
                   "-movflags", "+faststart", str(dst))
    except RuntimeError:
        return video
    finally:
        cleanup(tf)
    return dst
