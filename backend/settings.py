"""Per-user settings + editing presets. Secrets are encrypted at rest and never returned to the frontend."""
import copy
import json

from . import db
from .config import cfg
from .security import decrypt, encrypt

SECRET_KEYS = {"anthropic_api_key", "transcribe_api_key", "google_client_secret", "youtube_tokens"}

SIMPLE_KEYS = {
    "anthropic_model", "anthropic_base_url", "transcribe_model", "transcribe_base_url", "transcribe_engine",
    "google_client_id", "auto_publish", "default_privacy", "max_publish_per_day", "default_prefs",
    "source_retention_days", "storage_max_gb", "llm_enabled", "append_shorts_tag",
}

DEFAULT_PREFS = {
    "duration": 45,            # maximum clip length in seconds (15/30/45/60/custom)
    "max_clips": 5,
    "min_score": 40,
    "variants": 1,             # 2 = also render an alternative editing style for the best moments
    "preset": "auto",
    "content_type": "auto",    # auto | podcast | gaming | educational | comedy | vlog | other
    "language": "auto",
    "caption_language": "",    # translate captions to this language (needs LLM) ; "" = original
    "llm_depth": "standard",   # off | standard
    "captions": {
        "enabled": True, "style": "pop", "font": "Poppins", "size": 96, "uppercase": True,
        "color": "#FFFFFF", "highlight": "#FFD400", "keyword_color": "#3DDC97", "outline": "#000000",
        "position": "lower",   # top | middle | lower | bottom
        "words_per_line": 3,
    },
    "audio": {
        "denoise": True, "voice_enhance": True, "normalize": True, "target_lufs": -14.0,
        "music": "auto",       # auto | none | <asset id>
        "music_db": -18.0, "sfx": "auto", "ducking": True,
    },
    "editing": {
        "layout": "auto",      # auto | face | blur | center
        "zoom": "auto",        # auto | off | subtle | punchy
        "remove_pauses": True, "pause_threshold": 0.7, "pause_keep": 0.22,
        "jump_cut_zoom": True, "color": True,
    },
}

PRESETS = {
    "clean": {"editing": {"zoom": "subtle", "jump_cut_zoom": False}, "audio": {"music": "none", "sfx": "none"},
              "captions": {"style": "classic", "uppercase": False, "size": 80}},
    "punchy": {"editing": {"zoom": "punchy", "jump_cut_zoom": True}, "audio": {"music": "auto", "sfx": "auto"},
               "captions": {"style": "pop", "uppercase": True, "size": 104}},
    "podcast": {"editing": {"layout": "face", "zoom": "subtle"}, "audio": {"music": "none", "sfx": "none", "voice_enhance": True},
                "captions": {"style": "pop", "words_per_line": 3}},
    "gaming": {"editing": {"layout": "blur", "zoom": "punchy", "remove_pauses": False},
               "audio": {"music": "none", "sfx": "auto", "ducking": True}, "captions": {"style": "pop", "position": "lower"}},
    "educational": {"editing": {"zoom": "subtle", "jump_cut_zoom": False},
                    "audio": {"music": "auto", "music_db": -24.0, "sfx": "none"},
                    "captions": {"style": "classic", "uppercase": False, "size": 76, "words_per_line": 4}},
    "minimal": {"editing": {"zoom": "off", "jump_cut_zoom": False, "color": False}, "audio": {"music": "none", "sfx": "none"},
                "captions": {"style": "classic", "uppercase": False, "size": 72}},
}

# Which preset the agent picks when the user leaves it on "auto", by detected content type.
CONTENT_TYPE_PRESET = {"podcast": "podcast", "gaming": "gaming", "educational": "educational",
                       "comedy": "punchy", "vlog": "punchy", "other": "clean"}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def get(user_id: int, key: str, default=None):
    row = db.one("SELECT value FROM settings WHERE user_id=? AND key=?", (user_id, key))
    if not row:
        return default
    v = row["value"]
    if key in SECRET_KEYS:
        v = decrypt(v)
    return v


def set_value(user_id: int, key: str, value) -> None:
    if key not in SECRET_KEYS and key not in SIMPLE_KEYS:
        raise ValueError(f"Unknown setting: {key}")
    if not isinstance(value, str):
        value = json.dumps(value)
    if value == "":
        db.run("DELETE FROM settings WHERE user_id=? AND key=?", (user_id, key))
        return
    if key in SECRET_KEYS:
        value = encrypt(value)
    db.run("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
           (user_id, key, value))


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes", "on")


def user_default_prefs(user_id: int) -> dict:
    stored = get(user_id, "default_prefs")
    try:
        over = json.loads(stored) if stored else {}
    except ValueError:
        over = {}
    return deep_merge(DEFAULT_PREFS, over)


def effective(user_id: int) -> dict:
    """Merged, decrypted configuration used by backend components (never sent to the browser)."""
    g = lambda k, d="": get(user_id, k, d)
    return {
        "anthropic_api_key": g("anthropic_api_key") or cfg.ANTHROPIC_API_KEY,
        "anthropic_model": g("anthropic_model") or cfg.ANTHROPIC_MODEL,
        "anthropic_base_url": g("anthropic_base_url") or cfg.ANTHROPIC_BASE_URL,
        "llm_enabled": _bool(g("llm_enabled", "true"), True),
        "transcribe_api_key": g("transcribe_api_key") or cfg.TRANSCRIBE_API_KEY,
        "transcribe_base_url": g("transcribe_base_url") or cfg.TRANSCRIBE_BASE_URL,
        "transcribe_model": g("transcribe_model") or cfg.TRANSCRIBE_MODEL,
        "transcribe_engine": g("transcribe_engine") or "auto",
        "google_client_id": g("google_client_id") or cfg.GOOGLE_CLIENT_ID,
        "google_client_secret": g("google_client_secret") or cfg.GOOGLE_CLIENT_SECRET,
        "auto_publish": _bool(g("auto_publish", "false")),
        "default_privacy": g("default_privacy") or "private",
        "max_publish_per_day": int(g("max_publish_per_day") or 5),
        "append_shorts_tag": _bool(g("append_shorts_tag", "true"), True),
        "source_retention_days": int(g("source_retention_days") or cfg.SOURCE_RETENTION_DAYS),
        "storage_max_gb": float(g("storage_max_gb") or cfg.STORAGE_MAX_GB),
    }


def public_view(user_id: int) -> dict:
    """What the Settings page sees: secrets are reduced to a boolean + the last 4 chars."""
    eff = effective(user_id)
    out = {}
    for k, v in eff.items():
        if k in SECRET_KEYS or k.endswith("_api_key") or k.endswith("_secret"):
            out[k] = {"set": bool(v), "hint": ("…" + v[-4:]) if v and len(v) > 8 else ""}
        else:
            out[k] = v
    out["default_prefs"] = user_default_prefs(user_id)
    return out
