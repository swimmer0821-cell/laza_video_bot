"""Claude превращает идею на русском в английские промты для моделей видео и картинок."""
import base64
import json
import logging
import re
from pathlib import Path

from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

BRAND = """Brand context: LAZA (Instagram @laza.uzbekistan) — premium freeze-dried snacks made in Uzbekistan:
ice cream cubes you can crunch like a snack. Light, airy, crunchy, dry cubes that keep the ice-cream taste and do not
melt. Flavors: fruit mix, forest berries, crème brûlée, mix of 4 flavors, pistachio, plombir, chocolate,
strawberry, banana, chocolate-bar and candy-coated-chocolate flavors (never show other brands' logos).
Also freeze-dried suluguni cheese and kurut. Packaging: containers and bags with LAZA labels (premium design).
BRAND CORE (follow strictly): emotions -> pleasure -> virality. Content runs on surprise, delight, reactions,
humor, crunch sound, unexpected flavors and everyday life moments in Uzbekistan (university, car, trips,
moms with kids, friends, family table after plov, summer heat). NEVER lecture: no technology or drying
process explanations, no factory or lab, no vitamins, no health or diet claims, no "healthy food for fitness
people". A fantasy cartoon "magic" world of Laza is allowed instead of a factory. The product is a new kind of
treat — ice cream as a crunchy snack. Target: vertical 9:16 Reels / Shorts, native-looking, fun, not a TV ad."""

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
  decide in 1.5 seconds whether to keep watching. No slow start.
- PEOPLE: if a person is in the frame, keep EXACTLY the same face, hair, age and clothes in every frame (no
  identity drift). People do NOT talk and do not move lips as if speaking — a voice-over is added later. They
  smile, chew, react, look at the camera, show the product. Never write "says", "tells", "talks".
- PACKAGING: the product package is a rigid printed object — its shape, colors, logo and text stay exactly the
  same in every frame, never redrawn, never morphing, never new letters.
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


VOICE = """Laza's voice and personality: a cheerful, cheeky, a bit mischievous young character with self-irony;
the ice cream that is never afraid of the heat. Loves surprising people, jokes about Tashkent life (heat,
traffic, family feasts), never boasts with facts. Speaks simply, warmly, with energy, to the viewer as a friend.
Russian: natural modern spoken Russian, «ты»-форма. Write the name in Cyrillic: «Лаза» (stress on the last
syllable is added automatically). Uzbek: natural spoken Uzbek in LATIN script
(o', g', sh, ch), «sen» form, no Russian words except brand names.
Speech rate ~10 characters per second (relaxed natural voice). You may add at most 2 ElevenLabs v3 audio tags in English square
brackets for emotion, e.g. [excited], [laughs], [whispers], [cheerfully] — they are not counted in length.
No emoji, no hashtags, no prices or phone numbers unless the user asked."""

VOICE_SYS = f"""You write a short OFF-SCREEN voice-over line spoken by the LAZA mascot.
If a person is visible in the video, Laza comments from off-screen as a friend (never pretends to be that person,
never "I am the ice cream" while someone else is on screen). People in the video do not speak.
{BRAND}
{VOICE}
Return ONLY JSON: {{"text": "line in the target language", "ru": "Russian translation (same as text if Russian)"}}"""

