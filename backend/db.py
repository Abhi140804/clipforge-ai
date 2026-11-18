"""SQLite persistence (WAL mode, one connection per thread)."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .config import cfg

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
  user_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL,           -- url | upload
  source_url TEXT,
  platform TEXT,                        -- youtube | twitch | kick | direct | upload
  status TEXT NOT NULL DEFAULT 'queued',-- queued running completed failed cancelled needs_upload
  stage TEXT NOT NULL DEFAULT '',
  progress REAL NOT NULL DEFAULT 0,
  error TEXT,
  video_path TEXT,
  sidecar_path TEXT,
  duration REAL,
  width INTEGER, height INTEGER,
  meta TEXT NOT NULL DEFAULT '{}',
  prefs TEXT NOT NULL DEFAULT '{}',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, created_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  tool TEXT,
  message TEXT NOT NULL,
  data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  variant TEXT NOT NULL DEFAULT 'A',
  start REAL NOT NULL, end REAL NOT NULL, duration REAL NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  score_detail TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', -- pending rendering ready failed
  error TEXT,
  file_path TEXT, preview_path TEXT, thumb_path TEXT,
  edit TEXT NOT NULL DEFAULT '{}',
  words TEXT NOT NULL DEFAULT '[]',
  qc TEXT NOT NULL DEFAULT '{}',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clips_project ON clips(project_id, idx);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,                   -- process | render
  project_id INTEGER, clip_id INTEGER,
  args TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued',-- queued running done failed cancelled
  error TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS publish_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  clip_id INTEGER NOT NULL,
  title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  privacy TEXT NOT NULL DEFAULT 'private',
  schedule_at REAL,
  status TEXT NOT NULL,   -- pending_approval scheduled uploading published failed rate_limited cancelled
  youtube_id TEXT, error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, published_at REAL
);
CREATE TABLE IF NOT EXISTS quota_usage(
  user_id INTEGER NOT NULL, day TEXT NOT NULL, units INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS oauth_states(
  state TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,                      -- NULL = built-in, generated locally (royalty-free)
  kind TEXT NOT NULL,                   -- music | sfx
  name TEXT NOT NULL, path TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(str(cfg.DB_PATH), timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c = c
    return c


def init() -> None:
    conn().executescript(SCHEMA)


def q(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql: str, args=()) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def now() -> float:
    return time.time()


def jl(s, default=None):
    """json.loads that never raises."""
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def jd(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def update(table: str, row_id: int, **fields) -> None:
    if not fields:
        return
    allowed_tables = {"projects", "clips", "jobs", "publish_items"}
    assert table in allowed_tables
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
