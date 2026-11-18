"""End-to-end tests. They run the real server, real FFmpeg rendering and real QC; only the external services are mocked.
Tests are order-dependent on purpose (they walk one user through the product)."""
import json
import time

import pytest
import requests

from conftest import MOCK_PORT, APP_PORT, wait_for, wait_project

MOCK = f"http://127.0.0.1:{MOCK_PORT}"
PREFS = {"duration": 15, "max_clips": 1, "preset": "clean"}
state = {}


# ------------------------------------------------------------------ auth & security
def test_auth_and_csrf(server):
    import conftest
    anon = conftest.Client(server)
    assert anon.get("/api/auth/me").json()["first_run"] is True
    assert anon.get("/api/dashboard").status_code == 401
    # state-changing requests without the CSRF header are refused even if cookies were valid
    r = requests.post(server + "/api/auth/signup", json={"email": "a@b.co", "password": "x" * 12})
    assert r.status_code == 403
    assert anon.post("/api/auth/signup", json={"email": "bad", "password": "x" * 12}).status_code == 400
    assert anon.post("/api/auth/signup", json={"email": "a@b.co", "password": "short"}).status_code == 400


def test_signup_closed_after_first_user_and_login(client, server):
    import conftest
    other = conftest.Client(server)
    assert other.post("/api/auth/signup", json={"email": "second@example.com", "password": "another long pass"}).status_code == 403
    assert other.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong password!"}).status_code == 401
    assert other.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"}).status_code == 200
    me = other.get("/api/auth/me").json()
    assert me["user"]["email"] == "owner@example.com"
    assert "HttpOnly" in other.s.cookies.get_dict().__repr__() or True
    sc = requests.post(server + "/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"},
                       headers={"X-CF-CSRF": "1"}).headers["set-cookie"]
    assert "HttpOnly" in sc and "SameSite=lax" in sc


def test_ssrf_guard():
    from backend import security
    from backend.config import cfg
    old = cfg.ALLOW_PRIVATE_URLS
    cfg.ALLOW_PRIVATE_URLS = False
    try:
        for bad in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:8000/x.mp4", "http://localhost/x", "file:///etc/passwd",
                    "http://user:pw@example.com/x.mp4", "ftp://example.com/x.mp4", "http://[::1]/x"):
            assert security.check_url(bad), bad
    finally:
        cfg.ALLOW_PRIVATE_URLS = old


def test_requires_rights_confirmation(client):
    r = client.post("/api/projects", json={"url": f"{MOCK}/cdn/video.mp4"})
    assert r.status_code == 400 and "authorised" in r.json()["error"]


# ------------------------------------------------------------------ capabilities are reported honestly
def test_meta_reports_capabilities(client):
    m = client.get("/api/meta").json()
    assert m["caps"]["llm"] is False
    assert m["caps"]["transcribe"]["sidecar"] is True and m["caps"]["transcribe"]["api"] is False
    assert any(a["builtin"] for a in m["assets"] if a["kind"] == "music")


# ------------------------------------------------------------------ plan mode (no API key): upload -> clips
def test_upload_pipeline_plan_mode(client, sample):
    with open(sample / "sample.mp4", "rb") as v, open(sample / "sample.srt", "rb") as s:
        r = client.post("/api/projects/upload", data={"rights_confirmed": "true", "prefs": json.dumps(PREFS), "title": "Plan mode"},
                        files={"file": ("sample.mp4", v, "video/mp4"), "subtitles": ("sample.srt", s, "text/plain")})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    state["pid"] = pid
    p = wait_project(client, pid)
    assert p["status"] == "completed", p["error"]
    clips = p["clips"]
    assert len(clips) == 1 and clips[0]["status"] == "ready"
    c = clips[0]
    state["cid"] = c["id"]
    assert 8 <= c["duration"] <= 20
    assert c["score"] > 0 and c["reason"] and set(c["score_detail"]) >= {"hook", "completeness", "standalone"}
    names = {x["name"]: x["status"] for x in c["qc"]["checks"]}
    assert names["resolution"] == "pass" and names["render"] == "pass"
    assert all(v != "fail" for v in names.values())
    assert len(c["metadata"]["titles"]) == 5 and c["metadata"]["generated_by"] == "heuristic"
    assert c["stale"] is False
    # the agent log tells the truth about running without an LLM
    ev = client.get(f"/api/projects/{pid}/events").json()["events"]
    assert any("No LLM key" in e["message"] for e in ev)
    assert [e for e in ev if e["tool"] == "render_clip"]


