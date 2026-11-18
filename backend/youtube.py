"""Official YouTube Data API v3 integration (Google OAuth 2.0 web-server flow + resumable uploads).

Policy decisions (deliberate):
  * Human approval is the default. Items start as `pending_approval`; only Approve (or the opt-in auto-publish setting) moves them on.
  * Quota is tracked locally (videos.insert = 1600 units of the default 10,000/day, resetting at midnight Pacific).
    We stop before exceeding it and never retry in a loop; a Google `quotaExceeded` answer parks the item as `rate_limited`.
  * Scheduling uses YouTube's own `status.publishAt` (video uploaded private, Google flips it public at that time), so it
    keeps working even if this server is offline at the scheduled moment.
  * Note for operators: videos uploaded through an API project that has not passed Google's API compliance audit are
    locked to private by YouTube. That is Google's rule; we do not work around it.

Status lifecycle: pending_approval -> queued -> uploading -> published | scheduled | failed | rate_limited | cancelled
"""
import json
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import db, settings
from .config import cfg
from .security import check_url  # noqa: F401  (re-exported for symmetry; YouTube URLs are fixed, not user supplied)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
MAX_ATTEMPTS = 4
YT_CATEGORY = {"Gaming": "20", "Education": "27", "Comedy": "23", "People & Blogs": "22", "Entertainment": "24",
               "Science & Technology": "28", "News & Politics": "25", "Howto & Style": "26", "Music": "10"}


class YouTubeError(Exception):
    def __init__(self, msg: str, kind: str = "failed"):
        super().__init__(msg)
        self.kind = kind  # failed | rate_limited | transient | auth


# ------------------------------------------------------------------ tokens / OAuth
def redirect_uri() -> str:
    return f"{cfg.BASE_URL}/api/youtube/callback"


def _tokens(uid: int) -> dict | None:
    raw = settings.get(uid, "youtube_tokens")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save_tokens(uid: int, t: dict) -> None:
    settings.set_value(uid, "youtube_tokens", json.dumps(t))


def connected(uid: int) -> bool:
    t = _tokens(uid)
    return bool(t and t.get("refresh_token"))


def status(uid: int) -> dict:
    ucfg = settings.effective(uid)
    t = _tokens(uid)
    return {"configured": bool(ucfg["google_client_id"] and ucfg["google_client_secret"]),
            "connected": bool(t and t.get("refresh_token")), "channel": (t or {}).get("channel"),
            "redirect_uri": redirect_uri(), "quota": quota(uid)}


