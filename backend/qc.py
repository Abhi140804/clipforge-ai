"""Quality control: measure the finished file, never trust the renderer.

Each check yields status pass|warn|fail. A clip passes when no check fails. For failures the checker proposes
`patches` (edits to the clip's edit-decision JSON) that the agent applies before re-rendering.
"""
import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from . import analysis as A
from . import captions as C
from . import editor
from .llm import LLM, LLMError

SIZES = {"final": (1080, 1920), "preview": (540, 960)}
DANGLING = {"and", "but", "so", "because", "which", "then", "also", "or", "however", "anyway"}


def _chk(name, status, detail, **extra):
    return {"name": name, "status": status, "detail": detail, **extra}


def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    return json.loads(r.stdout)


def _hex_lum(h: str) -> float:
    h = (h or "#FFFFFF").lstrip("#")
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        r = g = b = 1.0
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a: str, b: str) -> float:
    la, lb = _hex_lum(a), _hex_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _black_intervals(path: Path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", "blackdetect=d=0.2:pix_th=0.10", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    out = []
    for m in re.finditer(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", r.stderr):
        out.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
    return out


def _decode_errors(path: Path) -> str:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return r.stderr.strip()[:300]


def _best_offset(words, active, hop=A.HOP):
    """Return (score_at_0, best_offset, best_score) of caption words against a voice-activity mask."""
    def score(off):
        ok = 0
        for w in words:
            a, b = int((w["s"] + off) / hop), max(int((w["s"] + off) / hop) + 1, int((w["e"] + off) / hop))
            seg = active[max(0, a):max(0, b)]
            if seg.size and seg.mean() >= 0.5:
                ok += 1
        return ok / max(1, len(words))
    base = score(0.0)
    best_off, best = 0.0, base
    for off in np.arange(-0.6, 0.61, 0.05):
        sc = score(float(off))
        if sc > best + 0.02:
            best_off, best = float(off), sc
    return base, round(best_off, 2), best


def check_clip(video: Path, edit: dict, info: dict, mode: str = "final", llm: LLM | None = None,
               context: dict | None = None, expect_audio: bool = True) -> dict:
    checks, patches = [], []
    W, H = SIZES[mode]
    if not video.exists() or video.stat().st_size < 10_000:
        c = [_chk("render", "fail", "Output file is missing or empty")]
        return {"passed": False, "checks": c, "patches": [{"check": "render", "patch": {"_safe": True}, "why": "retry with safe settings"}]}
    err = _decode_errors(video)
    checks.append(_chk("render", "fail" if err else "pass", err or "File decodes without errors"))
    if err:
        patches.append({"check": "render", "patch": {"_safe": True}, "why": "decode errors: retry with the simplest filter graph"})
    pr = _probe(video)
    vs = next((s for s in pr.get("streams", []) if s["codec_type"] == "video"), None)
    aus = next((s for s in pr.get("streams", []) if s["codec_type"] == "audio"), None)
    dur = float(pr.get("format", {}).get("duration", 0) or 0)
    if not vs:
        checks.append(_chk("resolution", "fail", "No video stream"))
        return {"passed": False, "checks": checks, "patches": [{"check": "render", "patch": {"_safe": True}, "why": "no video stream"}]}
    ok_res = (vs["width"], vs["height"]) == (W, H)
    checks.append(_chk("resolution", "pass" if ok_res else "fail", f"{vs['width']}x{vs['height']} (expected {W}x{H})"))
    ratio = vs["width"] / vs["height"]
    checks.append(_chk("aspect_ratio", "pass" if abs(ratio - 9 / 16) < 0.002 else "fail", f"{ratio:.4f} (9:16 = 0.5625)"))
    if not ok_res or abs(ratio - 9 / 16) >= 0.002:
        patches.append({"check": "resolution", "patch": {"_safe": True}, "why": "wrong output geometry"})
    exp = float(info.get("out_duration", dur))
    dd = abs(dur - exp)
    checks.append(_chk("duration", "pass" if dd < 0.5 else "fail", f"{dur:.2f}s (planned {exp:.2f}s)"))
    if dd >= 0.5:
        patches.append({"check": "duration", "patch": {"_safe": True}, "why": "duration mismatch"})
    if dur < 3:
        checks.append(_chk("length", "fail", f"Clip is only {dur:.1f}s"))
    elif dur > 180:
        checks.append(_chk("length", "fail", f"{dur:.0f}s exceeds the 3-minute Shorts limit"))
    else:
        checks.append(_chk("length", "pass", f"{dur:.1f}s"))

    # black frames
    blacks = _black_intervals(video)
    bad = None
    for s, e, d in blacks:
        if d > 0.3 and s < 0.15:
            bad = ("start", d)
        elif d > 0.3 and e > dur - 0.15:
            bad = ("end", d)
    total_black = sum(d for _, _, d in blacks)
    if bad:
        checks.append(_chk("black_frames", "fail", f"Black frames at the {bad[0]} ({bad[1]:.1f}s)"))
        patches.append({"check": "black_frames", "patch": {"+trim.start_delta": bad[1] + 0.05} if bad[0] == "start" else {"+trim.end_delta": -(bad[1] + 0.05)},
                        "why": f"trim black frames at the {bad[0]}"})
    elif total_black > 0.15 * dur:
        checks.append(_chk("black_frames", "fail", f"{total_black:.1f}s of black frames"))
        patches.append({"check": "black_frames", "patch": {"_safe": True}, "why": "excess black frames"})
    else:
        checks.append(_chk("black_frames", "pass", "No unexpected black frames" if not blacks else f"{total_black:.1f}s of dark footage, not at the edges"))

    # audio
    if not aus:
        checks.append(_chk("audio", "fail" if expect_audio else "warn", "No audio stream"))
    else:
        loud = editor.measure_loudness(video)
        target = float(edit.get("audio", {}).get("target_lufs", -14.0))
        if not loud:
            checks.append(_chk("loudness", "fail", "Audio is silent or unmeasurable"))
        else:
            li, tp = loud["input_i"], loud["input_tp"]
            norm = edit.get("audio", {}).get("normalize", True)
            if norm and abs(li - target) > 2.5:
                checks.append(_chk("loudness", "fail", f"{li:.1f} LUFS (target {target:.0f} ±2.5)", value=li))
                patches.append({"check": "loudness", "patch": {"audio.target_lufs": round(max(-24, min(-8, target + (target - li))), 1)},
                                "why": f"loudness {li:.1f} LUFS is off target"})
            elif tp > -0.4:
                checks.append(_chk("loudness", "fail", f"True peak {tp:.1f} dBTP is too hot", value=li))
                patches.append({"check": "loudness", "patch": {"audio.target_lufs": round(target - (tp + 1.2), 1)}, "why": "true peak above -0.4 dBTP"})
            else:
                checks.append(_chk("loudness", "pass", f"{li:.1f} LUFS, true peak {tp:.1f} dBTP", value=li))
        # envelope-based checks run on the final mix
        db = A.audio_envelope(str(video))
        if db.size:
            clipped = float((db > -0.3).mean())
            checks.append(_chk("clipping", "fail" if clipped > 0.005 else "pass", f"{clipped * 100:.2f}% of frames at full scale"))
            if clipped > 0.005:
                patches.append({"check": "clipping", "patch": {"audio.target_lufs": round(target - 2.0, 1)}, "why": "clipping"})
            dyn = float(np.percentile(db, 90) - np.percentile(db, 10))
            den = edit.get("audio", {}).get("denoise", True)
            if dyn < 20 and not den:
                checks.append(_chk("audio_clarity", "fail", f"Noisy: only {dyn:.0f} dB between speech and background"))
                patches.append({"check": "audio_clarity", "patch": {"audio.denoise": True, "audio.voice_enhance": True}, "why": "noise floor too high"})
            elif dyn < 14:
                checks.append(_chk("audio_clarity", "warn", f"Low speech-to-background contrast ({dyn:.0f} dB)"))
            else:
                checks.append(_chk("audio_clarity", "pass", f"{dyn:.0f} dB between speech and background"))
            thr = float(np.percentile(db, 90)) - 25
            per = int(0.5 / A.HOP)
            lead = float(np.mean(db[:per] < thr)) if db.size >= per else 0
            tail = float(np.mean(db[-int(1.0 / A.HOP):] < thr)) if db.size >= int(1.0 / A.HOP) else 0
            if lead > 0.9:
                checks.append(_chk("clean_start", "warn", "The first half-second is silent"))
                patches.append({"check": "clean_start", "patch": {"+trim.start_delta": 0.3}, "why": "trim leading silence", "soft": True})
            else:
                checks.append(_chk("clean_start", "pass", "Starts with sound"))
            if tail > 0.9:
                checks.append(_chk("clean_end", "warn", "The last second is silent"))
                patches.append({"check": "clean_end", "patch": {"+trim.end_delta": -0.5}, "why": "trim trailing silence", "soft": True})
            else:
                checks.append(_chk("clean_end", "pass", "Ends without dead air"))

    # captions
    cap = edit.get("captions", {})
    words = info.get("out_words") or []
    if cap.get("enabled", True) and info.get("captions_burned") and words:
        voice_wav = info.get("voice_path")
        mask_src = Path(voice_wav) if voice_wav and Path(voice_wav).exists() else video
        vdb = A.audio_envelope(str(mask_src))
        active = (vdb > (np.percentile(vdb, 90) - 22)).astype(np.float32) if vdb.size else np.zeros(1)
        base, off, best = _best_offset(words, active)
        if base >= 0.7:
            checks.append(_chk("caption_sync", "pass", f"{base * 100:.0f}% of caption words fall on speech"))
        elif best >= base + 0.1 and abs(off) >= 0.1:
            checks.append(_chk("caption_sync", "fail", f"Only {base * 100:.0f}% on speech; best alignment is {off:+.2f}s ({best * 100:.0f}%)"))
            patches.append({"check": "caption_sync", "patch": {"+caption_offset": off}, "why": f"shift captions {off:+.2f}s"})
        else:
            checks.append(_chk("caption_sync", "warn", f"{base * 100:.0f}% of caption words fall on detected speech (word timing may be approximate)"))
        size = int(cap.get("size", 96))
        ok_size = size >= 56
        mc = C.max_chars_for(size, 1080, cap.get("uppercase", True))
        lines = C.lines_for_editing(words, cap)
        long_lines = [l for l in lines if len(l["text"]) > mc + 3]
        fast = [l for l in lines if (l["end"] - l["start"] + 0.2) > 0 and len(l["text"]) / (l["end"] - l["start"] + 0.2) > 28]
        cr = contrast(cap.get("color", "#FFFFFF"), cap.get("outline", "#000000"))
        problems = []
        if not ok_size:
            problems.append(f"font size {size}px is too small for a phone")
            patches.append({"check": "caption_readability", "patch": {"captions.size": 80}, "why": "enlarge captions"})
        if long_lines:
            problems.append(f"{len(long_lines)} lines exceed {mc} characters")
            patches.append({"check": "caption_readability", "patch": {"captions.words_per_line": max(2, int(cap.get("words_per_line", 3)) - 1)}, "why": "shorter caption lines"})
        if cr < 4.5:
            problems.append(f"text/outline contrast {cr:.1f}:1 is below 4.5:1")
            patches.append({"check": "caption_readability", "patch": {"captions.outline": "#000000", "captions.color": "#FFFFFF"}, "why": "restore high-contrast colours"})
        if len(fast) > max(2, len(lines) // 4):
            problems.append("reading speed is too fast for several lines")
        checks.append(_chk("caption_readability", "fail" if (not ok_size or long_lines or cr < 4.5) else ("warn" if problems else "pass"),
                           "; ".join(problems) or f"{len(lines)} lines, size {size}px, contrast {cr:.1f}:1"))
    elif cap.get("enabled", True):
        checks.append(_chk("captions", "warn", "Captions were requested but no transcript was available for this clip"))

    # beginning / ending / context
    ctx_words = info.get("clip_words") or []
    if ctx_words:
        first = re.sub(r"[^a-z']", "", ctx_words[0]["w"].lower())
        last_w = ctx_words[-1]["w"]
        issues = []
        if first in DANGLING:
            issues.append(f"starts with the connective '{ctx_words[0]['w']}' (refers to something not in the clip)")
        if not re.search(r"[.!?…][\"')\]]*$", last_w):
            issues.append("ends without finishing the sentence")
        verdict, detail, trim_patch = ("pass", "Starts and ends on complete thoughts", None)
        if issues:
            verdict, detail = "warn", "; ".join(issues)
        if llm and llm.available and context:
            try:
                res = llm.complete_json(
                    "You review short-form video clips cut from longer videos. Decide whether the clip preserves the speaker's meaning, starts and ends at natural points, "
                    "and would not mislead a viewer who has not seen the rest. Return JSON {\"verdict\":\"ok|warn|fail\",\"issue\":\"one sentence\"}.",
                    f"BEFORE THE CLIP: {context.get('before', '')[-500:]}\n\nCLIP: {context.get('text', '')}\n\nAFTER THE CLIP: {context.get('after', '')[:500]}", 400)
                v = str(res.get("verdict", "ok")).lower()
                if v in ("warn", "fail"):
                    verdict, detail = v, f"LLM review: {res.get('issue', '')}"
                elif not issues:
                    detail = "LLM review: meaning preserved; natural start and end"
            except (LLMError, AttributeError):
                pass
        checks.append(_chk("context", verdict, detail))
        if verdict == "fail":
            patches.append({"check": "context", "patch": {"+trim.end_delta": 2.0}, "why": "extend the ending to finish the thought", "soft": True})
    passed = all(c["status"] != "fail" for c in checks)
    return {"passed": passed, "checks": checks, "patches": [] if passed else patches}