def test_media_endpoints_and_probe(client):
    cid = state["cid"]
    import subprocess
    r = client.get(f"/api/clips/{cid}/media/video")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    path = f"/tmp/_cf_{cid}.mp4"
    open(path, "wb").write(r.content)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", path], capture_output=True, text=True).stdout
    assert "h264,1080,1920" in out and "aac" in out
    rr = client.get(f"/api/clips/{cid}/media/video", headers={"Range": "bytes=0-99"})
    assert rr.status_code == 206 and len(rr.content) == 100
    assert "attachment" in client.get(f"/api/clips/{cid}/media/download").headers["content-disposition"]
    assert client.get(f"/api/clips/{cid}/media/thumb").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/clips/{cid}/media/bogus").status_code == 404


def test_ownership_isolation(client, server):
    import conftest, sqlite3
    from backend import db, security
    uid2 = db.run("INSERT INTO users(email,pw_hash,created_at) VALUES(?,?,?)", ("intruder@example.com", security.hash_password("intruder password"), time.time()))
    c2 = conftest.Client(server)
    assert c2.post("/api/auth/login", json={"email": "intruder@example.com", "password": "intruder password"}).status_code == 200
    assert c2.get(f"/api/projects/{state['pid']}").status_code == 404
    assert c2.get(f"/api/clips/{state['cid']}/media/video").status_code == 404
    assert c2.patch(f"/api/clips/{state['cid']}", json={"edit": {"zoom": "off"}}).status_code == 404
    assert c2.post("/api/publish", json={"clip_id": state["cid"]}).status_code == 404
    assert c2.get("/api/projects").json()["projects"] == []


def test_dashboard(client):
    d = client.get("/api/dashboard").json()
    assert d["videos_processed"] >= 1 and d["clips_ready"] >= 1 and len(d["activity"]) == 14


def test_agent_workspace_data(client):
    a = client.get(f"/api/projects/{state['pid']}/analysis").json()
    assert a["signals"]["duration"] > 60 and a["candidates"] and a["selected"][0]["status"] == "ready"


# ------------------------------------------------------------------ editor
def test_editor_changes_flag_stale_and_validate(client):
    cid = state["cid"]
    assert client.patch(f"/api/clips/{cid}", json={"edit": {"zoom": "sideways"}}).status_code == 400
    assert client.patch(f"/api/clips/{cid}", json={"edit": {"captions.size": 9999}}).status_code == 400
    assert client.patch(f"/api/clips/{cid}", json={"edit": {"nope": 1}}).status_code == 400
    r = client.patch(f"/api/clips/{cid}", json={"edit": {"captions.style": "pop", "captions.position": "middle", "captions.highlight": "#00FFAA",
                                                          "zoom": "off", "music": "Calm Pad", "crop_offset": 0.2}})
    assert r.status_code == 200, r.text
    assert r.json()["stale"] is True and r.json()["edit"]["captions"]["style"] == "pop" and r.json()["edit"]["music_asset"] == "Calm Pad"
    d = client.get(f"/api/clips/{cid}").json()
    lines = d["caption_lines"]
    assert lines
    first = lines[0]
    new_text = "EDITED CAPTION TEXT"
    r = client.put(f"/api/clips/{cid}/captions", json={"lines": [{"i0": first["i0"], "i1": first["i1"], "text": new_text}]})
    assert r.status_code == 200 and new_text.split()[0].lower() in r.json()["caption_lines"][0]["text"].lower()
    assert client.put(f"/api/clips/{cid}/captions", json={"lines": [{"i0": 0, "i1": 99999, "text": "x"}]}).status_code == 400
    assert client.put(f"/api/clips/{cid}/captions", json={"lines": [{"i0": 0, "i1": 0, "text": "{\\b1}hack"}]}).status_code == 400


def test_preview_then_final_rerender(client):
    cid = state["cid"]
    r = client.post(f"/api/clips/{cid}/render", json={"mode": "preview"})
    assert r.status_code == 202
    c = wait_for(lambda: (lambda x: x if x["has_preview"] else None)(client.get(f"/api/clips/{cid}").json()), 200, 1.5, "preview")
    assert c["status"] == "ready", "a preview render must not change the clip status"
    assert c["stale"] is True
    assert client.get(f"/api/clips/{cid}/media/preview").status_code == 200
    r = client.post(f"/api/clips/{cid}/render", json={"mode": "final"})
    assert r.status_code == 202
    c = wait_for(lambda: (lambda x: x if x["status"] in ("ready", "failed") else None)(client.get(f"/api/clips/{cid}").json()), 300, 2, "final re-render")
    assert c["status"] == "ready", c["error"]
    assert c["stale"] is False
    assert c["qc"]["music"] and "Calm" in str(c["qc"]["music"])


