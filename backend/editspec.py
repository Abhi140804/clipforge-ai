"""Validation of edit decisions and preferences coming from the UI or from the LLM agent.

Never trust either: every key is whitelisted and every value is range/enum checked before it reaches the renderer.
"""
import re

from . import assets, captions, settings

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

BOOLS = {"remove_pauses", "jump_cut_zoom", "color", "captions.enabled", "captions.uppercase",
         "audio.denoise", "audio.voice_enhance", "audio.normalize", "audio.ducking"}
ENUMS = {
    "layout": ["auto", "face", "blur", "center"],
    "zoom": ["auto", "off", "subtle", "punchy"],
    "captions.style": ["pop", "classic", "minimal"],
    "captions.position": ["top", "middle", "lower", "bottom"],
    "sfx": ["none", "auto"],
}
RANGES = {
    "crop_offset": (-1.0, 1.0), "crop_zoom": (1.0, 2.0), "caption_offset": (-1.0, 1.0),
    "captions.size": (40, 160), "captions.words_per_line": (1, 6),
    "audio.music_db": (-40.0, -6.0), "audio.target_lufs": (-24.0, -9.0),
    "pause_threshold": (0.3, 2.0), "pause_keep": (0.05, 0.6),
}
COLORS = {"captions.color", "captions.highlight", "captions.keyword_color", "captions.outline"}
INT_KEYS = {"captions.size", "captions.words_per_line"}


def describe_for_agent() -> str:
    d = {}
    for k in sorted(BOOLS):
        d[k] = "bool"
    d.update({k: v for k, v in ENUMS.items()})
    d["music"] = ["none", "Calm Pad", "Upbeat Pulse", "Tension Drone"]
    d["trim_start/trim_end"] = "seconds in [-3,3]; negative start extends earlier"
    return str(d).replace("'", '"')


def validate_edit(changes: dict, user_id: int, max_trim: float = 10.0) -> tuple[dict, dict, dict]:
    """Returns (patch for producer.apply_patch, applied, rejected)."""
    patch, applied, rejected = {}, {}, {}
    for k, v in (changes or {}).items():
        try:
            if k in ("trim_start", "trim_end"):
                d = float(v)
                if abs(d) > max_trim:
                    raise ValueError(f"must be within ±{max_trim:g}")
                patch["trim." + ("start_delta" if k == "trim_start" else "end_delta")] = d
            elif k in BOOLS:
                if not isinstance(v, bool):
                    raise ValueError("must be true or false")
                patch[k] = v
            elif k in ENUMS:
                if v not in ENUMS[k]:
                    raise ValueError(f"must be one of {ENUMS[k]}")
                patch[k] = v
            elif k in RANGES:
                lo, hi = RANGES[k]
                n = float(v)
                if not lo <= n <= hi:
                    raise ValueError(f"must be between {lo} and {hi}")
                patch[k] = int(n) if k in INT_KEYS else n
            elif k in COLORS:
                if not (isinstance(v, str) and HEX.match(v)):
                    raise ValueError("must be a #RRGGBB colour")
                patch[k] = v
            elif k == "captions.font":
                if v not in captions.available_fonts():
                    raise ValueError(f"font not installed; choose from {captions.available_fonts()}")
                patch[k] = v
            elif k == "music":
                if v in (None, "", "none"):
                    patch["music_asset"] = None
                elif str(v).isdigit():
                    a = assets.get_asset(v, user_id)
                    if not a or a["kind"] != "music":
                        raise ValueError("unknown music asset")
                    patch["music_asset"] = str(a["id"])
                elif assets.named("music", str(v)):
                    patch["music_asset"] = str(v)
                else:
                    raise ValueError("unknown music asset")
            else:
                raise ValueError("unknown setting")
            applied[k] = v
        except (TypeError, ValueError) as e:
            rejected[k] = str(e)
    return patch, applied, rejected


def clean_prefs(p: dict) -> dict:
    """Sanitise project / default preferences (nested form as stored in the DB)."""
    out = {}
    if not isinstance(p, dict):
        return out

    def num(v, lo, hi, cast=float):
        return cast(max(lo, min(hi, float(v))))

    try:
        if "duration" in p:
            out["duration"] = num(p["duration"], 10, 180, int)
        if "max_clips" in p:
            out["max_clips"] = num(p["max_clips"], 1, 12, int)
        if "min_score" in p:
            out["min_score"] = num(p["min_score"], 0, 100)
        if "variants" in p:
            out["variants"] = 2 if int(p["variants"]) >= 2 else 1
    except (TypeError, ValueError):
        pass
    if p.get("preset") in list(settings.PRESETS) + ["auto"]:
        out["preset"] = p["preset"]
    if p.get("content_type") in ("auto", "podcast", "gaming", "educational", "comedy", "vlog", "other"):
        out["content_type"] = p["content_type"]
    if isinstance(p.get("language"), str) and re.fullmatch(r"[A-Za-z-]{2,10}", p["language"]):
        out["language"] = p["language"]
    if isinstance(p.get("caption_language"), str) and re.fullmatch(r"[A-Za-z -]{0,24}", p["caption_language"]):
        out["caption_language"] = p["caption_language"].strip()
    if p.get("llm_depth") in ("off", "standard"):
        out["llm_depth"] = p["llm_depth"]
    for group, prefix in (("captions", "captions."), ("audio", "audio."), ("editing", "")):
        sub = p.get(group)
        if not isinstance(sub, dict):
            continue
        flat = {}
        for k, v in sub.items():
            key = prefix + k
            if group == "audio" and k == "music":
                if v in ("auto", "none", "", None) or str(v).isdigit() or assets.named("music", str(v)):
                    out.setdefault("audio", {})["music"] = v if v else "none"
                continue
            if group == "audio" and k == "sfx" and v in ("auto", "none"):
                out.setdefault("audio", {})["sfx"] = v
                continue
            flat[key] = v
        patch, _, _ = validate_edit(flat, 0)
        for dotted, v in patch.items():
            if group == "editing":
                if "." in dotted or dotted in ("sfx", "music_asset", "crop_offset", "crop_zoom", "caption_offset"):
                    continue
                out.setdefault("editing", {})[dotted] = v
            elif dotted.startswith(prefix):
                out.setdefault(group, {})[dotted[len(prefix):]] = v
    return out
