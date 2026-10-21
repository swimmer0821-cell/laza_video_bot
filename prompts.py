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
- HOOK: the first second must already be the most striking moment (motion, crunch, close-up) — viewers
  decide in 2 seconds whether to keep watching. No slow start.
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
- Framing: medium shot, the mascot is big in the frame — about 55-65% of the frame height, face clearly
  visible and well lit, mouth visible (for lip-sync later). Not a tiny figure in a big room.
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
    msg = await client.messages.create(model=CLAUDE_MODEL, max_tokens=4000, system=system,
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


VOICE = """Laza's voice and personality: a cheerful, friendly, slightly mischievous young character, proud of
"real ice cream without middlemen", speaks simply, warmly, with energy. Speaks to the viewer as a friend.
Russian: natural modern spoken Russian, «ты»-форма. Write the name in Cyrillic: «Лаза» (stress on the last
syllable is added automatically). Uzbek: natural spoken Uzbek in LATIN script
(o', g', sh, ch), «sen» form, no Russian words except brand names.
Speech rate ~13 characters per second. You may add at most 2 ElevenLabs v3 audio tags in English square
brackets for emotion, e.g. [excited], [laughs], [whispers], [cheerfully] — they are not counted in length.
No emoji, no hashtags, no prices or phone numbers unless the user asked."""

VOICE_SYS = f"""You write a short voice-over line spoken by the LAZA mascot.
{BRAND}
{VOICE}
Return ONLY JSON: {{"text": "line in the target language", "ru": "Russian translation (same as text if Russian)"}}"""

SCENES_SYS = f"""You are a director of short vertical ads (Instagram Reels / YouTube Shorts) starring the LAZA mascot.
{BRAND}
{CHARACTER}
{VOICE}
HOW SHORT VIDEOS WIN (follow strictly):
- The first 2 seconds decide everything. Scene 1 opens IN the action — no greeting, no "hi, I'm Laza",
  no logo, no slow intro. The hook fires on 3 channels at once: a striking first frame, the first spoken
  words, and a short on-screen text. Most people watch muted, so the on-screen text alone must hook.
- A hook opens a gap the viewer needs to close (curiosity, surprise, stakes, a specific number, a myth,
  identity "if you love ice cream…", a challenge). It must be TRUE — the video must pay it off.
  Allowed facts only: real ice cream from natural cream, freeze-dried, does not melt, crunchy, light,
  made in Uzbekistan by the producer (no middlemen). Never invent numbers, awards, prices or claims.
- One idea per video. Every scene changes the picture (new action or angle) — no static talking.
- Open a loop early, pay it off at the end. The last scene ends on a soft CTA and, if possible, a beat
  that makes the viewer want to rewatch or send it to a friend ("send this to someone who…").
- Think: who would DM this video to whom, and why? Build that reason in.
Make a storyboard of N scenes. For EACH scene write:
- "title_ru": 2-5 words in Russian.
- "image_prompt": English prompt (60-110 words) for an image editing model that receives mascot reference
  images: one vertical 9:16 frame, setting, pose, emotion, lighting. Keep the mascot identical to references
  (same body shape and proportions, not wider, not stretched). Medium shot: mascot about 55-65% of frame
  height, face and mouth clearly visible. No text except "LAZA" on cap/sneakers. Consistent warm commercial
  look across scenes. Real-looking environments. Scene 1 frame must be visually striking (bold action,
  close-up, unexpected moment) and leave the top 25% of the frame calm for on-screen text.
- "motion_prompt": English prompt (50-100 words) animating that frame for the given seconds, as a timeline
  "0-1s: ... 1-4s: ...". Main action starts in the first second. When the scene has a voice line the mascot
  talks to the camera with natural gestures. Camera calm. Ice cream cubes stay dry, matte and rigid,
  no melting. End with "Keep the character design and body proportions unchanged."
- "voice": the line Laza says in this scene in the target language (or "" if no voice). It must fit the
  scene seconds at ~13 chars/second minus 0.5 s. Scene 1 voice STARTS with the spoken hook.
- "voice_ru": Russian translation of the voice line ("" if no voice).
Also write:
- "hooks": exactly 3 alternative hooks for scene 1, each on a DIFFERENT mechanism, best first:
  {{"mechanism_ru": "2-3 words in Russian (e.g. «миф», «интрига», «вызов»)",
    "overlay": "on-screen text, max 6 words, in the voice language (Russian if no voice), no emoji",
    "voice": "full scene-1 voice line starting with this hook (same length rule; \"\" if no voice)",
    "voice_ru": "its Russian translation"}}. hooks[0] must equal scene 1 voice.
- "caption": Instagram caption in the voice language (Russian if no voice): first sentence ≤ 120 chars with
  the main keyword (e.g. сублимированное мороженое / sublimatsiya qilingan muzqaymoq) and a reason to watch,
  then 1-2 short lines, a soft CTA to order. No fake claims.
- "hashtags": 3-5 relevant hashtags (strings with #).
- "sends_ru": one short sentence in Russian — who would send this to whom and why.
"summary_ru": 1-2 sentences in Russian about the whole video.
Return ONLY JSON: {{"summary_ru": "...", "hooks": [...], "scenes": [{{"title_ru": "...", "image_prompt": "...",
"motion_prompt": "...", "voice": "...", "voice_ru": "..."}}], "caption": "...", "hashtags": [...],
"sends_ru": "..."}}"""


async def voice_line(context: str, seconds: float, lang: str, image: Path | None = None) -> dict:
    """Реплика Лазы для готового ролика."""
    limit = int(seconds * 13)
    text = (f"Target language: {'Uzbek (Latin)' if lang == 'uz' else 'Russian'}.\n"
            f"The video lasts {seconds:.0f} s, so the line must be at most {limit} characters (without tags).\n"
            f"What happens in the video / user idea: «{context or 'Лаза в ролике про мороженое Laza'}».")
    res = await _ask(VOICE_SYS, [image] if image else [], text)
    if not str(res.get("text", "")).strip():
        raise ValueError("Пустая реплика")
    return res


async def storyboard(refs: list[Path], idea: str, n: int, sec: int, lang: str | None) -> dict:
    """Раскадровка ролика из n сцен по sec секунд."""
    target = {"ru": "Russian", "uz": "Uzbek (Latin script)"}.get(lang or "", "NO VOICE — leave voice fields empty")
    text = (f"Images are mascot references. Scenes: exactly {n}, each {sec} seconds. "
            f"Voice language: {target}.\nUser idea (Russian): «{idea}».")
    res = await _ask(SCENES_SYS, refs[:2], text)
    scenes = res.get("scenes") or []
    if len(scenes) != n or not all(s.get("image_prompt") and s.get("motion_prompt") for s in scenes):
        raise ValueError("Claude вернул неполную раскадровку")
    if not lang:
        for s in scenes:
            s["voice"] = s["voice_ru"] = ""
    hooks = [h for h in (res.get("hooks") or []) if isinstance(h, dict) and h.get("overlay")][:3]
    for h in hooks:
        if not lang:
            h["voice"] = h["voice_ru"] = ""
    if not hooks:
        hooks = [{"mechanism_ru": "", "overlay": "", "voice": scenes[0].get("voice", ""),
                  "voice_ru": scenes[0].get("voice_ru", "")}]
    res["hooks"] = hooks
    return res