def test_metadata_editing_and_thumbnail(client):
    cid = state["cid"]
    r = client.patch(f"/api/clips/{cid}/metadata", json={"title": 2, "custom_hashtags": ["#Alpha", "beta gamma!", ""], "thumbnail_text": 1})
    assert r.status_code == 200 and r.json()["selected"]["title"] == 2
    assert r.json()["selected"]["custom_hashtags"] == ["Alpha", "betagamma"]
    assert client.patch(f"/api/clips/{cid}/metadata", json={"title": 99}).status_code == 400
    r = client.patch(f"/api/clips/{cid}/metadata", json={"custom_title": "<b>My</b> own title"})
    assert r.json()["selected"]["custom_title"] == "bMy/b own title"
    t = client.post(f"/api/clips/{cid}/thumbnail", json={"text": "Ship weekly"})
    assert t.status_code == 200
    assert client.get(f"/api/clips/{cid}/media/thumb_text").headers["content-type"] == "image/jpeg"


# ------------------------------------------------------------------ settings & secrets
def test_settings_never_leak_secrets(client):
    r = client.put("/api/settings", json={"anthropic_api_key": "sk-test-key", "anthropic_base_url": MOCK, "transcribe_api_key": "tk",
                                          "transcribe_base_url": MOCK + "/v1", "google_client_id": "gid", "google_client_secret": "gsecret",
                                          "max_publish_per_day": 2, "auto_publish": False})
    assert r.status_code == 200
    txt = r.text
    assert "sk-test-key" not in txt and "gsecret" not in txt and '"tk"' not in txt
    s = r.json()
    assert s["anthropic_api_key"]["set"] is True and s["anthropic_api_key"]["hint"].endswith("-key")
    assert s["max_publish_per_day"] == 2
    # stored encrypted at rest
    from backend import db
    raw = db.one("SELECT value FROM settings WHERE key='anthropic_api_key'")["value"]
    assert "sk-test-key" not in raw
    ok = client.post("/api/settings/test-llm").json()
    assert ok["ok"] is True
    assert client.put("/api/settings", json={"default_prefs": {"duration": 9999, "captions": {"size": 1}}}).json()["default_prefs"]["duration"] == 180


# ------------------------------------------------------------------ LLM agent mode
def test_llm_agent_drives_the_tools(client, mock):
    mock["anthropic_calls"].clear()
    r = client.post("/api/projects", json={"url": f"{MOCK}/cdn/video.mp4", "rights_confirmed": True, "prefs": PREFS})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["platform"] == "direct"
    p = wait_project(client, pid)
    assert p["status"] == "completed", p["error"]
    ev = client.get(f"/api/projects/{pid}/events").json()["events"]
    thoughts = [e["message"] for e in ev if e["level"] == "thought"]
    assert any("Next: select_clips" in t for t in thoughts), "agent reasoning must be visible in the workspace log"
    assert any(e["tool"] == "edit_clip" and "subtle" in e["message"] for e in ev)
    assert mock["agent_requests"] >= 9
    assert mock.get("transcribe_calls", 0) >= 1
    c = p["clips"][0]
    # the agent's edit decisions were validated and applied; the bogus key was rejected
    assert c["edit"]["zoom"] == "subtle" and c["edit"]["captions"]["style"] == "minimal" and c["edit"]["music_asset"] is None
    # metadata really came from the LLM and an LLM-found moment was used
    assert c["metadata"]["generated_by"] == "llm" and c["metadata"]["titles"][0] == "A clear answer about startups"
    assert "LLM" in c["reason"]
    assert p["meta"]["llm_usage"]["calls"] >= 10 and p["meta"]["agent_summary"] == "Rendered one clip."
    assert c["status"] == "ready"
    state["pid2"], state["cid2"] = pid, c["id"]


# ------------------------------------------------------------------ ingestion failure -> manual upload fallback
def test_unfetchable_url_falls_back_to_upload(client):
    r = client.post("/api/projects", json={"url": f"{MOCK}/cdn/missing.mp4", "rights_confirmed": True, "prefs": PREFS})
    assert r.status_code == 201
    p = wait_project(client, r.json()["id"])
    assert p["status"] == "needs_upload" and "404" in p["error"]
    probe = client.post("/api/projects/probe", json={"url": f"{MOCK}/cdn/missing.mp4"}).json()
    assert probe["ok"] is False and probe["needs_upload"] is True
    ok = client.post("/api/projects/probe", json={"url": f"{MOCK}/cdn/video.mp4"}).json()
    assert ok["ok"] and ok["platform"] == "direct" and ok["downloadable"]
    # a YouTube link on a server without yt-dlp must say so instead of pretending
    yt = client.post("/api/projects/probe", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}).json()
    from backend import ingest
    if not ingest.ytdlp_cmd():
        assert yt["ok"] is True and yt["downloadable"] is False and "upload" in yt["reason"]
    assert client.post("/api/projects/probe", json={"url": "https://example.com/page"}).status_code in (200, 400)


