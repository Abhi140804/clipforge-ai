"""Moment detection + clip selection.

Pipeline:
  1. candidates   - sentence-aligned windows (or silence/scene-aligned windows when there is no transcript)
  2. heuristic scoring across seven dimensions using transcript + audio + visual signals
  3. optional LLM pass (chunked) that discovers/scores moments with real understanding of the content
  4. boundary refinement, de-duplication and ranking
"""
import bisect
import math
import re

import numpy as np

from . import analysis as A
from . import transcribe as T
from .llm import LLM, LLMError

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is
it its itself just me more most my no nor not now of off on once only or other our out over own same she should so some such than
that the their them then there these they this those through to too under until up very was we were what when where which while who
whom why will with would you your yours yeah um uh like gonna kinda really actually basically literally""".split())

HOOK_WORDS = {"secret", "never", "always", "nobody", "everyone", "biggest", "worst", "best", "crazy", "insane", "wait", "truth",
              "mistake", "why", "imagine", "stop", "problem", "learned", "changed", "weird", "unbelievable", "shocking",
              "surprising", "love", "hate", "wrong", "reason", "actually", "listen", "important", "lesson", "scary", "impossible"}
EMOTION_WORDS = {"haha", "lol", "laugh", "laughing", "laughter", "wow", "omg", "god", "amazing", "crazy", "insane", "hate", "love",
                 "scared", "afraid", "angry", "sad", "cry", "crying", "excited", "terrible", "awesome", "incredible", "unbelievable",
                 "ridiculous", "hilarious", "funny", "no way", "what"}
DANGLING_START = {"and", "but", "so", "because", "which", "that", "then", "also", "or", "he", "she", "they", "it", "this",
                  "those", "these", "him", "her", "them", "its", "his", "their", "however", "anyway"}
GAMING_WORDS = {"gg", "clutch", "kill", "killed", "boss", "level", "respawn", "loot", "noob", "ranked", "lobby", "squad", "spawn",
                "headshot", "stream", "chat", "frag", "queue", "dps", "build", "meta"}
EDU_WORDS = {"because", "means", "example", "learn", "understand", "explain", "step", "first", "second", "how", "works", "concept",
             "theory", "definition", "tutorial", "lesson", "research", "study", "data"}

WEIGHTS = {"hook": 0.22, "relevance": 0.12, "engagement": 0.18, "emotion": 0.14, "completeness": 0.14, "quality": 0.08, "standalone": 0.12}


def toks(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def content_toks(text: str) -> list[str]:
    return [t for t in toks(text) if t not in STOP and len(t) > 2]


def rank01(vals: list[float]) -> list[float]:
    if not vals:
        return []
    order = np.argsort(np.argsort(vals))
    return [float(o) / max(1, len(vals) - 1) for o in order]


# ---------------------------------------------------------------- candidates
def _sent_candidates(sents, min_d, max_d):
    out = []
    n = len(sents)
    for i in range(n):
        ends = []
        for j in range(i, n):
            d = sents[j]["end"] - sents[i]["start"]
            if d > max_d:
                break
            gap_next = (sents[j + 1]["start"] - sents[j]["end"]) if j + 1 < n else 99
            if d >= min_d and (sents[j]["terminal"] or gap_next > 0.7 or j == n - 1):
                ends.append(j)
        if not ends:
            continue
        picks = {ends[-1], ends[0]}
        for j in picks:
            out.append((sents[i]["start"], sents[j]["end"]))
    return out


def _signal_candidates(an, min_d, max_d):
    """No transcript: align to silence boundaries, fall back to a scene-snapped grid."""
    dur = an["duration"]
    sil = an["silences"]
    starts = [0.0] + [e for _, e in sil]
    ends = [s for s, _ in sil] + [dur]
    out = []
    ends_arr = np.array(ends)
    for s in starts:
        ok = ends_arr[(ends_arr - s >= min_d) & (ends_arr - s <= max_d)]
        if ok.size:
            out.append((s, float(ok.max())))
    if len(out) < 6:
        step = max(min_d, max_d / 2)
        t = 0.0
        cuts = np.array(an["cuts"] or [0])
        while t + min_d <= dur:
            s = float(cuts[np.argmin(np.abs(cuts - t))]) if np.abs(cuts - t).min() < 2 else t
            out.append((s, min(dur, s + max_d)))
            t += step
    return out


def _refine(start, end, words, wstarts, duration, silences):
    """Pad slightly into the surrounding gaps; never cut into a neighbouring word."""
    if words:
        i = bisect.bisect_left(wstarts, start) - 1
        prev_end = words[i]["e"] if i >= 0 else 0.0
        j = bisect.bisect_right(wstarts, end - 0.01)
        next_start = words[j]["s"] if j < len(words) else duration
        start = max(prev_end + 0.04, start - 0.25, 0.0) if start - prev_end > 0.08 else start
        end = min(next_start - 0.04, end + 0.35, duration) if next_start - end > 0.08 else end
    else:
        for s, e in silences:  # trim into silence edges
            if s <= start <= e:
                start = e - 0.1
            if s <= end <= e:
                end = s + 0.1
    return round(max(0.0, start), 2), round(min(duration, end), 2)


# ---------------------------------------------------------------- scoring
def _features(c, tr_words, an, idf, info, max_d):
    s, e = c["start"], c["end"]
    ws = [w for w in tr_words if w["e"] > s + 0.03 and w["s"] < e - 0.03] if tr_words else []
    text = " ".join(w["w"] for w in ws)
    st = A.window_stats(an, s, e)
    head = " ".join(w["w"] for w in ws if w["s"] < s + 3.5)
    head_l = head.lower()
    hook = 0.0
    hook += 0.30 if "?" in head else 0.0
    hook += 0.25 if any(t in HOOK_WORDS for t in toks(head)) else 0.0
    hook += 0.12 if re.search(r"\d", head) else 0.0
    hook += 0.10 if re.match(r"^(so|okay|listen|look|here's|imagine|let me|what if|the thing)\b", head_l) else 0.0
    zhead = float(an["energy_z"][int(s):int(s) + 3].mean()) if an["n_sec"] > int(s) else 0.0
    hook += min(0.35, max(0.0, zhead) * 0.15)
    if not ws:  # no transcript: hook = energy + motion at the start
        hook = min(1.0, 0.3 + max(0.0, zhead) * 0.2 + float(an["motion"][int(s):int(s) + 3].mean()) * 3)
    first = ws[0]["w"].lower().strip(".,!?") if ws else ""
    if first in DANGLING_START:
        hook -= 0.2

    ct = content_toks(text)
    rel = 0.0
    if ct:
        cnt = {}
        for t in ct:
            cnt[t] = cnt.get(t, 0) + 1
        top = sorted(((v * idf.get(t, 1.0)) for t, v in cnt.items()), reverse=True)[:8]
        rel = float(np.mean(top))

    dur = max(0.1, e - s)
    wps = len(ws) / dur
    density = 1.0 - min(1.0, abs(wps - 2.7) / 2.7) if ws else 0.5
    eng = 0.45 * st["energy_mean"] + 0.25 * st["energy_peak"] + 4 * st["motion"] + 0.8 * density
    emo = 0.8 * st["energy_std"] + 0.35 * text.count("!") + 0.4 * sum(1 for t in toks(text) if t in EMOTION_WORDS) \
        + 0.5 * len(re.findall(r"\b(haha+|lol|\[laughter\]|\(laughs?\))", text.lower())) + 0.2 * text.count("?")

    ends_terminal = bool(ws) and bool(T.SENT_END.search(ws[-1]["w"]))
    starts_clean = c.get("starts_clean", True)
    fit = 1.0 if 0.55 * max_d <= dur <= max_d else max(0.2, 1 - abs(dur - 0.55 * max_d) / max_d)
    comp = 0.35 * (1.0 if starts_clean else 0.4) + 0.35 * (1.0 if (ends_terminal or not ws) else 0.3) + 0.3 * fit
    if not ws:
        comp = 0.35 * 0.7 + 0.35 * 0.6 + 0.3 * fit

    face_part = st["face"] if an["face_ratio"] > 0.25 else 0.5
    level = float(np.clip((st["mean_db"] + 45) / 30, 0, 1))
    res = 1.0 if info["height"] >= 720 else 0.6
    qual = 0.45 * face_part + 0.35 * level + 0.2 * res - (0.3 if an["clip_ratio"] > 0.02 else 0)

    standalone = 1.0
    if first in DANGLING_START:
        standalone -= 0.45
    if ws and len(toks(ws[0]["w"] + " " + (ws[1]["w"] if len(ws) > 1 else ""))) and len(ws) < 12:
        standalone -= 0.3
    if ws and not ends_terminal:
        standalone -= 0.15
    return {"hook": hook, "relevance": rel, "engagement": eng, "emotion": emo, "completeness": comp,
            "quality": qual, "standalone": standalone, "_text": text, "_stats": st, "_wps": wps, "_ends_terminal": ends_terminal,
            "_first": first, "_words": ws}


def _reason(f, kind):
    bits = []
    if f["hook"] > 0.45:
        bits.append("opens with a strong hook")
    if f["engagement"] > 0.7:
        bits.append("high vocal energy and visual activity")
    if f["emotion"] > 0.7:
        bits.append("emotionally charged delivery")
    if f["relevance"] > 0.7:
        bits.append("dense with distinctive, on-topic content")
    if f["completeness"] > 0.8:
        bits.append("a complete thought that ends on a natural stop")
    if f["standalone"] > 0.85:
        bits.append("makes sense without the surrounding video")
    if not bits:
        bits.append("reasonably self-contained and engaging segment")
    return (kind.capitalize() + " moment: " if kind else "Moment: ") + "; ".join(bits[:4]) + "."


def guess_content_type(tr, an) -> str:
    text = " ".join(w["w"] for w in T.all_words(tr)) if tr else ""
    t = toks(text)
    n = max(1, len(t))
    gaming = sum(1 for x in t if x in GAMING_WORDS) / n
    edu = sum(1 for x in t if x in EDU_WORDS) / n
    laugh = len(re.findall(r"\b(haha+|lol|\[laughter\]|\(laughs?\))", text.lower())) / max(1, an["duration"] / 60)
    if gaming > 0.012 or (an["face_ratio"] < 0.1 and float(an["motion"].mean()) > 0.12):
        return "gaming"
    if laugh > 1.0:
        return "comedy"
    if edu > 0.03:
        return "educational"
    if an["face_ratio"] > 0.4 and float(an["motion"].mean()) < 0.06:
        return "podcast"
    return "other"


# ---------------------------------------------------------------- main entry
def find_candidates(tr, an, info, prefs: dict, log=lambda m: None) -> list[dict]:
    max_d = float(prefs["duration"])
    min_d = max(8.0, round(max_d * 0.5))
    words = T.all_words(tr) if tr else []
    wstarts = [w["s"] for w in words]
    if tr and words:
        sents = T.sentences(tr)
        raw = _sent_candidates(sents, min_d, max_d)
        log(f"Generated {len(raw)} sentence-aligned candidate windows")
    else:
        raw = _signal_candidates(an, min_d, max_d)
        log(f"No transcript available; generated {len(raw)} candidates from silence and scene boundaries")
    # idf over the whole transcript
    cnt = {}
    for w in words:
        for t in content_toks(w["w"]):
            cnt[t] = cnt.get(t, 0) + 1
    total = max(1, sum(cnt.values()))
    idf = {t: math.log((total + 1) / (v + 1)) for t, v in cnt.items()}
    cands = []
    seen = set()
    for s, e in raw:
        s2, e2 = _refine(s, e, words, wstarts, an["duration"], an["silences"])
        key = (round(s2), round(e2))
        if key in seen or e2 - s2 < min_d * 0.8:
            continue
        seen.add(key)
        starts_clean = True
        if not words:
            starts_clean = any(abs(s - se) < 0.3 for _, se in an["silences"]) or s < 0.5
        f = _features({"start": s2, "end": e2, "starts_clean": starts_clean}, words, an, idf, info, max_d)
        cands.append({"start": s2, "end": e2, "f": f, "source": "heuristic"})
    # percentile-normalise the open-ended dimensions across the video
    for dim in ("relevance", "engagement", "emotion"):
        for c, r in zip(cands, rank01([c["f"][dim] for c in cands])):
            c["f"][dim] = r
    for c in cands:
        f = c["f"]
        f["hook"] = float(np.clip(f["hook"], 0, 1))
        f["quality"] = float(np.clip(f["quality"], 0, 1))
        f["standalone"] = float(np.clip(f["standalone"], 0, 1))
        c["dims"] = {k: round(float(f[k]), 3) for k in WEIGHTS}
        c["heuristic"] = round(100 * sum(WEIGHTS[k] * f[k] for k in WEIGHTS), 1)
        c["score"] = c["heuristic"]
    return cands


# ---------------------------------------------------------------- LLM discovery
LLM_SYSTEM = """You are the moment-detection brain of a short-form video editor. You are given a timestamped transcript chunk \
of a longer video. Find the segments that would work best as standalone 15-60 second vertical clips (YouTube Shorts / Reels / TikTok).

