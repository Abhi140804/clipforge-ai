"""Titles, hooks, hashtags, descriptions, thumbnail text, summary, niche.

With an LLM key the model writes everything from the clip transcript; without one a deterministic, honest
fallback is used (flagged `generated_by: heuristic`). Either way output is validated: no titles with numbers or
superlatives that are absent from the transcript, length limits enforced, hashtags sanitised.
"""
import re
from collections import Counter

from .clips import STOP, content_toks, toks
from .llm import LLM, LLMError

CATEGORY = {"gaming": "Gaming", "educational": "Education", "comedy": "Comedy", "podcast": "People & Blogs",
            "vlog": "People & Blogs", "other": "Entertainment"}
YT_CATEGORY_ID = {"Gaming": "20", "Education": "27", "Comedy": "23", "People & Blogs": "22", "Entertainment": "24",
                  "Science & Technology": "28", "News & Politics": "25", "Howto & Style": "26", "Music": "10"}

SYSTEM = """You write metadata for a short vertical video clip (YouTube Shorts / Reels / TikTok) from its transcript.
Hard rules:
- Stay faithful to the transcript. Do not invent facts, numbers, names, results or claims that are not stated.
- No misleading clickbait: the title must be something the clip actually delivers. No false urgency, no "you won't believe".
- Natural, specific, human-sounding. Max 70 characters per title. No emoji spam (at most one), no ALL CAPS words.
Return JSON:
{"titles":[5 distinct titles],"hooks":[4 alternative opening lines for on-screen text, max 12 words each],
 "hashtags":[8-10 lowercase single-word hashtags without '#'],"description":"2-3 sentence SEO-friendly description",
 "thumbnail_text":[3 options, max 4 words each],"summary":"one sentence","category":"one of: Gaming, Education, Comedy, People & Blogs, Entertainment, Science & Technology, News & Politics, Howto & Style, Music","niche":"2-4 word niche"}"""


def _clean_tag(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", t.lower().lstrip("#"))[:30]


def _supported(title: str, transcript: str) -> bool:
    """A title may not introduce digits or absolutist claims that the transcript never makes."""
    tl = transcript.lower()
    for num in re.findall(r"\d+", title):
        if num not in tl and not re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b", tl):
            return False
    for w in ("never", "always", "everyone", "nobody", "guaranteed", "secret", "shocking"):
        if re.search(rf"\b{w}\b", title.lower()) and w not in tl:
            return False
    return True


def _keywords(text: str, n: int = 6) -> list[str]:
    c = Counter(t for t in content_toks(text) if not t.isdigit())
    return [w for w, _ in c.most_common(n)]


def heuristic(text: str, duration: float, content_type: str, title_hint: str = "") -> dict:
    kws = _keywords(text) or ["video"]
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 3]
    first = sentences[0] if sentences else text[:70]
    first = re.sub(r"\s+", " ", first)
    k1, k2, k3 = (kws + kws + kws)[:3]
    titles = []
    if len(first) <= 70:
        titles.append(first.rstrip(".") if not first.endswith("?") else first)
    else:
        titles.append(first[:67].rsplit(" ", 1)[0] + "…")
    titles += [f"{k1.title()} and {k2} in {int(round(duration))} seconds", f"Quick take on {k1}",
               f"{k1.title()}: the key point, explained briefly", f"What was said about {k1} and {k3}"]
    seen, out = set(), []
    for t in titles:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:100])
    cat = CATEGORY.get(content_type, "Entertainment")
    tags = [_clean_tag(k) for k in kws] + [content_type if content_type != "other" else "shorts", "shorts"]
    tags = list(dict.fromkeys(t for t in tags if t))[:10]
    summary = (sentences[0] if sentences else text[:140])[:200]
    return {
        "titles": out[:5], "hooks": [first[:80], f"Here's the point about {k1}", f"Listen to this part about {k1}", f"{k1.title()} — in {int(round(duration))} seconds"],
        "hashtags": tags, "description": f"{summary} " + ("Clip from: " + title_hint + "." if title_hint else ""),
        "thumbnail_text": [" ".join(k1.upper().split()[:3]), f"{k1} {k2}".upper(), f"ABOUT {k1}".upper()],
        "summary": summary, "category": cat, "niche": f"{k1} {content_type if content_type != 'other' else ''}".strip(),
    }


def generate(llm: LLM | None, text: str, duration: float, content_type: str = "other", kind: str = "", title_hint: str = "") -> dict:
    meta, by = None, "heuristic"
    if llm and llm.available and text.strip():
        try:
            res = llm.complete_json(SYSTEM, f"Source video title: {title_hint or 'unknown'}\nClip length: {duration:.0f}s\nMoment type: {kind or 'n/a'}\n\nTRANSCRIPT:\n{text[:6000]}", 1500)
            if isinstance(res, dict) and res.get("titles"):
                meta, by = res, "llm"
        except LLMError:
            meta = None
    fallback = heuristic(text, duration, content_type, title_hint)
    if meta is None:
        meta = fallback
    # validation / sanitising
    titles = [re.sub(r"\s+", " ", str(t)).strip().strip('"')[:100] for t in meta.get("titles", [])]
    titles = [t for t in titles if t and _supported(t, text) and "<" not in t and ">" not in t]
    for t in fallback["titles"]:
        if len(titles) >= 5:
            break
        if t.lower() not in {x.lower() for x in titles}:
            titles.append(t)
    tags = list(dict.fromkeys(_clean_tag(str(t)) for t in meta.get("hashtags", []) if _clean_tag(str(t))))[:12] or fallback["hashtags"]
    cat = meta.get("category") if meta.get("category") in YT_CATEGORY_ID else fallback["category"]
    return {
        "titles": titles[:5], "hooks": [str(h)[:100] for h in (meta.get("hooks") or fallback["hooks"])][:6],
        "hashtags": tags, "description": str(meta.get("description") or fallback["description"])[:1500],
        "thumbnail_text": [str(t)[:40] for t in (meta.get("thumbnail_text") or fallback["thumbnail_text"])][:4],
        "summary": str(meta.get("summary") or fallback["summary"])[:300], "category": cat,
        "niche": str(meta.get("niche") or fallback["niche"])[:60], "generated_by": by,
        "selected": {"title": 0, "hook": 0, "thumbnail_text": 0, "custom_title": "", "custom_description": "", "custom_hashtags": None},
    }