SCENES_SYS = f"""You are a director of short vertical ads (Instagram Reels / YouTube Shorts) starring the LAZA mascot.
{BRAND}
{CHARACTER}
{VOICE}
HOW SHORT VIDEOS WIN (follow strictly):
- The first 1.5 seconds decide everything. The spoken hook must be said within the first 1.5 s (max ~20 characters before the first pause), the on-screen text says the same idea. Scene 1 opens IN the action — no greeting, no "hi, I'm Laza",
  no logo, no slow intro. The hook fires on 3 channels at once: a striking first frame, the first spoken
  words, and a short on-screen text. Most people watch muted, so the on-screen text alone must hook.
- A hook opens a gap the viewer needs to close: curiosity, surprise, humor, a relatable situation
  ("when it's +42 and…"), a challenge, a flavor guess, a reaction. Emotion, NOT facts or technology.
  It must be TRUE — the video must pay it off. Never invent numbers, awards, prices, reviews or health claims.
- One idea per video. Every scene changes the picture (new action or angle) — no static talking.
- Open a loop early, pay it off at the end. End on a funny or satisfying beat (punchline, reveal, crunch) and a
  very soft brand mention ("Laza"), not a sales pitch. Do NOT ask viewers to send/tag/share — Instagram demotes
  engagement bait; the reason to send must be inside the video (relatable, funny, surprising).
- Think: who would DM this video to whom, and why? Build that reason in.
- If a product photo is given, the real LAZA package appears in the frames as is (label unchanged).
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
- "voice": the line Laza says in this scene in the target language (or "" if no voice). SHORT: at most
  ~10 characters per scene second (5 s scene = max 45-50 characters), one or two short phrases, so a natural
  relaxed voice fits. Scene 1 voice STARTS with the spoken hook.
- TALKING SCENES (voice not empty): Laza is the ONLY character whose face is visible and the only one who talks.
  Other people may appear only from behind, out of focus, as hands, or far in the background with faces not
  visible — never a second visible face (lip-sync would make them talk too). Scenes with people's faces and
  reactions must have voice "".
- "voice_ru": Russian translation of the voice line ("" if no voice).
Also write:
- "hooks": exactly 3 alternative hooks for scene 1, each on a DIFFERENT mechanism, best first:
  {{"mechanism_ru": "2-3 words in Russian (e.g. «миф», «интрига», «вызов»)",
    "overlay": "on-screen text, max 6 words, in the voice language (Russian if no voice), no emoji",
    "voice": "full scene-1 voice line starting with this hook (same length rule; \"\" if no voice)",
    "voice_ru": "its Russian translation"}}. hooks[0] must equal scene 1 voice.
- "caption": Instagram caption in the voice language (Russian if no voice): first sentence ≤ 120 chars with
  the main keyword (e.g. сублимированное мороженое / sublimatsiya qilingan muzqaymoq) and a reason to watch,
  then 1-2 short lines in Laza's voice, end with where to find it: «Laza — ссылка в профиле» / «Laza — profilda».
  No fake claims, no health claims.
- "hashtags": 3-5 relevant hashtags (strings with #).
- "sends_ru": one short sentence in Russian — who would send this to whom and why.
"summary_ru": 1-2 sentences in Russian about the whole video.
Return ONLY JSON: {{"summary_ru": "...", "hooks": [...], "scenes": [{{"title_ru": "...", "image_prompt": "...",
"motion_prompt": "...", "voice": "...", "voice_ru": "..."}}], "caption": "...", "hashtags": [...],
"sends_ru": "..."}}"""


async def voice_line(context: str, seconds: float, lang: str, image: Path | None = None) -> dict:
    """Реплика Лазы для готового ролика."""
    limit = int(seconds * 10)
    text = (f"Target language: {'Uzbek (Latin)' if lang == 'uz' else 'Russian'}.\n"
            f"The video lasts {seconds:.0f} s, so the line must be at most {limit} characters (without tags).\n"
            f"What happens in the video / user idea: «{context or 'Лаза в ролике про мороженое Laza'}».")
    res = await _ask(VOICE_SYS, [image] if image else [], text)
    if not str(res.get("text", "")).strip():
        raise ValueError("Пустая реплика")
    return res


async def storyboard(refs: list[Path], idea: str, n: int, sec: int, lang: str | None,
                     product: Path | None = None) -> dict:
    """Раскадровка ролика из n сцен по sec секунд."""
    target = {"ru": "Russian", "uz": "Uzbek (Latin script)"}.get(lang or "", "NO VOICE — leave voice fields empty")
    text = (f"Images are mascot references" + (" and the LAST image is the real LAZA product package that must "
            "appear in the scenes" if product else "") + f". Scenes: exactly {n}, each {sec} seconds. "
            f"Voice language: {target}.\nUser idea (Russian): «{idea}».")
    res = await _ask(SCENES_SYS, refs[:2] + ([product] if product else []), text)
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


TRANSLATE_SYS = f"""You adapt a short LAZA video script into another language for the same video.
{VOICE}
Rules: keep the meaning, humor and timing; each line must fit the SAME seconds as the original
(~10 characters per second, leave 0.5 s spare) — shorten if needed. Natural spoken language, not literal.
Uzbek in LATIN script. Keep "Laza"/«Лаза» as the name. On-screen hook text: max 6 words.
Return ONLY JSON: {{"voices": ["line for scene 1", ...], "voices_ru": ["Russian meaning of each line", ...],
"overlay": "on-screen hook text", "caption": "Instagram caption in the target language", "hashtags": ["#...", ...]}}"""


async def translate_board(board: dict, lang: str, sec: int) -> dict:
    """Перевод реплик, текста-хука и подписи ролика на другой язык (для второй языковой версии)."""
    hooks = board.get("hooks") or [{}]
    src = {"scene_seconds": sec,
           "voices": [s.get("voice", "") for s in board["scenes"]],
           "overlay": hooks[board.get("hook_i", 0)].get("overlay", ""),
           "caption": board.get("caption", ""), "hashtags": board.get("hashtags", [])}
    target = "Uzbek (Latin script)" if lang == "uz" else "Russian"
    res = await _ask(TRANSLATE_SYS, [], f"Target language: {target}.\nScript JSON:\n{json.dumps(src, ensure_ascii=False)}")
    voices = res.get("voices") or []
    if len(voices) != len(board["scenes"]):
        raise ValueError("Перевод вернул не то число реплик")
    return res