def auth_url(uid: int) -> str:
    ucfg = settings.effective(uid)
    if not (ucfg["google_client_id"] and ucfg["google_client_secret"]):
        raise YouTubeError("Add your Google OAuth client ID and secret in Settings first.")
    state = secrets.token_urlsafe(24)
    db.run("DELETE FROM oauth_states WHERE created_at<?", (db.now() - 3600,))
    db.run("INSERT INTO oauth_states(state,user_id,created_at) VALUES(?,?,?)", (state, uid, db.now()))
    q = {"client_id": ucfg["google_client_id"], "redirect_uri": redirect_uri(), "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "state": state, "include_granted_scopes": "true"}
    return f"{cfg.GOOGLE_AUTH_URL}?{urlencode(q)}"


def _token_request(ucfg: dict, data: dict) -> dict:
    data = {**data, "client_id": ucfg["google_client_id"], "client_secret": ucfg["google_client_secret"]}
    try:
        r = requests.post(cfg.GOOGLE_TOKEN_URL, data=data, timeout=30)
    except requests.RequestException as e:
        raise YouTubeError(f"Could not reach Google: {e}", "transient")
    if r.status_code != 200:
        try:
            err = r.json().get("error_description") or r.json().get("error")
        except ValueError:
            err = r.text[:200]
        raise YouTubeError(f"Google rejected the token request: {err}", "auth")
    return r.json()


def handle_callback(code: str, state: str) -> int:
    row = db.one("SELECT * FROM oauth_states WHERE state=?", (state,))
    if not row or db.now() - row["created_at"] > 900:
        raise YouTubeError("Invalid or expired sign-in attempt. Start again from Publishing.")
    db.run("DELETE FROM oauth_states WHERE state=?", (state,))
    uid = row["user_id"]
    ucfg = settings.effective(uid)
    tok = _token_request(ucfg, {"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    if not tok.get("refresh_token"):
        old = _tokens(uid)
        if not old or not old.get("refresh_token"):
            raise YouTubeError("Google did not return a refresh token. Remove ClipForge from your Google account's connected apps and try again.")
        tok["refresh_token"] = old["refresh_token"]
    t = {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
         "expires_at": time.time() + int(tok.get("expires_in", 3600)), "scope": tok.get("scope", ""), "channel": None}
    _save_tokens(uid, t)
    t["channel"] = fetch_channel(uid)
    _save_tokens(uid, t)
    return uid


def _access_token(uid: int, force: bool = False) -> str:
    t = _tokens(uid)
    if not t or not t.get("refresh_token"):
        raise YouTubeError("YouTube is not connected.", "auth")
    if force or time.time() > t.get("expires_at", 0) - 60:
        tok = _token_request(settings.effective(uid), {"refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
        t["access_token"] = tok["access_token"]
        t["expires_at"] = time.time() + int(tok.get("expires_in", 3600))
        _save_tokens(uid, t)
    return t["access_token"]


def _auth_headers(uid: int, force: bool = False) -> dict:
    return {"Authorization": f"Bearer {_access_token(uid, force)}"}


def _req(uid: int, method: str, url: str, **kw) -> requests.Response:
    """Authenticated request; refreshes the token once on 401."""
    extra, timeout = kw.pop("headers", {}), kw.pop("timeout", 60)
    for attempt in range(2):
        h = {**_auth_headers(uid, force=attempt == 1), **extra}
        try:
            r = requests.request(method, url, headers=h, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise YouTubeError(f"Network error talking to YouTube: {e}", "transient")
        if r.status_code == 401 and attempt == 0:
            continue
        return r
    return r


def fetch_channel(uid: int) -> dict | None:
    r = _req(uid, "GET", f"{cfg.YT_API_BASE}/channels", params={"part": "snippet", "mine": "true"})
    if r.status_code != 200:
        raise YouTubeError(f"Connected to Google, but the channel lookup failed (HTTP {r.status_code}). "
                           "Make sure the account has a YouTube channel and the YouTube Data API v3 is enabled.")
    items = r.json().get("items") or []
    if not items:
        raise YouTubeError("This Google account has no YouTube channel.")
    sn = items[0]["snippet"]
    return {"id": items[0]["id"], "title": sn.get("title"), "thumbnail": ((sn.get("thumbnails") or {}).get("default") or {}).get("url")}


def disconnect(uid: int) -> None:
    t = _tokens(uid)
    if t and t.get("refresh_token"):
        try:  # best effort: tell Google to revoke the grant
            requests.post(cfg.GOOGLE_TOKEN_URL.rsplit("/", 1)[0] + "/revoke", data={"token": t["refresh_token"]}, timeout=10)
        except requests.RequestException:
            pass
    settings.set_value(uid, "youtube_tokens", "")


# ------------------------------------------------------------------ quota
def _pacific_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def quota(uid: int) -> dict:
    row = db.one("SELECT units FROM quota_usage WHERE user_id=? AND day=?", (uid, _pacific_day()))
    used = row["units"] if row else 0
    return {"used": used, "limit": cfg.YT_DAILY_QUOTA, "uploads_left": max(0, (cfg.YT_DAILY_QUOTA - used) // cfg.YT_UPLOAD_COST)}


def _add_quota(uid: int, units: int) -> None:
    db.run("INSERT INTO quota_usage(user_id,day,units) VALUES(?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET units=units+excluded.units",
           (uid, _pacific_day(), units))


# ------------------------------------------------------------------ publish items
def _clean(s: str, limit: int) -> str:
    return " ".join(str(s).replace("<", "").replace(">", "").split())[:limit]


def fields_from_clip(clip: dict, ucfg: dict) -> dict:
    """Title/description/tags from the user's selections in Content Studio (falls back to the first suggestion)."""
    m = db.jl(clip["metadata"], {})
    sel = m.get("selected") or {}
    titles = m.get("titles") or [f"Clip {clip['idx']}"]
    title = sel.get("custom_title") or titles[min(int(sel.get("title", 0) or 0), len(titles) - 1)]
    tags = sel.get("custom_hashtags") if sel.get("custom_hashtags") is not None else m.get("hashtags", [])
    desc = sel.get("custom_description") or m.get("description", "")
    return {"title": _clean(title, 100) or f"Clip {clip['idx']}", "description": desc[:4500], "tags": [t for t in tags if t][:15],
            "privacy": ucfg["default_privacy"]}


def build_description(description: str, tags: list[str], append_shorts: bool) -> str:
    tagline = " ".join("#" + t.lstrip("#") for t in tags[:12])
    if append_shorts and "#shorts" not in tagline.lower():
        tagline = (tagline + " #Shorts").strip()
    return _clean_multiline(f"{description}\n\n{tagline}".strip())[:4900]


def _clean_multiline(s: str) -> str:
    return s.replace("<", "").replace(">", "")


def queue_clip(uid: int, clip: dict, approved: bool = False, **over) -> int:
    ucfg = settings.effective(uid)
    f = {**fields_from_clip(clip, ucfg), **{k: v for k, v in over.items() if v is not None}}
    if f["privacy"] not in ("private", "unlisted", "public"):
        f["privacy"] = "private"
    return db.run("INSERT INTO publish_items(user_id,clip_id,title,description,tags,privacy,schedule_at,status,created_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (uid, clip["id"], _clean(f["title"], 100), f["description"], db.jd(f["tags"]), f["privacy"], over.get("schedule_at"),
                   "queued" if approved else "pending_approval", db.now(), db.now()))


# ------------------------------------------------------------------ upload
def _api_error(r: requests.Response) -> YouTubeError:
    try:
        err = r.json().get("error", {})
        reasons = [e.get("reason", "") for e in err.get("errors", [])]
        msg = err.get("message", r.text[:200])
    except (ValueError, AttributeError):
        reasons, msg = [], r.text[:200]
    if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons or "rateLimitExceeded" in reasons:
        return YouTubeError("YouTube API quota exceeded for today. It will retry automatically after the quota resets (midnight Pacific).", "rate_limited")
    if "uploadLimitExceeded" in reasons:
        return YouTubeError("YouTube's daily upload limit for this channel was reached. Try again tomorrow.", "rate_limited")
    if r.status_code == 401:
        return YouTubeError("Google rejected the credentials; reconnect YouTube in Publishing.", "auth")
    if r.status_code >= 500 or r.status_code == 429:
        return YouTubeError(f"YouTube returned HTTP {r.status_code}: {msg}", "transient")
    return YouTubeError(f"YouTube rejected the upload (HTTP {r.status_code}): {msg}", "failed")


def _upload_file(uid: int, path: Path, body: dict, progress=lambda f: None) -> dict:
    size = path.stat().st_size
    init = _req(uid, "POST", f"{cfg.YT_UPLOAD_BASE}/videos", params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4", "Content-Type": "application/json; charset=UTF-8"},
                data=json.dumps(body))
    if init.status_code != 200 or not init.headers.get("Location"):
        raise _api_error(init)
    session = init.headers["Location"]
    sent, fails = 0, 0
    with open(path, "rb") as fh:
        while sent < size:
            fh.seek(sent)
            chunk = fh.read(CHUNK)
            end = sent + len(chunk) - 1
            try:
                r = requests.put(session, data=chunk, timeout=300, headers={"Authorization": _auth_headers(uid)["Authorization"],
                                 "Content-Length": str(len(chunk)), "Content-Range": f"bytes {sent}-{end}/{size}", "Content-Type": "video/mp4"})
            except requests.RequestException:
                r = None
            if r is not None and r.status_code in (200, 201):
                progress(1.0)
                return r.json()
            if r is not None and r.status_code == 308:
                rng = r.headers.get("Range", "")
                sent = int(rng.split("-")[1]) + 1 if "-" in rng else sent + len(chunk)
                progress(sent / size)
                fails = 0
                continue
            if r is not None and r.status_code < 500 and r.status_code != 429:
                raise _api_error(r)
            fails += 1
            if fails > 5:
                raise YouTubeError("Upload kept failing (network or server errors).", "transient")
            time.sleep(min(2 ** fails, 20))
            try:  # ask the server how much it really has, then resume from there
                q = requests.put(session, timeout=60, headers={**_auth_headers(uid), "Content-Length": "0", "Content-Range": f"bytes */{size}"})
                if q.status_code in (200, 201):
                    return q.json()
                if q.status_code == 308:
                    rng = q.headers.get("Range", "")
                    sent = int(rng.split("-")[1]) + 1 if "-" in rng else 0
            except requests.RequestException:
                pass
    raise YouTubeError("Upload finished without a response from YouTube.", "transient")


def upload_item(item_id: int, log=lambda m: None) -> None:
    item = db.one("SELECT * FROM publish_items WHERE id=?", (item_id,))
    uid = item["user_id"]
    ucfg = settings.effective(uid)
    clip = db.one("SELECT * FROM clips WHERE id=?", (item["clip_id"],))
    if not clip or clip["status"] != "ready" or not clip["file_path"] or not Path(clip["file_path"]).exists():
        db.update("publish_items", item_id, status="failed", error="The clip file is not available (clip missing, not rendered or cleaned up).", updated_at=db.now())
        return
    q = quota(uid)
    if q["uploads_left"] < 1:
        db.update("publish_items", item_id, status="rate_limited", error=f"Local quota guard: {q['used']}/{q['limit']} units used today.", updated_at=db.now())
        return
    sched = item["schedule_at"]
    status_body = {"privacyStatus": item["privacy"], "selfDeclaredMadeForKids": False}
    if sched and sched > time.time() + 300:
        status_body = {"privacyStatus": "private", "publishAt": datetime.fromtimestamp(sched, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "selfDeclaredMadeForKids": False}
    meta = db.jl(clip["metadata"], {})
    tags = db.jl(item["tags"], [])
    body = {"snippet": {"title": item["title"], "description": build_description(item["description"], tags, ucfg["append_shorts_tag"]),
                        "tags": [t for t in tags][:15], "categoryId": YT_CATEGORY.get(meta.get("category", ""), "22")},
            "status": status_body}
    db.update("publish_items", item_id, status="uploading", attempts=item["attempts"] + 1, error=None, updated_at=db.now())
    try:
        res = _upload_file(uid, Path(clip["file_path"]), body, lambda f: None)
    except YouTubeError as e:
        log(f"Upload {item_id} failed: {e}")
        if e.kind == "rate_limited":
            db.update("publish_items", item_id, status="rate_limited", error=str(e), updated_at=db.now())
        elif e.kind in ("transient",) and item["attempts"] + 1 < MAX_ATTEMPTS:
            db.update("publish_items", item_id, status="queued", error=f"Retrying after error: {e}", updated_at=db.now())
        else:
            db.update("publish_items", item_id, status="failed", error=str(e), updated_at=db.now())
        return
    _add_quota(uid, cfg.YT_UPLOAD_COST)
    scheduled = "publishAt" in status_body
    db.update("publish_items", item_id, status="scheduled" if scheduled else "published", youtube_id=res.get("id"), error=None,
              published_at=sched if scheduled else db.now(), updated_at=db.now())
    log(f"Uploaded publish item {item_id} as YouTube video {res.get('id')}")


def process_due(log=lambda m: None) -> int:
    """Called by the background scheduler loop. Returns the number of items handled."""
    now = db.now()
    rows = db.q(
        "SELECT id FROM publish_items WHERE (status='queued' AND (attempts=0 OR updated_at < ? - 60*(1<<attempts)))"
        " OR (status='rate_limited' AND updated_at < ?) ORDER BY id LIMIT 5", (now, now - 3600))
    # items stuck in 'uploading' after a crash go back to the queue
    db.run("UPDATE publish_items SET status='queued' WHERE status='uploading' AND updated_at<?", (now - 3600,))
    n = 0
    for r in rows:
        try:
            if connected(db.one("SELECT user_id FROM publish_items WHERE id=?", (r["id"],))["user_id"]):
                upload_item(r["id"], log)
            else:
                db.update("publish_items", r["id"], status="failed", error="YouTube is not connected.", updated_at=db.now())
            n += 1
        except YouTubeError as e:
            db.update("publish_items", r["id"], status="failed", error=str(e), updated_at=db.now())
        except Exception as e:  # never let one item kill the scheduler
            db.update("publish_items", r["id"], status="failed", error=f"Unexpected error: {e}", updated_at=db.now())
    return n