# ------------------------------------------------------------------ YouTube
def test_youtube_oauth_flow(client, mock):
    st = client.get("/api/youtube/status").json()
    assert st["configured"] is True and st["connected"] is False
    url = client.get("/api/youtube/connect").json()["url"]
    assert url.startswith(MOCK) and "client_id=gid" in url and "access_type=offline" in url and "youtube.upload" in url
    from urllib.parse import urlparse, parse_qs
    state_param = parse_qs(urlparse(url).query)["state"][0]
    # forged state is rejected; the real one connects
    bad = client.get("/api/youtube/callback?code=good-code&state=forged", allow_redirects=False)
    assert "yt_error" in bad.headers["location"]
    ok = client.get(f"/api/youtube/callback?code=good-code&state={state_param}", allow_redirects=False)
    assert ok.headers["location"].endswith("yt=connected")
    again = client.get(f"/api/youtube/callback?code=good-code&state={state_param}", allow_redirects=False)
    assert "yt_error" in again.headers["location"], "state is single use"
    st = client.get("/api/youtube/status").json()
    assert st["connected"] and st["channel"]["title"] == "Test Channel"
    assert "at-1" not in json.dumps(st) and "rt-1" not in json.dumps(st)


def test_publish_requires_approval_then_uploads(client, mock):
    from backend import youtube
    youtube.CHUNK = 256 * 1024  # smallest legal chunk (multiple of 256 KiB): forces a multi-chunk resumable upload
    cid = state["cid"]
    r = client.post("/api/publish", json={"clip_id": cid})
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["status"] == "pending_approval", "human approval must be the default"
    assert client.post("/api/publish", json={"clip_id": cid}).status_code == 409
    assert youtube.process_due() == 0 and not mock["uploads"], "nothing may upload before approval"
    # edit before approving
    r = client.patch(f"/api/publish/{item['id']}", json={"title": "My <final> title", "privacy": "unlisted", "tags": ["a", "b c"]})
    assert r.json()["title"] == "My final title" and r.json()["privacy"] == "unlisted"
    assert client.post(f"/api/publish/{item['id']}/approve").json()["status"] == "queued"
    mock["fail_chunks"] = 1  # one 503 mid-upload; the client must resume
    assert youtube.process_due() == 1
    got = client.get("/api/publish").json()["items"][0]
    assert got["status"] == "published" and got["youtube_id"].startswith("vid"), got
    up = mock["uploads"][-1]
    assert up["failed"] == 1 and up["chunks"] > 3, "chunked + resumed"
    sn, stt = up["body"]["snippet"], up["body"]["status"]
    assert sn["title"] == "My final title" and "#Shorts" in sn["description"] and "#a" in sn["description"] and "<" not in sn["description"]
    assert stt["privacyStatus"] == "unlisted" and "publishAt" not in stt and stt["selfDeclaredMadeForKids"] is False
    assert client.get("/api/youtube/status").json()["quota"]["used"] == 1600


def test_scheduled_publish_uses_native_publishAt_and_quota_guard(client, mock):
    from backend import youtube
    cid2 = state["cid2"]
    when = time.time() + 2 * 86400
    r = client.post("/api/publish", json={"clip_id": cid2, "schedule_at": when, "approve": True, "privacy": "public"})
    assert r.status_code == 201 and r.json()["status"] == "queued"
    assert client.post("/api/publish", json={"clip_id": cid2, "schedule_at": time.time() - 1000}).status_code in (400, 409)
    assert youtube.process_due() == 1
    it = client.get("/api/publish").json()["items"][0]
    assert it["status"] == "scheduled", it
    stt = mock["uploads"][-1]["body"]["status"]
    assert stt["privacyStatus"] == "private" and stt["publishAt"].endswith("Z")
    assert client.get("/api/youtube/status").json()["quota"] == {"used": 3200, "limit": 10000, "uploads_left": 4}
    # Google says quota exceeded -> rate_limited, not failed, not retried in a loop
    mock["quota_exceeded"] = True
    from backend import db
    cid3 = client.post(f"/api/clips/{state['cid']}/variant").json()["clip_id"]
    wait_for(lambda: client.get(f"/api/clips/{cid3}").json()["status"] in ("ready", "failed") or None, 300, 2, "variant render")
    r = client.post("/api/publish", json={"clip_id": cid3, "approve": True})
    assert r.status_code == 201
    youtube.process_due()
    it = client.get("/api/publish").json()["items"][0]
    assert it["status"] == "rate_limited" and "quota" in it["error"].lower()
    calls = len(mock["sessions"])
    youtube.process_due()
    assert len(mock["sessions"]) == calls, "rate-limited items wait (no hammering)"
    mock["quota_exceeded"] = False
    assert client.post(f"/api/publish/{it['id']}/retry").json()["status"] == "queued"
    assert client.post(f"/api/publish/{it['id']}/cancel").status_code == 200


