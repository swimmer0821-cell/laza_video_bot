"""Claude превращает идею на русском в английские промты для моделей видео и картинок."""
import base64
import json
import logging
import re
from pathlib import Path

from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

BRAND = """Brand context: LAZA — freeze-dried (sublimated) ice cream made in Uzbekistan. The product is
airy, crunchy, porous cubes/pieces of ice cream that do not melt; flavors include vanilla/plombir,
pistachio, chocolate, strawberry, cookies, fruit mix. Packaging: containers and bags with LAZA labels.
Target: vertical 9:16 videos for Instagram Reels / YouTube Shorts, appetizing premium food commercial."""

CHARACTER = """The LAZA mascot: a cute 3D cartoon character whose body is a single hexagonal cube of
freeze-dried vanilla ice cream (pale cream-beige, porous texture with small holes), big glossy brown eyes,
brown eyebrows, rosy cheeks, wide smile, golden-mustard baseball cap with the word "LAZA" on the front,
white cartoon gloves, thin beige arms and legs, white sneakers with "LAZA" on the side. Pixar-like soft
3D render, warm lighting. The character's design must stay EXACTLY as in the reference images."""

VIDEO_SYS = f"""You write prompts for image-to-video AI models (Kling, Seedance, Wan).
{BRAND}
CRITICAL physics: freeze-dried ice cream is completely DRY, matte, airy, light and crunchy like meringue.
It NEVER melts, never drips, never looks wet, glossy, sticky or creamy, never bends, squishes or changes shape.
When touched or bitten it cracks with dry crumbs. Pieces are rigid solid objects.
Rules:
- The first frame is the given image; describe ONLY motion, camera and atmosphere, keep the subject as is.
- Prefer elegant commercial motion: slow camera push-in/orbit/parallax, light sweep, a piece being picked up
  or snapped in half with dry crumbs, pieces gently falling and bouncing. Keep motion modest — avoid morphing.
- TIMING is critical. Write the prompt as a short timeline with seconds, e.g. "0-1s: ... 1-4s: ... 4-5s: ...".
  The main action from the user idea must START within the first second and FINISH at least 1 second before
  the end of the part. No long establishing shots or slow intros. For a 5-second part use at most ONE main
  action; if the idea has more actions than fit, keep the most important one and say so in summary_ru.
- When hands are needed, they enter the frame at 0s already close to the product.
- Camera: if there is an action, keep the camera almost static (very slight push-in at most).
- Always end each prompt with: "Ice cream pieces stay dry, matte and rigid, no melting, no liquid, no morphing."
- Never add text, captions, logos or new labels; keep existing packaging/labels unchanged and readable.
- One clear action per part; smooth camera; realistic physics for food (crumbs, crunch, gentle steam of cold).
- If the user idea is empty, invent an appetizing commercial idea that fits the image.
- If there are several parts, each next part starts exactly from the last frame of the previous one,
  so make the action continue naturally (no cuts back to the start).
- Prompts in English, 50-110 words each. If the idea clearly needs more time than given, add to summary_ru
  a short tip in Russian like «Совет: для этой идеи лучше 10 с». "summary_ru" — 1-2 short sentences in Russian for the user.
Return ONLY JSON: {{"summary_ru": "...", "parts": [{{"duration": N, "prompt": "..."}}]}}"""

IMAGE_SYS = f"""You write prompts for an image editing model that receives reference images.
{BRAND}
{CHARACTER}
Rules:
- Reference images show the mascot (and maybe a product photo). Keep the mascot identical to the references.
- Describe one scene for a vertical 9:16 frame: setting, pose, emotion, lighting, camera angle.
- If a product photo is given, the mascot interacts with that exact product; keep its packaging unchanged.
- No text in the image except "LAZA" on the cap/sneakers and existing product labels.
- Leave some empty space at the top for captions that will be added later.
- Prompt in English, 50-110 words. "summary_ru" — 1 short sentence in Russian.
Return ONLY JSON: {{"summary_ru": "...", "prompt": "..."}}"""


def _img_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Claude вернул ответ без JSON")
    return json.loads(m.group(0))


async def _ask(system: str, images: list[Path], text: str) -> dict:
    content = [_img_block(p) for p in images] + [{"type": "text", "text": text}]
    msg = await client.messages.create(model=CLAUDE_MODEL, max_tokens=1500, system=system,
                                       messages=[{"role": "user", "content": content}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    logging.info("Claude ответ: %s", out[:500])
    return _parse(out)


async def video_plan(image: Path, idea: str, parts: list[int], kind: str) -> dict:
    """План ролика: части с длительностью и английским промтом."""
    who = "the LAZA mascot character" if kind == "character" else "a LAZA product photo"
    text = (f"The image is {who}. User idea (Russian): «{idea or 'нет, придумай сам'}».\n"
            f"Make exactly {len(parts)} part(s) with durations {parts} seconds.")
    plan = await _ask(VIDEO_SYS, [image], text)
    got = plan.get("parts") or []
    if len(got) != len(parts):
        raise ValueError("Claude вернул неверное число частей")
    for p, d in zip(got, parts):
        p["duration"] = d
        if not str(p.get("prompt", "")).strip():
            raise ValueError("Пустой промт")
    return plan


async def image_prompt(refs: list[Path], product: Path | None, idea: str) -> dict:
    """Промт для картинки с персонажем."""
    imgs = refs + ([product] if product else [])
    text = (f"First {len(refs)} image(s) are mascot references."
            + (" The last image is the product photo." if product else "")
            + f"\nUser scene idea (Russian): «{idea}».")
    res = await _ask(IMAGE_SYS, imgs, text)
    if not str(res.get("prompt", "")).strip():
        raise ValueError("Пустой промт")
    return res
