"""ClipForge HTTP API (Starlette) + static frontend."""
import threading
import json
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import assets, captions, db, detectors, editor, editspec, ingest, jobs, metadata, producer, security, settings, youtube
from . import transcribe as T
from .config import cfg
from .llm import LLM, LLMError

COOKIE = "cf_session"
login_limiter = security.RateLimiter(10, 300)
signup_limiter = security.RateLimiter(5, 3600)
probe_limiter = security.RateLimiter(30, 60)
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


def J(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def authed(fn):
    async def wrapper(request: Request):
        uid = security.read_token(request.cookies.get(COOKIE, ""))
        if not uid or not db.one("SELECT 1 AS x FROM users WHERE id=?", (uid,)):
            raise ApiError(401, "Please sign in.")
        request.state.uid = uid
        return await fn(request)
    return wrapper


async def body(request: Request) -> dict:
    if int(request.headers.get("content-length") or 0) > 2_000_000:
        raise ApiError(413, "Request too large.")
    try:
        data = await request.json()
    except ValueError:
        raise ApiError(400, "Invalid JSON body.")
    if not isinstance(data, dict):
        raise ApiError(400, "Expected a JSON object.")
    return data


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# ------------------------------------------------------------------ views
def project_view(p: dict, with_counts: bool = True) -> dict:
    meta = db.jl(p["meta"], {})
    out = {k: p[k] for k in ("id", "title", "source_type", "source_url", "platform", "status", "stage", "progress", "error",
                             "duration", "width", "height", "created_at", "updated_at")}
    out["prefs"] = db.jl(p["prefs"], {})
    out["meta"] = {k: meta.get(k) for k in ("content_type", "agent_summary", "llm_usage", "source_removed", "platform_info")}
    if with_counts:
        rows = db.q("SELECT status, COUNT(*) n FROM clips WHERE project_id=? GROUP BY status", (p["id"],))
        out["clip_counts"] = {r["status"]: r["n"] for r in rows}
        t = db.one("SELECT thumb_path, id FROM clips WHERE project_id=? AND status='ready' AND thumb_path IS NOT NULL ORDER BY score DESC LIMIT 1", (p["id"],))
        out["thumb_clip_id"] = t["id"] if t else None
    return out


def clip_view(c: dict) -> dict:
    qc = db.jl(c["qc"], {})
    edit = db.jl(c["edit"], {})
    words = db.jl(c["words"], [])
    stale = bool(c["status"] == "ready" and qc.get("edit_hash") and qc["edit_hash"] != producer.edit_fingerprint(edit, c["words"]))
    return {"id": c["id"], "project_id": c["project_id"], "idx": c["idx"], "variant": c["variant"], "start": c["start"], "end": c["end"],
            "duration": c["duration"], "score": c["score"], "score_detail": db.jl(c["score_detail"], {}), "reason": c["reason"],
            "kind": c["kind"], "status": c["status"], "error": c["error"], "edit": edit, "qc": qc,
            "metadata": db.jl(c["metadata"], {}), "stale": stale, "word_count": len(words), "updated_at": c["updated_at"],
            "has_file": bool(c["file_path"] and Path(c["file_path"]).exists()),
            "has_preview": bool(c["preview_path"] and Path(c["preview_path"]).exists()),
            "has_thumb": bool(c["thumb_path"] and Path(c["thumb_path"]).exists())}


def get_project(request: Request, pid: int) -> dict:
    p = db.one("SELECT * FROM projects WHERE id=? AND user_id=?", (pid, request.state.uid))
    if not p:
        raise ApiError(404, "Project not found.")
    return p


def get_clip(request: Request, cid: int) -> dict:
    c = db.one("SELECT * FROM clips WHERE id=? AND user_id=?", (cid, request.state.uid))
    if not c:
        raise ApiError(404, "Clip not found.")
    return c


def pid_of(request: Request) -> int:
    return int(request.path_params["id"])


# ------------------------------------------------------------------ auth
async def auth_me(request: Request):
    uid = security.read_token(request.cookies.get(COOKIE, ""))
    u = db.one("SELECT id,email FROM users WHERE id=?", (uid,)) if uid else None
    n = db.one("SELECT COUNT(*) n FROM users")["n"]
    return J({"user": u, "first_run": n == 0, "signup_open": n == 0 or cfg.ALLOW_SIGNUP})


def _set_session(resp: Response, uid: int) -> Response:
    resp.set_cookie(COOKIE, security.make_token(uid), max_age=security.SESSION_TTL, httponly=True, samesite="lax", secure=cfg.COOKIE_SECURE, path="/")
    return resp


async def auth_signup(request: Request):
    d = await body(request)
    if not signup_limiter.allow(client_ip(request)):
        raise ApiError(429, "Too many sign-up attempts. Try again later.")
    n = db.one("SELECT COUNT(*) n FROM users")["n"]
    if n > 0 and not cfg.ALLOW_SIGNUP:
        raise ApiError(403, "Sign-ups are closed on this server.")
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if not EMAIL.match(email):
        raise ApiError(400, "Enter a valid email address.")
    if len(pw) < 10:
        raise ApiError(400, "Password must be at least 10 characters.")
    if db.one("SELECT 1 AS x FROM users WHERE email=?", (email,)):
        raise ApiError(409, "An account with that email already exists.")
    h = await run_in_threadpool(security.hash_password, pw)
    uid = db.run("INSERT INTO users(email,pw_hash,created_at) VALUES(?,?,?)", (email, h, db.now()))
    return _set_session(J({"user": {"id": uid, "email": email}}), uid)


_DUMMY = security.hash_password("dummy-password-for-timing")


async def auth_login(request: Request):
    d = await body(request)
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if not login_limiter.allow(f"{client_ip(request)}|{email}"):
        raise ApiError(429, "Too many attempts. Wait a few minutes.")
    u = db.one("SELECT * FROM users WHERE email=?", (email,))
    ok = await run_in_threadpool(security.verify_password, pw, u["pw_hash"] if u else _DUMMY)
    if not u or not ok:
        raise ApiError(401, "Wrong email or password.")
    return _set_session(J({"user": {"id": u["id"], "email": u["email"]}}), u["id"])


async def auth_logout(request: Request):
    r = J({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


# ------------------------------------------------------------------ meta / dashboard
@authed
async def meta_view(request: Request):
    uid = request.state.uid
    ucfg = settings.effective(uid)
    return J({
        "presets": list(settings.PRESETS), "preset_defs": settings.PRESETS, "content_types": ["auto", "podcast", "gaming", "educational", "comedy", "vlog", "other"],
        "fonts": captions.available_fonts(), "assets": assets.list_assets(uid), "defaults": settings.DEFAULT_PREFS,
        "caps": {
            "llm": bool(ucfg["anthropic_api_key"] and ucfg["llm_enabled"]), "yt_dlp": bool(ingest.ytdlp_cmd()),
            "transcribe": T.available_engines(ucfg), "faces": detectors.backend_name() if detectors.available() else None,
            "youtube_configured": bool(ucfg["google_client_id"] and ucfg["google_client_secret"]),
            "youtube_connected": youtube.connected(uid), "max_upload_mb": cfg.MAX_UPLOAD_MB,
        }})


@authed
async def dashboard(request: Request):
    uid = request.state.uid
    ps = db.q("SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC", (uid,))
    day = 86400
    since = time.time() - 13 * day
    activity = []
    today0 = int(time.time() // day) * day
    for i in range(13, -1, -1):
        a, b = today0 - i * day, today0 - i * day + day
        activity.append({"day": time.strftime("%m-%d", time.gmtime(a)),
                         "projects": sum(1 for p in ps if a <= p["created_at"] < b),
                         "clips": db.one("SELECT COUNT(*) n FROM clips WHERE user_id=? AND status='ready' AND updated_at>=? AND updated_at<?", (uid, a, b))["n"]})
    pub = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM publish_items WHERE user_id=? GROUP BY status", (uid,))}
    clip_totals = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM clips WHERE user_id=? GROUP BY status", (uid,))}
    best = db.one("SELECT AVG(score) a FROM clips WHERE user_id=? AND status='ready'", (uid,))["a"]
    return J({"videos_processed": sum(1 for p in ps if p["status"] == "completed"), "projects_total": len(ps),
              "clips_ready": clip_totals.get("ready", 0), "clips_failed": clip_totals.get("failed", 0),
              "avg_score": round(best, 1) if best else None,
              "active": [project_view(p) for p in ps if p["status"] in ("queued", "running")],
              "recent": [project_view(p) for p in ps[:6]], "activity": activity, "publishing": pub,
              "quota": youtube.quota(uid) if youtube.connected(uid) else None})


# ------------------------------------------------------------------ projects
def _ok_url(url: str) -> tuple[str, str]:
    url = (url or "").strip()
    if len(url) > 2000:
        raise ApiError(400, "URL is too long.")
    err = security.check_url(url)
    if err:
        raise ApiError(400, err)
    return url, ingest.detect_platform(url)[0]


def _require_rights(d: dict):
    if d.get("rights_confirmed") is not True:
        raise ApiError(400, "Please confirm that you own this video or are authorised to use it.")


def _create_project(uid: int, source_type: str, url: str | None, platform: str, prefs: dict, title: str = "", video_path: str | None = None,
                    sidecar: str | None = None, meta: dict | None = None, enqueue: bool = True) -> int:
    pid = db.run("INSERT INTO projects(user_id,title,source_type,source_url,platform,status,stage,video_path,sidecar_path,meta,prefs,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,'queued','queued',?,?,?,?,?,?)",
                 (uid, title, source_type, url, platform, video_path, sidecar, db.jd(meta or {}), db.jd(prefs), db.now(), db.now()))
    if enqueue:
        jobs.enqueue("process", project_id=pid)
    return pid


@authed
async def project_probe(request: Request):
    d = await body(request)
    if not probe_limiter.allow(f"p{request.state.uid}"):
        raise ApiError(429, "Slow down a little.")
    url, _ = _ok_url(d.get("url", ""))
    try:
        info = await run_in_threadpool(ingest.fetch_info, url)
    except ingest.IngestError as e:
        return J({"ok": False, "error": str(e), "needs_upload": e.needs_upload})
    info.pop("final_url", None)
    return J({"ok": True, **info})


@authed
async def project_create(request: Request):
    d = await body(request)
    _require_rights(d)
    url, platform = _ok_url(d.get("url", ""))
    if platform == "unknown":
        try:
            info = await run_in_threadpool(ingest.fetch_info, url)
            platform = info["platform"]
        except ingest.IngestError as e:
            raise ApiError(400, str(e))
    pid = _create_project(request.state.uid, "url", url, platform, editspec.clean_prefs(d.get("prefs") or {}), str(d.get("title", ""))[:200])
    return J(project_view(db.one("SELECT * FROM projects WHERE id=?", (pid,))), 201)


@authed
async def project_batch(request: Request):
    d = await body(request)
    _require_rights(d)
    urls = d.get("urls")
    if not isinstance(urls, list) or not urls or len(urls) > 25:
        raise ApiError(400, "Provide 1–25 URLs.")
    prefs = editspec.clean_prefs(d.get("prefs") or {})
    results, seen = [], set()
    for u in urls:
        try:
            url, platform = _ok_url(str(u))
            if platform == "unknown":
                raise ApiError(400, "Unsupported link (use YouTube, Twitch VOD, Kick or a direct video URL).")
            if url in seen:
                raise ApiError(400, "Duplicate in this batch.")
            seen.add(url)
            pid = _create_project(request.state.uid, "url", url, platform, prefs)
            results.append({"url": url, "ok": True, "project_id": pid})
        except ApiError as e:
            results.append({"url": str(u)[:200], "ok": False, "error": e.msg})
    return J({"results": results}, 201)


async def _save_upload(request: Request, form, pid_dir_name: str) -> tuple[str, str | None, dict]:
    up = form.get("file")
    if up is None or not hasattr(up, "filename"):
        raise ApiError(400, "Choose a video file to upload.")
    name = security.safe_filename(up.filename or "video")
    if Path(name).suffix.lower() not in ingest.VIDEO_EXTS:
        raise ApiError(400, "Unsupported file type. Use MP4, MOV, MKV or WebM.")
    dest = cfg.UPLOAD_DIR / pid_dir_name
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    cap = cfg.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(path, "wb") as fh:
        while chunk := await up.read(1024 * 1024):
            size += len(chunk)
            if size > cap:
                fh.close()
                shutil.rmtree(dest, ignore_errors=True)
                raise ApiError(413, f"File exceeds the {cfg.MAX_UPLOAD_MB} MB upload limit.")
            fh.write(chunk)
    try:
        info = await run_in_threadpool(ingest.validate_media, path)
    except ingest.IngestError as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise ApiError(400, str(e))
    sidecar = None
    sub = form.get("subtitles")
    if sub is not None and hasattr(sub, "filename") and sub.filename:
        if Path(sub.filename).suffix.lower() not in (".srt", ".vtt"):
            shutil.rmtree(dest, ignore_errors=True)
            raise ApiError(400, "Subtitles must be .srt or .vtt.")
        data = await sub.read()
        if len(data) > 5_000_000:
            shutil.rmtree(dest, ignore_errors=True)
            raise ApiError(413, "Subtitle file too large.")
        sp = dest / security.safe_filename(sub.filename)
        sp.write_bytes(data)
        sidecar = str(sp)
    return str(path), sidecar, info


@authed
async def project_upload(request: Request):
    if int(request.headers.get("content-length") or 0) > (cfg.MAX_UPLOAD_MB + 10) * 1024 * 1024:
        raise ApiError(413, f"File exceeds the {cfg.MAX_UPLOAD_MB} MB upload limit.")
    async with request.form(max_files=2, max_fields=10) as form:
        if str(form.get("rights_confirmed", "")).lower() != "true":
            raise ApiError(400, "Please confirm that you own this video or are authorised to use it.")
        try:
            prefs = editspec.clean_prefs(json.loads(form.get("prefs") or "{}"))
        except ValueError:
            raise ApiError(400, "Invalid preferences.")
        token = f"tmp{int(time.time() * 1000)}"
        path, sidecar, info = await _save_upload(request, form, token)
        title = str(form.get("title") or Path(path).stem)[:200]
    pid = _create_project(request.state.uid, "upload", None, "upload", prefs, title, path, sidecar, {"probe": info}, enqueue=False)
    # move out of the temp dir into the project dir, and only then let a worker see the project
    final = cfg.UPLOAD_DIR / f"p{pid}"
    shutil.move(str(cfg.UPLOAD_DIR / token), str(final))
    db.update("projects", pid, video_path=str(final / Path(path).name), sidecar_path=str(final / Path(sidecar).name) if sidecar else None)
    jobs.enqueue("process", project_id=pid)
    return J(project_view(db.one("SELECT * FROM projects WHERE id=?", (pid,))), 201)


@authed
async def project_attach_upload(request: Request):
    """Provide the file for a project whose URL could not be fetched (needs_upload)."""
    p = get_project(request, pid_of(request))
    if p["status"] not in ("needs_upload", "failed"):
        raise ApiError(409, "This project does not need a file.")
    async with request.form(max_files=2, max_fields=5) as form:
        path, sidecar, info = await _save_upload(request, form, f"p{p['id']}")
    meta = db.jl(p["meta"], {})
    meta.update(probe=info)
    meta.pop("source_removed", None)
    db.update("projects", p["id"], video_path=path, sidecar_path=sidecar, meta=db.jd(meta), status="queued", stage="queued", error=None,
              cancel_requested=0, updated_at=db.now())
    jobs.enqueue("process", project_id=p["id"])
    return J(project_view(db.one("SELECT * FROM projects WHERE id=?", (p["id"],))))


@authed
async def project_list(request: Request):
    rows = db.q("SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC LIMIT 200", (request.state.uid,))
    return J({"projects": [project_view(p) for p in rows]})


@authed
async def project_get(request: Request):
    p = get_project(request, pid_of(request))
    out = project_view(p)
    out["clips"] = [clip_view(c) for c in db.q("SELECT * FROM clips WHERE project_id=? ORDER BY idx, variant", (p["id"],))]
    return J(out)


@authed
async def project_events(request: Request):
    p = get_project(request, pid_of(request))
    after = int(request.query_params.get("after", 0) or 0)
    lvl = "" if request.query_params.get("debug") else "AND level!='debug'"
    rows = db.q(f"SELECT id,ts,level,tool,message FROM events WHERE project_id=? AND id>? {lvl} ORDER BY id LIMIT 500", (p["id"], after))
    return J({"events": rows, "status": p["status"], "stage": p["stage"], "progress": p["progress"], "error": p["error"]})


@authed
async def project_analysis(request: Request):
    p = get_project(request, pid_of(request))
    wd = cfg.WORK_DIR / f"p{p['id']}"
    out = {"signals": None, "candidates": []}
    for key, f in (("signals", "signals.json"), ("candidates", "candidates.json")):
        fp = wd / f
        if fp.exists():
            out[key] = db.jl(fp.read_text(), out[key])
    out["selected"] = [{"id": c["id"], "idx": c["idx"], "variant": c["variant"], "start": c["start"], "end": c["end"], "score": c["score"],
                        "score_detail": db.jl(c["score_detail"], {}), "reason": c["reason"], "kind": c["kind"], "status": c["status"]}
                       for c in db.q("SELECT * FROM clips WHERE project_id=? ORDER BY idx, variant", (p["id"],))]
    out["content_type"] = db.jl(p["meta"], {}).get("content_type")
    return J(out)


@authed
async def project_cancel(request: Request):
    p = get_project(request, pid_of(request))
    if p["status"] not in ("queued", "running"):
        raise ApiError(409, "Project is not running.")
    db.update("projects", p["id"], cancel_requested=1, updated_at=db.now())
    if p["status"] == "queued":
        db.run("UPDATE jobs SET status='cancelled', finished_at=? WHERE project_id=? AND kind='process' AND status='queued'", (db.now(), p["id"]))
        db.update("projects", p["id"], status="cancelled", stage="cancelled")
    return J({"ok": True})


@authed
async def project_retry(request: Request):
    p = get_project(request, pid_of(request))
    if p["status"] not in ("failed", "cancelled"):
        raise ApiError(409, "Only failed or cancelled projects can be retried.")
    if not p["video_path"] and p["source_type"] == "upload":
        raise ApiError(409, "The uploaded file is gone; upload it again.")
    db.update("projects", p["id"], status="queued", stage="queued", progress=0, error=None, cancel_requested=0, updated_at=db.now())
    jobs.enqueue("process", project_id=p["id"])
    return J({"ok": True})


@authed
async def project_delete(request: Request):
    p = get_project(request, pid_of(request))
    if p["status"] in ("queued", "running"):
        raise ApiError(409, "Cancel the project before deleting it.")
    cids = [c["id"] for c in db.q("SELECT id FROM clips WHERE project_id=?", (p["id"],))]
    for cid in cids:
        db.run("UPDATE publish_items SET status='cancelled', updated_at=? WHERE clip_id=? AND status IN ('pending_approval','queued','rate_limited')", (db.now(), cid))
    for base in (cfg.CLIP_DIR, cfg.WORK_DIR, cfg.SOURCE_DIR, cfg.UPLOAD_DIR):
        shutil.rmtree(base / f"p{p['id']}", ignore_errors=True)
    db.run("DELETE FROM clips WHERE project_id=?", (p["id"],))
    db.run("DELETE FROM events WHERE project_id=?", (p["id"],))
    db.run("DELETE FROM jobs WHERE project_id=?", (p["id"],))
    db.run("DELETE FROM projects WHERE id=?", (p["id"],))
    return J({"ok": True})


# ------------------------------------------------------------------ clips
@authed
async def clip_list(request: Request):
    q = request.query_params
    sql, args = "SELECT c.*, p.title AS ptitle FROM clips c JOIN projects p ON p.id=c.project_id WHERE c.user_id=?", [request.state.uid]
    if q.get("project"):
        sql += " AND c.project_id=?"
        args.append(int(q["project"]))
    if q.get("status"):
        sql += " AND c.status=?"
        args.append(q["status"])
    rows = db.q(sql + " ORDER BY c.updated_at DESC LIMIT 300", args)
    return J({"clips": [{**clip_view(c), "project_title": c["ptitle"]} for c in rows]})


@authed
async def clip_get(request: Request):
    c = get_clip(request, pid_of(request))
    out = clip_view(c)
    ed = out["edit"]
    words = db.jl(c["words"], [])
    out["caption_lines"] = captions.lines_for_editing(words, ed.get("captions", {})) if words else []
    out["project"] = {"id": c["project_id"], "title": db.one("SELECT title FROM projects WHERE id=?", (c["project_id"],))["title"]}
    return J(out)


@authed
async def clip_patch(request: Request):
    c = get_clip(request, pid_of(request))
    if c["status"] == "rendering":
        raise ApiError(409, "This clip is rendering; wait for it to finish.")
    d = await body(request)
    edit = db.jl(c["edit"], {})
    patch, applied, rejected = editspec.validate_edit(d.get("edit") or {}, request.state.uid)
    if rejected:
        raise ApiError(400, "; ".join(f"{k}: {v}" for k, v in rejected.items()))
    if patch:
        edit = producer.apply_patch(edit, patch)
        edit.pop("_safe", None)
    db.update("clips", c["id"], edit=db.jd(edit), updated_at=db.now())
    return J(clip_view(db.one("SELECT * FROM clips WHERE id=?", (c["id"],))))


@authed
async def clip_captions_put(request: Request):
    c = get_clip(request, pid_of(request))
    if c["status"] == "rendering":
        raise ApiError(409, "This clip is rendering; wait for it to finish.")
    d = await body(request)
    lines = d.get("lines")
    if not isinstance(lines, list) or len(lines) > 400:
        raise ApiError(400, "Invalid caption lines.")
    words = db.jl(c["words"], [])
    clean = []
    for ln in lines:
        try:
            i0, i1, text = int(ln["i0"]), int(ln["i1"]), " ".join(str(ln["text"]).split())[:200]
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "Invalid caption line.")
        if not (0 <= i0 <= i1 < len(words)) or not text or "{" in text or "}" in text or "\\" in text:
            raise ApiError(400, "Invalid caption line (empty, out of range, or contains control characters).")
        clean.append({"i0": i0, "i1": i1, "text": text})
    new_words = captions.apply_line_edits(words, clean)
    db.update("clips", c["id"], words=db.jd(new_words), updated_at=db.now())
    edit = db.jl(c["edit"], {})
    return J({"caption_lines": captions.lines_for_editing(new_words, edit.get("captions", {})), "stale": True})


@authed
async def clip_render(request: Request):
    c = get_clip(request, pid_of(request))
    d = await body(request) if request.headers.get("content-length", "0") != "0" else {}
    mode = "preview" if d.get("mode") == "preview" else "final"
    if c["status"] == "rendering":
        raise ApiError(409, "Already rendering.")
    if db.one("SELECT 1 AS x FROM jobs WHERE clip_id=? AND kind='render' AND status IN ('queued','running')", (c["id"],)):
        raise ApiError(409, "A render for this clip is already queued.")
    if mode == "final":
        db.update("clips", c["id"], status="rendering", error=None, updated_at=db.now())
    jid = jobs.enqueue("render", clip_id=c["id"], args={"mode": mode})
    return J({"job_id": jid, "mode": mode}, 202)


@authed
async def clip_variant(request: Request):
    """Create an alternative editing treatment of the same moment (variant B, then C...)."""
    c = get_clip(request, pid_of(request))
    proj = db.one("SELECT * FROM projects WHERE id=?", (c["project_id"],))
    existing = db.q("SELECT variant FROM clips WHERE project_id=? AND idx=?", (c["project_id"], c["idx"]))
    letters = "ABCDEFGH"
    nxt = next((l for l in letters if l not in {e["variant"] for e in existing}), None)
    if not nxt:
        raise ApiError(409, "Too many variants of this moment.")
    ct = db.jl(proj["meta"], {}).get("content_type", "other")
    from .agent import Agent
    prefs = Agent.__new__(Agent)
    prefs.uid, prefs.prefs_in = request.state.uid, db.jl(proj["prefs"], {})
    edit = producer.build_edit(Agent.prefs_for(prefs, ct), ct, c["kind"], "B")
    if not db.jl(c["words"], []):
        edit["captions"]["enabled"] = False
    cid = db.run("INSERT INTO clips(project_id,user_id,idx,variant,start,end,duration,score,score_detail,reason,kind,status,edit,words,metadata,created_at,updated_at)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (c["project_id"], c["user_id"], c["idx"], nxt, c["start"], c["end"], c["duration"], c["score"], c["score_detail"],
                  c["reason"] + f" (Variant {nxt}: alternative edit.)", c["kind"], "rendering", db.jd(edit), c["words"], c["metadata"], db.now(), db.now()))
    jobs.enqueue("render", clip_id=cid, args={"mode": "final"})
    return J({"clip_id": cid}, 201)


@authed
async def clip_delete(request: Request):
    c = get_clip(request, pid_of(request))
    if c["status"] == "rendering":
        raise ApiError(409, "This clip is rendering.")
    db.run("UPDATE publish_items SET status='cancelled', updated_at=? WHERE clip_id=? AND status IN ('pending_approval','queued','rate_limited')", (db.now(), c["id"]))
    for k in ("file_path", "preview_path", "thumb_path"):
        if c[k]:
            Path(c[k]).unlink(missing_ok=True)
    db.run("DELETE FROM clips WHERE id=?", (c["id"],))
    return J({"ok": True})


MEDIA_TYPES = {"video": ("file_path", "video/mp4"), "preview": ("preview_path", "video/mp4"), "thumb": ("thumb_path", "image/jpeg"),
               "download": ("file_path", "video/mp4"), "thumb_text": (None, "image/jpeg")}


@authed
async def clip_media(request: Request):
    c = get_clip(request, pid_of(request))
    kind = request.path_params["kind"]
    if kind not in MEDIA_TYPES:
        raise ApiError(404, "Unknown media.")
    col, ctype = MEDIA_TYPES[kind]
    path = Path(producer.project_dir(c["project_id"]) / f"c{c['id']}_tt.jpg") if kind == "thumb_text" else Path(c[col]) if c[col] else None
    if not path or not path.exists():
        raise ApiError(404, "Not available.")
    headers = {"Cache-Control": "private, max-age=30", "X-Content-Type-Options": "nosniff"}
    fname = f"clipforge_{c['project_id']}_{c['idx']}{c['variant']}.mp4"
    return FileResponse(path, media_type=ctype, headers=headers, filename=fname if kind == "download" else None,
                        content_disposition_type="attachment" if kind == "download" else "inline")


@authed
async def clip_metadata_patch(request: Request):
    c = get_clip(request, pid_of(request))
    d = await body(request)
    meta = db.jl(c["metadata"], {})
    if not meta.get("titles"):
        raise ApiError(409, "No metadata yet.")
    sel = meta.setdefault("selected", {})
    for k, mx in (("title", len(meta["titles"])), ("hook", len(meta.get("hooks", []))), ("thumbnail_text", len(meta.get("thumbnail_text", [])))):
        if k in d:
            try:
                i = int(d[k])
            except (TypeError, ValueError):
                raise ApiError(400, f"Invalid {k}.")
            if not 0 <= i < max(1, mx):
                raise ApiError(400, f"Invalid {k} index.")
            sel[k] = i
    if "custom_title" in d:
        sel["custom_title"] = " ".join(str(d["custom_title"]).replace("<", "").replace(">", "").split())[:100]
    if "custom_description" in d:
        sel["custom_description"] = str(d["custom_description"]).replace("<", "").replace(">", "")[:4500]
    if "custom_hashtags" in d:
        tags = d["custom_hashtags"]
        sel["custom_hashtags"] = None if tags is None else [re.sub(r"[^A-Za-z0-9_]", "", str(t))[:30] for t in tags[:15] if re.sub(r"[^A-Za-z0-9_]", "", str(t))]
    for k in ("titles", "hooks", "thumbnail_text"):  # editing the suggestion lists themselves
        if k in d and isinstance(d[k], list):
            meta[k] = [" ".join(str(x).replace("<", "").replace(">", "").split())[:100] for x in d[k] if str(x).strip()][:8] or meta[k]
    if "description" in d:
        meta["description"] = str(d["description"]).replace("<", "").replace(">", "")[:4500]
    if "hashtags" in d and isinstance(d["hashtags"], list):
        meta["hashtags"] = [re.sub(r"[^A-Za-z0-9_]", "", str(t))[:30] for t in d["hashtags"][:15] if re.sub(r"[^A-Za-z0-9_]", "", str(t))]
    db.update("clips", c["id"], metadata=db.jd(meta), updated_at=db.now())
    return J(meta)


@authed
async def clip_metadata_regen(request: Request):
    c = get_clip(request, pid_of(request))
    if c["status"] != "ready":
        raise ApiError(409, "Render the clip first.")
    proj = db.one("SELECT * FROM projects WHERE id=?", (c["project_id"],))
    ucfg = settings.effective(request.state.uid)
    text = " ".join(w["w"] for w in db.jl(c["words"], []))
    ct = db.jl(proj["meta"], {}).get("content_type", "other")
    meta = await run_in_threadpool(metadata.generate, LLM.for_user(ucfg), text, c["duration"], ct, c["kind"], proj["title"])
    db.update("clips", c["id"], metadata=db.jd(meta), updated_at=db.now())
    return J(meta)


@authed
async def clip_thumbnail(request: Request):
    c = get_clip(request, pid_of(request))
    if c["status"] != "ready" or not c["file_path"] or not Path(c["file_path"]).exists():
        raise ApiError(409, "Render the clip first.")
    d = await body(request)
    text = " ".join(str(d.get("text", "")).split())[:60]
    if not text:
        raise ApiError(400, "Enter thumbnail text.")
    t = max(0.0, min(float(d.get("t", c["duration"] * 0.35)), c["duration"] - 0.5))
    out = producer.project_dir(c["project_id"]) / f"c{c['id']}_tt.jpg"
    try:
        await run_in_threadpool(editor.thumbnail_with_text, Path(c["file_path"]), out, text, t)
    except editor.RenderError as e:
        raise ApiError(500, f"Could not create thumbnail: {e}")
    return J({"ok": True, "v": int(time.time())})


# ------------------------------------------------------------------ assets
@authed
async def assets_list(request: Request):
    return J({"assets": assets.list_assets(request.state.uid)})


@authed
async def assets_upload(request: Request):
    async with request.form(max_files=1, max_fields=5) as form:
        kind = form.get("kind")
        up = form.get("file")
        if kind not in ("music", "sfx") or up is None or not hasattr(up, "filename"):
            raise ApiError(400, "Provide kind (music|sfx) and a file.")
        if form.get("license_confirmed") != "true":
            raise ApiError(400, "Confirm that you have the rights to use this audio.")
        ext = Path(up.filename or "").suffix.lower()
        if ext not in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
            raise ApiError(400, "Unsupported audio type (mp3, wav, m4a, aac, ogg, flac).")
        user_dir = cfg.ASSET_DIR / f"u{request.state.uid}"
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{int(time.time() * 1000)}{ext}"
        size, cap = 0, cfg.MAX_ASSET_MB * 1024 * 1024
        with open(path, "wb") as fh:
            while chunk := await up.read(1024 * 1024):
                size += len(chunk)
                if size > cap:
                    fh.close()
                    path.unlink(missing_ok=True)
                    raise ApiError(413, f"Audio exceeds {cfg.MAX_ASSET_MB} MB.")
                fh.write(chunk)
        try:
            info = await run_in_threadpool(ingest.probe, path)
            ok = info["acodec"] is not None or info["duration"] > 0
        except ingest.IngestError:
            ok = False
        if not ok:
            path.unlink(missing_ok=True)
            raise ApiError(400, "That file is not readable audio.")
        name = " ".join(str(form.get("name") or Path(up.filename).stem).split())[:60]
    aid = db.run("INSERT INTO assets(user_id,kind,name,path,builtin,created_at) VALUES(?,?,?,?,0,?)", (request.state.uid, kind, name, str(path), db.now()))
    return J({"id": aid, "kind": kind, "name": name}, 201)


@authed
async def assets_delete(request: Request):
    a = db.one("SELECT * FROM assets WHERE id=? AND user_id=? AND builtin=0", (pid_of(request), request.state.uid))
    if not a:
        raise ApiError(404, "Asset not found.")
    Path(a["path"]).unlink(missing_ok=True)
    db.run("DELETE FROM assets WHERE id=?", (a["id"],))
    return J({"ok": True})


@authed
async def assets_audio(request: Request):
    a = assets.get_asset(pid_of(request), request.state.uid)
    if not a or not Path(a["path"]).exists():
        raise ApiError(404, "Asset not found.")
    return FileResponse(a["path"], media_type="audio/mp4" if a["path"].endswith(".m4a") else None)


# ------------------------------------------------------------------ settings
URL_KEYS = ("anthropic_base_url", "transcribe_base_url")
SECRETS = ("anthropic_api_key", "transcribe_api_key", "google_client_secret")


@authed
async def settings_get(request: Request):
    uid = request.state.uid
    out = settings.public_view(uid)
    out["youtube_redirect_uri"] = youtube.redirect_uri()
    out["env_defaults"] = {"anthropic_model": cfg.ANTHROPIC_MODEL, "transcribe_model": cfg.TRANSCRIBE_MODEL}
    return J(out)


@authed
async def settings_put(request: Request):
    uid = request.state.uid
    d = await body(request)
    for k in SECRETS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            settings.set_value(uid, k, v.strip()[:500])
    for k in d.get("clear", []) if isinstance(d.get("clear"), list) else []:
        if k in SECRETS:
            settings.set_value(uid, k, "")
    for k in URL_KEYS:
        if k in d:
            v = str(d[k] or "").strip()
            if v:
                err = security.check_url(v)
                if err:
                    raise ApiError(400, f"{k}: {err}")
            settings.set_value(uid, k, v.rstrip("/"))
    for k in ("anthropic_model", "transcribe_model", "google_client_id"):
        if k in d:
            settings.set_value(uid, k, re.sub(r"[^\w.\-:/@ ]", "", str(d[k] or ""))[:200])
    if d.get("transcribe_engine") in ("auto", "faster_whisper", "openai_whisper", "api"):
        settings.set_value(uid, "transcribe_engine", d["transcribe_engine"])
    for k in ("auto_publish", "llm_enabled", "append_shorts_tag"):
        if isinstance(d.get(k), bool):
            settings.set_value(uid, k, "true" if d[k] else "false")
    if d.get("default_privacy") in ("private", "unlisted", "public"):
        settings.set_value(uid, "default_privacy", d["default_privacy"])
    for k, lo, hi in (("max_publish_per_day", 1, 50), ("source_retention_days", 0, 365), ("storage_max_gb", 1, 10000)):
        if k in d:
            try:
                settings.set_value(uid, k, str(max(lo, min(hi, int(float(d[k]))))))
            except (TypeError, ValueError):
                raise ApiError(400, f"Invalid {k}.")
    if "default_prefs" in d:
        settings.set_value(uid, "default_prefs", json.dumps(editspec.clean_prefs(d["default_prefs"])))
    return await settings_get(request)


@authed
async def settings_test_llm(request: Request):
    llm = LLM.for_user(settings.effective(request.state.uid))
    if not llm.available:
        return J({"ok": False, "error": "No API key configured."})
    try:
        r = await run_in_threadpool(llm.messages, "Reply with the single word OK.", [{"role": "user", "content": "ping"}], 10, None, None, 1)
        return J({"ok": True, "model": llm.model, "reply": LLM.text_of(r)[:40]})
    except LLMError as e:
        return J({"ok": False, "error": str(e)})


@authed
async def storage_get(request: Request):
    return J(await run_in_threadpool(jobs.storage_usage))


@authed
async def storage_cleanup(request: Request):
    d = await body(request) if request.headers.get("content-length", "0") != "0" else {}
    uid_projects = {p["id"] for p in db.q("SELECT id FROM projects WHERE user_id=?", (request.state.uid,))}
    r = await run_in_threadpool(jobs.cleanup, 0 if d.get("all") else None, bool(d.get("all")))
    return J({**r, "projects": len(uid_projects)})


# ------------------------------------------------------------------ YouTube + publishing
@authed
async def yt_status(request: Request):
    return J(youtube.status(request.state.uid))


@authed
async def yt_connect(request: Request):
    try:
        return J({"url": youtube.auth_url(request.state.uid)})
    except youtube.YouTubeError as e:
        raise ApiError(400, str(e))


async def yt_callback(request: Request):
    q = request.query_params
    if q.get("error"):
        return RedirectResponse(f"/#/publishing?yt_error={q['error'][:80]}")
    try:
        await run_in_threadpool(youtube.handle_callback, q.get("code", ""), q.get("state", ""))
    except youtube.YouTubeError as e:
        from urllib.parse import quote
        return RedirectResponse(f"/#/publishing?yt_error={quote(str(e)[:200])}")
    return RedirectResponse("/#/publishing?yt=connected")


@authed
async def yt_disconnect(request: Request):
    await run_in_threadpool(youtube.disconnect, request.state.uid)
    return J({"ok": True})


def item_view(i: dict) -> dict:
    c = db.one("SELECT id, idx, variant, project_id, duration, status, thumb_path FROM clips WHERE id=?", (i["clip_id"],))
    p = db.one("SELECT title FROM projects WHERE id=?", (c["project_id"],)) if c else None
    return {**{k: i[k] for k in ("id", "clip_id", "title", "description", "privacy", "schedule_at", "status", "youtube_id", "error", "attempts", "created_at", "updated_at", "published_at")},
            "tags": db.jl(i["tags"], []), "clip": ({"idx": c["idx"], "variant": c["variant"], "project_id": c["project_id"], "duration": c["duration"],
                                                    "project_title": p["title"] if p else "", "has_thumb": bool(c["thumb_path"])} if c else None),
            "url": f"https://www.youtube.com/watch?v={i['youtube_id']}" if i["youtube_id"] else None}


@authed
async def publish_list(request: Request):
    uid = request.state.uid
    rows = db.q("SELECT * FROM publish_items WHERE user_id=? ORDER BY id DESC LIMIT 300", (uid,))
    return J({"items": [item_view(i) for i in rows], "status": youtube.status(uid), "auto_publish": settings.effective(uid)["auto_publish"]})


def _parse_schedule(v):
    if v in (None, "", 0):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        raise ApiError(400, "Invalid schedule time.")
    if t < time.time() - 60:
        raise ApiError(400, "Schedule time is in the past.")
    if t > time.time() + 180 * 86400:
        raise ApiError(400, "YouTube can schedule at most ~6 months ahead.")
    return t


def _item_fields(d: dict) -> dict:
    out = {}
    if "title" in d:
        t = " ".join(str(d["title"]).replace("<", "").replace(">", "").split())[:100]
        if not t:
            raise ApiError(400, "Title is required.")
        out["title"] = t
    if "description" in d:
        out["description"] = str(d["description"]).replace("<", "").replace(">", "")[:4500]
    if "tags" in d and isinstance(d["tags"], list):
        out["tags"] = db.jd([re.sub(r"[^A-Za-z0-9_]", "", str(t))[:30] for t in d["tags"][:15] if re.sub(r"[^A-Za-z0-9_]", "", str(t))])
    if "privacy" in d:
        if d["privacy"] not in ("private", "unlisted", "public"):
            raise ApiError(400, "Invalid privacy setting.")
        out["privacy"] = d["privacy"]
    if "schedule_at" in d:
        out["schedule_at"] = _parse_schedule(d["schedule_at"])
    return out


@authed
async def publish_create(request: Request):
    uid = request.state.uid
    d = await body(request)
    try:
        c = get_clip(request, int(d.get("clip_id")))
    except (TypeError, ValueError):
        raise ApiError(400, "clip_id required.")
    if c["status"] != "ready":
        raise ApiError(409, "Only clips that passed quality control can be published.")
    if db.one("SELECT 1 AS x FROM publish_items WHERE clip_id=? AND status IN ('pending_approval','queued','uploading','rate_limited')", (c["id"],)):
        raise ApiError(409, "This clip is already in the publishing queue.")
    f = _item_fields(d)
    approve = bool(d.get("approve"))
    if approve and not youtube.connected(uid):
        raise ApiError(400, "Connect YouTube before approving uploads.")
    iid = youtube.queue_clip(uid, c, approved=approve, **f)
    return J(item_view(db.one("SELECT * FROM publish_items WHERE id=?", (iid,))), 201)


def _item(request: Request) -> dict:
    i = db.one("SELECT * FROM publish_items WHERE id=? AND user_id=?", (pid_of(request), request.state.uid))
    if not i:
        raise ApiError(404, "Queue item not found.")
    return i


@authed
async def publish_patch(request: Request):
    i = _item(request)
    if i["status"] not in ("pending_approval", "queued", "failed", "rate_limited"):
        raise ApiError(409, "This item can no longer be edited.")
    f = _item_fields(await body(request))
    if f:
        db.update("publish_items", i["id"], updated_at=db.now(), **f)
    return J(item_view(db.one("SELECT * FROM publish_items WHERE id=?", (i["id"],))))


@authed
async def publish_approve(request: Request):
    i = _item(request)
    if i["status"] != "pending_approval":
        raise ApiError(409, "Only items awaiting approval can be approved.")
    if not youtube.connected(request.state.uid):
        raise ApiError(400, "Connect YouTube before approving uploads.")
    if i["schedule_at"] and i["schedule_at"] < time.time():
        db.update("publish_items", i["id"], schedule_at=None)
    db.update("publish_items", i["id"], status="queued", attempts=0, updated_at=db.now())
    return J(item_view(db.one("SELECT * FROM publish_items WHERE id=?", (i["id"],))))


@authed
async def publish_cancel(request: Request):
    i = _item(request)
    if i["status"] not in ("pending_approval", "queued", "rate_limited", "failed"):
        raise ApiError(409, "Cannot cancel an item in this state.")
    db.update("publish_items", i["id"], status="cancelled", updated_at=db.now())
    return J({"ok": True})


@authed
async def publish_retry(request: Request):
    i = _item(request)
    if i["status"] not in ("failed", "rate_limited"):
        raise ApiError(409, "Only failed or rate-limited items can be retried.")
    db.update("publish_items", i["id"], status="queued", attempts=0, error=None, updated_at=db.now())
    return J(item_view(db.one("SELECT * FROM publish_items WHERE id=?", (i["id"],))))


# ------------------------------------------------------------------ app
class Guard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-cf-csrf") != "1":
                return J({"error": "Missing CSRF header."}, 403)
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                             "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                                                        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        return resp


async def on_api_error(request: Request, exc: ApiError):
    return J({"error": exc.msg}, exc.status)


async def on_http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return J({"error": exc.detail if exc.status_code != 404 else "Not found."}, exc.status_code)
    return Response(str(exc.detail), status_code=exc.status_code)


async def on_unexpected(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return J({"error": "Internal error. Check the server log."}, 500)


async def index(request: Request):
    return FileResponse(cfg.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})


R = lambda path, fn, methods: Route(path, fn, methods=methods)
routes = [
    R("/", index, ["GET"]),
    R("/api/auth/me", auth_me, ["GET"]), R("/api/auth/signup", auth_signup, ["POST"]), R("/api/auth/login", auth_login, ["POST"]),
    R("/api/auth/logout", auth_logout, ["POST"]),
    R("/api/meta", meta_view, ["GET"]), R("/api/dashboard", dashboard, ["GET"]),
    R("/api/projects", project_list, ["GET"]), R("/api/projects", project_create, ["POST"]),
    R("/api/projects/probe", project_probe, ["POST"]), R("/api/projects/batch", project_batch, ["POST"]),
    R("/api/projects/upload", project_upload, ["POST"]),
    R("/api/projects/{id:int}", project_get, ["GET"]), R("/api/projects/{id:int}", project_delete, ["DELETE"]),
    R("/api/projects/{id:int}/events", project_events, ["GET"]), R("/api/projects/{id:int}/analysis", project_analysis, ["GET"]),
    R("/api/projects/{id:int}/cancel", project_cancel, ["POST"]), R("/api/projects/{id:int}/retry", project_retry, ["POST"]),
    R("/api/projects/{id:int}/upload", project_attach_upload, ["POST"]),
    R("/api/clips", clip_list, ["GET"]), R("/api/clips/{id:int}", clip_get, ["GET"]), R("/api/clips/{id:int}", clip_patch, ["PATCH"]), R("/api/clips/{id:int}", clip_delete, ["DELETE"]),
    R("/api/clips/{id:int}/captions", clip_captions_put, ["PUT"]), R("/api/clips/{id:int}/render", clip_render, ["POST"]),
    R("/api/clips/{id:int}/variant", clip_variant, ["POST"]), R("/api/clips/{id:int}/media/{kind}", clip_media, ["GET"]),
    R("/api/clips/{id:int}/metadata", clip_metadata_patch, ["PATCH"]), R("/api/clips/{id:int}/metadata/regenerate", clip_metadata_regen, ["POST"]),
    R("/api/clips/{id:int}/thumbnail", clip_thumbnail, ["POST"]),
    R("/api/assets", assets_list, ["GET"]), R("/api/assets", assets_upload, ["POST"]),
    R("/api/assets/{id:int}", assets_delete, ["DELETE"]), R("/api/assets/{id:int}/audio", assets_audio, ["GET"]),
    R("/api/settings", settings_get, ["GET"]), R("/api/settings", settings_put, ["PUT"]), R("/api/settings/test-llm", settings_test_llm, ["POST"]),
    R("/api/storage", storage_get, ["GET"]), R("/api/storage/cleanup", storage_cleanup, ["POST"]),
    R("/api/youtube/status", yt_status, ["GET"]), R("/api/youtube/connect", yt_connect, ["GET"]), R("/api/youtube/callback", yt_callback, ["GET"]),
    R("/api/youtube/disconnect", yt_disconnect, ["POST"]),
    R("/api/publish", publish_list, ["GET"]), R("/api/publish", publish_create, ["POST"]),
    R("/api/publish/{id:int}", publish_patch, ["PATCH"]), R("/api/publish/{id:int}/approve", publish_approve, ["POST"]),
    R("/api/publish/{id:int}/cancel", publish_cancel, ["POST"]), R("/api/publish/{id:int}/retry", publish_retry, ["POST"]),
    Mount("/static", StaticFiles(directory=str(cfg.FRONTEND_DIR), check_dir=False), name="static"),
]


@asynccontextmanager
async def lifespan(app):
    db.init()
    # synthesizing the built-in music/SFX takes ~10s on first run; don't block startup on it
    threading.Thread(target=assets.ensure_builtin, name="asset-init", daemon=True).start()
    jobs.start()
    yield
    jobs.stop()


app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(Guard)],
                exception_handlers={ApiError: on_api_error, HTTPException: on_http_error, Exception: on_unexpected})