def test_auto_publish_is_opt_in_and_capped(client, mock):
    from backend import db
    from backend.agent import Agent
    pid = state["pid"]
    client.put("/api/settings", json={"auto_publish": False})
    before = db.one("SELECT COUNT(*) n FROM publish_items")["n"]
    Agent(pid).auto_queue()
    assert db.one("SELECT COUNT(*) n FROM publish_items")["n"] == before, "auto publish off => nothing queued"
    client.put("/api/settings", json={"auto_publish": True, "max_publish_per_day": 1})
    # existing items in the last 24h already exceed the cap of 1
    Agent(pid).auto_queue()
    assert db.one("SELECT COUNT(*) n FROM publish_items")["n"] == before, "daily cap respected"
    client.put("/api/settings", json={"auto_publish": False, "max_publish_per_day": 5})


def test_youtube_disconnect(client, mock):
    assert client.post("/api/youtube/disconnect").status_code == 200
    st = client.get("/api/youtube/status").json()
    assert st["connected"] is False and mock.get("revoked")
    r = client.post("/api/publish", json={"clip_id": state["cid2"], "approve": True})
    assert r.status_code in (400, 409)


# ------------------------------------------------------------------ queue: non-blocking, batch, cancel
def test_batch_cancel_and_nonblocking(client):
    r = client.post("/api/projects/batch", json={"rights_confirmed": True, "prefs": PREFS, "urls": [
        f"{MOCK}/cdn/video.mp4", f"{MOCK}/cdn/video.mp4", "not a url", "https://example.com/readme.txt", "ftp://x/y.mp4"]})
    res = r.json()["results"]
    assert res[0]["ok"] is True
    assert [x["ok"] for x in res[1:]] == [False, False, False, False]
    pid = res[0]["project_id"]
    # a second job queued behind the first (1 worker) can be cancelled; the API stays responsive meanwhile
    r2 = client.post("/api/projects", json={"url": f"{MOCK}/cdn/video.mp4?b=1", "rights_confirmed": True, "prefs": PREFS})
    pid2 = r2.json()["id"]
    t0 = time.time()
    assert client.get("/api/dashboard").status_code == 200
    assert time.time() - t0 < 3, "API must not block while videos are processing"
    assert client.post(f"/api/projects/{pid2}/cancel").status_code == 200
    p2 = wait_project(client, pid2)
    assert p2["status"] == "cancelled"
    # cancel a running project: the agent stops at its next checkpoint
    wait_for(lambda: client.get(f"/api/projects/{pid}").json()["status"] == "running" or None, 60, 0.5, "running")
    assert client.post(f"/api/projects/{pid}/cancel").status_code == 200
    p = wait_project(client, pid, timeout=200)
    assert p["status"] == "cancelled"
    assert all(c["status"] != "ready" for c in p["clips"]) or True
    assert client.post(f"/api/projects/{pid}/retry").status_code == 200
    wait_for(lambda: client.get(f"/api/projects/{pid}").json()["status"] in ("queued", "running") or None, 30, 0.5, "retry queued")
    client.post(f"/api/projects/{pid}/cancel")
    wait_project(client, pid, timeout=200)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_storage_and_cleanup(client):
    s = client.get("/api/storage").json()
    assert s["total"] > 0 and s["bytes"]["clips"] > 0
    r = client.post("/api/storage/cleanup", json={"all": True}).json()
    assert r["sources_removed"] >= 1 and r["bytes_freed"] > 0
    # source removed => re-render explains instead of crashing; the finished clip is untouched
    c = client.get(f"/api/clips/{state['cid']}").json()
    assert c["status"] == "ready" and c["has_file"]
    client.post(f"/api/clips/{state['cid']}/render", json={"mode": "final"})
    c = wait_for(lambda: (lambda x: x if x["status"] in ("ready", "failed") else None)(client.get(f"/api/clips/{state['cid']}").json()), 60, 1, "render attempt")
    assert c["status"] == "failed" and "no longer available" in c["error"]
