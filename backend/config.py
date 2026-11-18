"""Central configuration. Everything comes from environment variables / .env.

Per-user overrides (API keys, preferences) live in the database; see settings.py.
"""
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    BASE_URL = env("CLIPFORGE_BASE_URL", "http://localhost:8000").rstrip("/")
    HOST = env("CLIPFORGE_HOST", "127.0.0.1")
    PORT = env_int("CLIPFORGE_PORT", 8000)

    DATA_DIR = Path(env("CLIPFORGE_DATA", str(ROOT / "data"))).resolve()
    DB_PATH = DATA_DIR / "clipforge.db"
    UPLOAD_DIR = DATA_DIR / "uploads"
    SOURCE_DIR = DATA_DIR / "sources"
    WORK_DIR = DATA_DIR / "work"
    CLIP_DIR = DATA_DIR / "clips"
    ASSET_DIR = DATA_DIR / "assets"
    FRONTEND_DIR = ROOT / "frontend"

    WORKERS = max(1, env_int("CLIPFORGE_WORKERS", 2))
    MAX_UPLOAD_MB = env_int("CLIPFORGE_MAX_UPLOAD_MB", 4096)
    MAX_DOWNLOAD_MB = env_int("CLIPFORGE_MAX_DOWNLOAD_MB", 8192)
    MAX_ASSET_MB = 100
    ALLOW_SIGNUP = env_bool("CLIPFORGE_ALLOW_SIGNUP", False)  # first user can always sign up
    ALLOW_PRIVATE_URLS = env_bool("CLIPFORGE_ALLOW_PRIVATE_URLS", False)  # SSRF guard; only for tests
    COOKIE_SECURE = env_bool("CLIPFORGE_COOKIE_SECURE", BASE_URL.startswith("https"))

    # Storage management
    STORAGE_MAX_GB = float(env("CLIPFORGE_STORAGE_MAX_GB", "50"))
    SOURCE_RETENTION_DAYS = env_int("CLIPFORGE_SOURCE_RETENTION_DAYS", 3)

    # LLM (Anthropic Messages API). Users can override the key in Settings.
    ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
    ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    ANTHROPIC_BASE_URL = env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    # Speech recognition: OpenAI-compatible /audio/transcriptions endpoint (OpenAI, Groq, self-hosted...)
    TRANSCRIBE_API_KEY = env("TRANSCRIBE_API_KEY")
    TRANSCRIBE_BASE_URL = env("TRANSCRIBE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    TRANSCRIBE_MODEL = env("TRANSCRIBE_MODEL", "whisper-1")
    LOCAL_WHISPER_MODEL = env("LOCAL_WHISPER_MODEL", "base")

    # Google / YouTube (official Data API v3). Endpoints are overridable so tests can use a mock.
    GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
    GOOGLE_AUTH_URL = env("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
    GOOGLE_TOKEN_URL = env("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
    YT_API_BASE = env("YT_API_BASE", "https://www.googleapis.com/youtube/v3").rstrip("/")
    YT_UPLOAD_BASE = env("YT_UPLOAD_BASE", "https://www.googleapis.com/upload/youtube/v3").rstrip("/")
    YT_DAILY_QUOTA = env_int("YT_DAILY_QUOTA", 10000)
    YT_UPLOAD_COST = 1600  # quota units for videos.insert

    @property
    def SECRET_KEY(self) -> str:
        k = env("CLIPFORGE_SECRET_KEY")
        if k:
            return k
        f = self.DATA_DIR / ".secret_key"
        if f.exists():
            return f.read_text().strip()
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        k = secrets.token_urlsafe(48)
        f.write_text(k)
        os.chmod(f, 0o600)
        return k

    def ensure_dirs(self) -> None:
        for d in (self.DATA_DIR, self.UPLOAD_DIR, self.SOURCE_DIR, self.WORK_DIR, self.CLIP_DIR, self.ASSET_DIR):
            d.mkdir(parents=True, exist_ok=True)


cfg = Config()
cfg.ensure_dirs()