Rules:
- start and end MUST be timestamps that appear at the start/end of transcript lines you were given.
- A clip must make sense on its own: begin where a thought begins, end where it resolves. Do not start mid-sentence.
- Prefer a strong opening line. Never choose a segment that would mislead viewers about what the speaker meant.
- Respect the requested maximum duration. Return at most the requested number of moments; return fewer (or none) if nothing is good.
- Scores are integers 0-10: hook, relevance, engagement, emotion, completeness, standalone.
- type: one of funny, insightful, educational, emotional, controversial, reaction, highlight, story, other.
- misleading_risk: low | medium | high (high = clip removes context that changes the meaning).
- video_type: podcast | gaming | educational | comedy | vlog | other.
Return JSON: {"video_type": "...", "moments": [{"start": 0.0, "end": 0.0, "hook": 0, "relevance": 0, "engagement": 0, "emotion": 0, \
"completeness": 0, "standalone": 0, "type": "...", "reason": "one sentence", "misleading_risk": "low"}]}"""


def _chunks(sents, chunk_s=480, overlap_s=30):
    if not sents:
        return []
    out, i = [], 0
    while i < len(sents):
        t0 = sents[i]["start"]
        j = i
        while j < len(sents) and sents[j]["end"] - t0 < chunk_s:
            j += 1
        j = max(j, i + 1)
        out.append(sents[i:j])
        if j >= len(sents):
            break
        k = j
        while k > i + 1 and sents[j - 1]["end"] - sents[k - 1]["start"] < overlap_s:
            k -= 1
        i = max(k, i + 1)
    return out


def llm_discover(llm: LLM, tr, cands, an, info, prefs, video_title="", log=lambda m: None):
    """Ask the LLM for moments per transcript chunk; merge with heuristic candidates."""
    sents = T.sentences(tr)
    chunks = _chunks(sents)
    max_d = float(prefs["duration"])
    min_d = max(8.0, round(max_d * 0.5))
    # prioritise chunks that contain strong heuristic candidates when the video is very long
    if len(chunks) > 24:
        def best(ch):
            return max((c["score"] for c in cands if ch[0]["start"] <= c["start"] <= ch[-1]["end"]), default=0)
        chunks = sorted(sorted(chunks, key=best, reverse=True)[:24], key=lambda ch: ch[0]["start"])
    per_chunk = 3
    found, vtypes = [], []
    words = T.all_words(tr)
    wstarts = [w["s"] for w in words]
    idf = {}
    for k, ch in enumerate(chunks):
        lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in ch)
        user = (f"Video title: {video_title or 'unknown'}\nMaximum clip duration: {max_d:.0f}s (minimum {min_d:.0f}s).\n"
                f"Return at most {per_chunk} moments.\n\nTRANSCRIPT CHUNK {k + 1}/{len(chunks)}:\n{lines}")
        try:
            res = llm.complete_json(LLM_SYSTEM, user, max_tokens=2500)
        except LLMError as e:
            log(f"LLM analysis of chunk {k + 1}/{len(chunks)} failed ({e}); continuing with heuristics for that part")
            continue
        if isinstance(res, dict):
            vtypes.append(str(res.get("video_type", "")).lower())
            ms = res.get("moments", [])
        else:
            ms = []
        for m in ms if isinstance(ms, list) else []:
            try:
                s, e = float(m["start"]), float(m["end"])
            except (KeyError, TypeError, ValueError):
                continue
            # snap to the transcript's real sentence boundaries
            ch_s = min(ch, key=lambda x: abs(x["start"] - s))["start"]
            ch_e = min(ch, key=lambda x: abs(x["end"] - e))["end"]
            if ch_e - ch_s < min_d * 0.8 or ch_e - ch_s > max_d * 1.1:
                continue
            if str(m.get("misleading_risk", "low")).lower() == "high":
                log(f"LLM flagged moment at {ch_s:.0f}s as likely to mislead without context; discarded")
                continue
            found.append((ch_s, ch_e, m))
    log(f"LLM proposed {len(found)} moments across {len(chunks)} transcript chunks")
    if not found:
        return cands, ""
    cnt = {}
    for w in words:
        for t in content_toks(w["w"]):
            cnt[t] = cnt.get(t, 0) + 1
    total = max(1, sum(cnt.values()))
    idf = {t: math.log((total + 1) / (v + 1)) for t, v in cnt.items()}
    new = []
    for s, e, m in found:
        s2, e2 = _refine(s, e, words, wstarts, an["duration"], an["silences"])
        f = _features({"start": s2, "end": e2, "starts_clean": True}, words, an, idf, info, max_d)
        dims = {}
        for k in WEIGHTS:
            h = float(np.clip(f[k], 0, 1)) if k not in ("relevance", "engagement", "emotion") else None
            lv = float(np.clip(float(m.get(k, 5)), 0, 10)) / 10 if k in m else None
            if k in ("relevance", "engagement", "emotion"):
                # heuristic open-ended dims are rank-based; reuse the closest existing candidate's rank
                near = min(cands, key=lambda c: abs(c["start"] - s2) + abs(c["end"] - e2), default=None)
                h = near["dims"][k] if near else 0.5
            dims[k] = round((0.5 * h + 0.5 * lv) if lv is not None else h, 3)
        hs = round(100 * sum(WEIGHTS[k] * dims[k] for k in WEIGHTS), 1)
        new.append({"start": s2, "end": e2, "f": f, "source": "llm", "dims": dims, "heuristic": hs, "score": hs,
                    "llm": {"type": str(m.get("type", "")), "reason": str(m.get("reason", ""))[:300]}})
    # replace heuristic candidates that overlap an LLM-proposed moment; slightly demote the rest
    def iou(a, b):
        inter = max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
        return inter / max(1e-6, (a["end"] - a["start"]) + (b["end"] - b["start"]) - inter)
    kept = [c for c in cands if not any(iou(c, n) > 0.4 for n in new)]
    for c in kept:
        c["score"] = round(c["score"] * 0.9, 1)
    vt = max(set(vtypes), key=vtypes.count) if vtypes else ""
    return kept + new, vt


# ---------------------------------------------------------------- selection
def _overlap(a, b):
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-6, min(a["end"] - a["start"], b["end"] - b["start"]))


def _jaccard(a, b):
    sa, sb = set(content_toks(a["f"]["_text"])), set(content_toks(b["f"]["_text"]))
    return len(sa & sb) / max(1, len(sa | sb))


def select_clips(cands: list[dict], prefs: dict, an: dict) -> tuple[list[dict], str]:
    max_clips = int(prefs.get("max_clips", 5))
    min_score = float(prefs.get("min_score", 40))
    ranked = sorted(cands, key=lambda c: c["score"], reverse=True)
    chosen, note = [], ""
    for c in ranked:
        if c["score"] < min_score and chosen:
            break
        if any(_overlap(c, k) > 0.1 or _jaccard(c, k) > 0.5 for k in chosen):
            continue
        chosen.append(c)
        if len(chosen) >= max_clips:
            break
    if chosen and chosen[0]["score"] < min_score:
        note = "No segment cleared the quality threshold; showing the best available. Consider a different source video."
    for i, c in enumerate(chosen):
        f = c["f"]
        kind = (c.get("llm") or {}).get("type") or ("highlight" if f["emotion"] > 0.6 else "insight" if f["relevance"] > 0.6 else "")
        c["kind"] = kind
        c["reason"] = (c.get("llm") or {}).get("reason") or _reason(f, kind)
    return chosen, note


def keyword_flags(words: list[dict], idf_text_words: list[dict]) -> list[dict]:
    """Mark ~15% of words as 'important' for caption highlighting (numbers, hook words, rare long words)."""
    cnt = {}
    for w in idf_text_words:
        for t in content_toks(w["w"]):
            cnt[t] = cnt.get(t, 0) + 1
    total = max(1, sum(cnt.values()))
    scored = []
    for i, w in enumerate(words):
        t = re.sub(r"[^a-z0-9']", "", w["w"].lower())
        if not t or t in STOP or len(t) < 4 and not t.isdigit():
            continue
        sc = math.log((total + 1) / (cnt.get(t, 0) + 1)) + (2.0 if t.isdigit() else 0) + (1.5 if t in HOOK_WORDS else 0) \
            + (1.0 if w["w"].isupper() and len(t) > 2 else 0)
        scored.append((sc, i))
    k = max(1, int(len(words) * 0.15))
    top = {i for _, i in sorted(scored, reverse=True)[:k]}
    return [dict(w, k=1) if i in top else dict(w) for i, w in enumerate(words)]
