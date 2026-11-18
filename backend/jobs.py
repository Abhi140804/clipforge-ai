"""Background processing.

Design choice: a persistent job queue in SQLite + a pool of worker threads, instead of Celery/Redis. The heavy work runs in
FFmpeg/Whisper subprocesses (so threads are fine), there is nothing extra to deploy, and jobs survive restarts (anything that
was `running` when the server stopped is re-queued). The `enqueue/claim/run` seam is small, so swapping in Celery later only
means replacing this module.

Also hosts the YouTube publishing scheduler and storage housekeeping (retention + size cap).
"""
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import db, editor, producer, settings, youtube
from .agent import Agent
from .config import cfg
from .llm import LLM
from .producer import Cancelled

_wake = threading.Event()
_stop = threading.Event()
_threads: list[threading.Thread] = []
_started = False


def enqueue(kind: str, project_id: int | None = None, clip_id: int | None = None, args: dict | None = None) -> int:
    jid = db.run("INSERT INTO jobs(kind,project_id,clip_id,args,status,created_at) VALUES(?,?,?,?,?,?)",
                 (kind, project_id, clip_id, db.jd(args or {}), "queued", db.now()))
    _wake.set()
    return jid


def _claim() -> dict | None:
    with db.tx() as c:
        row = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (db.now(), row["id"]))
        return dict(row)


def _project_log(pid: int, msg: str, level: str = "info", tool: str | None = None):
    db.run("INSERT INTO events(project_id,ts,level,tool,message) VALUES(?,?,?,?,?)", (pid, db.now(), level, tool, msg[:1500]))


def _run_process(job: dict):
    pid = job["project_id"]
    proj = db.one("SELECT status, cancel_requested FROM projects WHERE id=?", (pid,))
    if not proj or proj["cancel_requested"]:
        db.update("projects", pid, status="cancelled", stage="cancelled", updated_at=db.now()) if proj else None
        return
    Agent(pid).run()


def _run_render(job: dict):
    cid, args = job["clip_id"], db.jl(job["args"], {})
    clip = db.one("SELECT * FROM clips WHERE id=?", (cid,))
    if not clip:
        return
    pid, mode = clip["project_id"], args.get("mode", "final")
    ucfg = settings.effective(clip["user_id"])
    llm = LLM.for_user(ucfg)
    log = lambda m: _project_log(pid, m, tool="render_clip")
    try:
        res = producer.render_clip(cid, mode, repair=(mode == "final"), log=log, llm=llm if mode == "final" else None)
        if res["ok"]:
            _project_log(pid, f"Clip {clip['idx']}{clip['variant']} {'re-rendered' if mode == 'final' else 'preview ready'}", tool="render_clip")
        else:
            _project_log(pid, f"Clip {clip['idx']}{clip['variant']} failed: {res.get('error', '')[:300]}", "error", "render_clip")
    except Cancelled:
        db.update("clips", cid, status="pending" if mode == "final" else clip["status"], updated_at=db.now())
    except editor.RenderError as e:
        db.update("clips", cid, status="failed" if mode == "final" else clip["status"], error=str(e)[:500], updated_at=db.now())
        _project_log(pid, f"Render error: {e}", "error", "render_clip")


def _worker(n: int):
    while not _stop.is_set():
        job = None
        try:
            job = _claim()
        except Exception:
            time.sleep(1)
            continue
        if not job:
            _wake.wait(2.0)
            _wake.clear()
            continue
        try:
            (_run_process if job["kind"] == "process" else _run_render)(job)
            db.update("jobs", job["id"], status="done", finished_at=db.now())
        except Exception as e:  # the agent catches its own errors; this is the last line of defence
            db.update("jobs", job["id"], status="failed", error=str(e)[:500], finished_at=db.now())
            if job.get("project_id"):
                db.update("projects", job["project_id"], status="failed", stage="failed", error=f"Worker error: {e}"[:500], updated_at=db.now())
                _project_log(job["project_id"], f"Worker error: {e}\n{traceback.format_exc()[-800:]}", "error")
            elif job.get("clip_id"):
                c = db.one("SELECT project_id FROM clips WHERE id=?", (job["clip_id"],))
                if c:
                    db.update("clips", job["clip_id"], status="failed", error=str(e)[:400], updated_at=db.now())


# ------------------------------------------------------------------ storage
def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def storage_usage() -> dict:
    parts = {"sources": cfg.SOURCE_DIR, "uploads": cfg.UPLOAD_DIR, "work": cfg.WORK_DIR, "clips": cfg.CLIP_DIR, "assets": cfg.ASSET_DIR}
    sizes = {k: _dir_size(v) for k, v in parts.items()}
    total = sum(sizes.values())
    disk = shutil.disk_usage(cfg.DATA_DIR)
    return {"bytes": sizes, "total": total, "disk_free": disk.free, "limit": int(cfg.STORAGE_MAX_GB * 1024 ** 3)}


def _remove_source(proj: dict) -> int:
    freed = 0
    for key in ("video_path", "sidecar_path"):
        p = proj.get(key)
        if p and Path(p).exists():
            try:
                freed += Path(p).stat().st_size
                Path(p).unlink()
            except OSError:
                continue
    for base in (cfg.SOURCE_DIR, cfg.UPLOAD_DIR):
        d = base / f"p{proj['id']}"
        if d.exists():
            freed += _dir_size(d)
            shutil.rmtree(d, ignore_errors=True)
    meta = db.jl(proj["meta"], {})
    meta["source_removed"] = True
    db.update("projects", proj["id"], meta=db.jd(meta), updated_at=db.now())
    return freed


def cleanup(retention_days: int | None = None, force_all: bool = False) -> dict:
    """Delete source videos past retention (and, over the size cap, oldest first). Rendered clips are never auto-deleted."""
    freed, removed = 0, 0
    days = cfg.SOURCE_RETENTION_DAYS if retention_days is None else retention_days
    rows = db.q("SELECT * FROM projects WHERE status IN ('completed','failed','cancelled') ORDER BY updated_at")
    for p in rows:
        meta = db.jl(p["meta"], {})
        if meta.get("source_removed"):
            continue
        if force_all or db.now() - p["updated_at"] > days * 86400:
            freed += _remove_source(p)
            removed += 1
    # transient ASR/work leftovers of finished projects
    for d in cfg.WORK_DIR.glob("p*/asr"):
        shutil.rmtree(d, ignore_errors=True)
    limit = cfg.STORAGE_MAX_GB * 1024 ** 3
    if storage_usage()["total"] > limit:
        for p in rows:
            if storage_usage()["total"] <= limit:
                break
            if not db.jl(db.one("SELECT meta FROM projects WHERE id=?", (p["id"],))["meta"], {}).get("source_removed"):
                freed += _remove_source(p)
                removed += 1
    return {"sources_removed": removed, "bytes_freed": freed}


def _scheduler():
    last_clean = 0.0
    while not _stop.wait(15):
        try:
            youtube.process_due(lambda m: None)
            if time.time() - last_clean > 3600:
                last_clean = time.time()
                cleanup()
        except Exception:
            pass


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    db.run("UPDATE jobs SET status='queued' WHERE status='running'")  # recover from a crash/restart
    for i in range(cfg.WORKERS):
        t = threading.Thread(target=_worker, args=(i,), daemon=True, name=f"worker-{i}")
        t.start()
        _threads.append(t)
    t = threading.Thread(target=_scheduler, daemon=True, name="scheduler")
    t.start()
    _threads.append(t)


def stop() -> None:
    _stop.set()
    _wake.set()
