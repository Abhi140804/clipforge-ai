"""Mock of the three outside worlds ClipForge talks to: Anthropic Messages API, Google OAuth + YouTube Data API, a CDN."""
import json
import re

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

VIDEO = None
TRUTH = None
STATE = {
    "anthropic_calls": [], "agent_requests": 0, "quota_exceeded": False, "uploads": [], "sessions": {}, "tokens_issued": 0,
    "token_requests": [], "channel_requests": 0, "fail_chunks": 0,
}
AGENT_PLAN = ["ingest_source", "transcribe_audio", "analyze_signals", "detect_moments", "select_clips", "edit_clip", "render_clip",
              "generate_metadata", "finish"]


def _tool_results(messages):
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            return [json.loads(b["content"]) for b in m["content"] if b.get("type") == "tool_result"]
    return []


async def anthropic(request: Request):
    d = await request.json()
    if request.headers.get("x-api-key") != "sk-test-key":
        return JSONResponse({"error": {"message": "bad key"}}, 401)
    STATE["anthropic_calls"].append({"system": d["system"][:60], "tools": bool(d.get("tools")), "n": len(d["messages"])})
    system = d["system"]

    def text(t):
        return JSONResponse({"content": [{"type": "text", "text": t}], "usage": {"input_tokens": 100, "output_tokens": 50}})

    if d.get("tools"):  # the agent loop: follow a fixed plan, reading earlier results for ids
        step = sum(1 for m in d["messages"] if m["role"] == "assistant")
        STATE["agent_requests"] += 1
        if step >= len(AGENT_PLAN):
            return text("All done.")
        name = AGENT_PLAN[step]
        args = {}
        res = _tool_results(d["messages"])
        clip_id = None
        for m in d["messages"]:
            if m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        j = json.loads(b["content"])
                        if "clips" in j:
                            clip_id = j["clips"][0]["clip_id"]
        if name == "select_clips":
            args = {"max_clips": 1}
        elif name == "edit_clip":
            args = {"clip_id": clip_id, "changes": {"music": "none", "zoom": "subtle", "captions.style": "minimal", "bogus_key": 1}}
        elif name in ("render_clip", "generate_metadata"):
            args = {"clip_id": clip_id}
        elif name == "finish":
            args = {"summary": "Rendered one clip."}
        return JSONResponse({"content": [{"type": "text", "text": f"Next: {name}."},
                                         {"type": "tool_use", "id": f"tu_{step}", "name": name, "input": args}],
                             "usage": {"input_tokens": 100, "output_tokens": 50}})
    if "moment-detection" in system:  # propose the first ~20s block of the chunk
        lines = re.findall(r"^\[([\d.]+)-([\d.]+)\] (.*)$", d["messages"][0]["content"], re.M)
        if not lines:
            return text('{"video_type":"podcast","moments":[]}')
        s0 = float(lines[0][0])
        end = next((float(e) for s, e, _ in lines if float(e) - s0 >= 14), float(lines[-1][1]))
        return text(json.dumps({"video_type": "podcast", "moments": [{"start": s0, "end": end, "hook": 9, "relevance": 9, "engagement": 9, "emotion": 8,
                                "completeness": 9, "standalone": 9, "type": "insightful", "reason": "LLM: strong opening question and a clear answer.",
                                "misleading_risk": "low"}]}))
    if "metadata for a short vertical video" in system:
        return text(json.dumps({"titles": ["A clear answer about startups", "Why founders ship weekly", "Talk to customers", "Shipping beats planning", "The boring advice that works"],
                                "hooks": ["Why do startups fail?", "Here is the answer", "Listen to this", "Simple but hard"],
                                "hashtags": ["startups", "founders", "advice", "business", "shorts"], "description": "A short clip about why founders should ship weekly.",
                                "thumbnail_text": ["SHIP WEEKLY", "TALK TO CUSTOMERS", "STARTUP ADVICE"], "summary": "Founders who ship weekly survive.",
                                "category": "Education", "niche": "startup advice"}))
    return text("OK")


async def transcriptions(request: Request):
    if request.headers.get("authorization") != "Bearer tk":
        return JSONResponse({"error": "bad key"}, 401)
    STATE["transcribe_calls"] = STATE.get("transcribe_calls", 0) + 1
    cues = json.loads(open(TRUTH).read())["cues"]
    words, segs = [], []
    for c in cues:
        toks = c["text"].split()
        step = (c["end"] - c["start"]) / len(toks)
        ws = [{"word": t, "start": round(c["start"] + i * step, 3), "end": round(c["start"] + (i + 1) * step - 0.02, 3)} for i, t in enumerate(toks)]
        words += ws
        segs.append({"start": c["start"], "end": c["end"], "text": c["text"]})
    return JSONResponse({"language": "en", "words": words, "segments": segs})


async def cdn_video(request: Request):
    return FileResponse(VIDEO, media_type="video/mp4")


async def cdn_missing(request: Request):
    return Response("nope", status_code=404)


async def google_token(request: Request):
    f = dict(await request.form())
    STATE["token_requests"].append({k: v for k, v in f.items() if k != "client_secret"} | {"secret_ok": f.get("client_secret") == "gsecret"})
    if f.get("client_id") != "gid" or f.get("client_secret") != "gsecret":
        return JSONResponse({"error": "invalid_client"}, 401)
    STATE["tokens_issued"] += 1
    if f["grant_type"] == "authorization_code":
        if f.get("code") != "good-code":
            return JSONResponse({"error": "invalid_grant", "error_description": "bad code"}, 400)
        return JSONResponse({"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": "yt"})
    if f["grant_type"] == "refresh_token" and f.get("refresh_token") == "rt-1":
        return JSONResponse({"access_token": f"at-refreshed-{STATE['tokens_issued']}", "expires_in": 3600})
    return JSONResponse({"error": "invalid_grant"}, 400)


async def google_revoke(request: Request):
    STATE["revoked"] = True
    return JSONResponse({})


async def yt_channels(request: Request):
    STATE["channel_requests"] += 1
    if not request.headers.get("authorization", "").startswith("Bearer at-"):
        return JSONResponse({"error": {"message": "unauth"}}, 401)
    return JSONResponse({"items": [{"id": "UC123", "snippet": {"title": "Test Channel", "thumbnails": {"default": {"url": "http://x/y.jpg"}}}}]})


async def yt_upload_init(request: Request):
    if not request.headers.get("authorization", "").startswith("Bearer at-"):
        return JSONResponse({"error": {"message": "unauth"}}, 401)
    if STATE["quota_exceeded"]:
        return JSONResponse({"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}, 403)
    body = await request.json()
    sid = str(len(STATE["sessions"]) + 1)
    STATE["sessions"][sid] = {"body": body, "total": int(request.headers["x-upload-content-length"]), "got": 0, "chunks": 0, "failed": 0}
    return Response(status_code=200, headers={"Location": f"http://127.0.0.1:{request.url.port}/upload/session/{sid}"})


async def yt_upload_put(request: Request):
    s = STATE["sessions"][request.path_params["sid"]]
    data = await request.body()
    cr = request.headers.get("content-range", "")
    m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
    if cr.startswith("bytes */"):  # status query
        return Response(status_code=308, headers={"Range": f"bytes=0-{s['got'] - 1}"} if s["got"] else {})
    a, b = int(m.group(1)), int(m.group(2))
    if STATE["fail_chunks"] > 0 and s["chunks"] == 1:  # simulate one transient failure mid-upload
        STATE["fail_chunks"] -= 1
        s["failed"] += 1
        s["chunks"] += 1
        return Response(status_code=503)
    assert a == s["got"], f"out-of-order chunk {a} vs {s['got']}"
    s["got"] = b + 1
    s["chunks"] += 1
    if s["got"] >= s["total"]:
        vid = f"vid{request.path_params['sid']}"
        STATE["uploads"].append({"id": vid, **s})
        return JSONResponse({"id": vid})
    return Response(status_code=308, headers={"Range": f"bytes=0-{b}"})


app = Starlette(routes=[
    Route("/v1/messages", anthropic, methods=["POST"]), Route("/v1/audio/transcriptions", transcriptions, methods=["POST"]),
    Route("/cdn/video.mp4", cdn_video), Route("/cdn/missing.mp4", cdn_missing),
    Route("/google/token", google_token, methods=["POST"]), Route("/google/revoke", google_revoke, methods=["POST"]),
    Route("/youtube/v3/channels", yt_channels),
    Route("/upload/youtube/v3/videos", yt_upload_init, methods=["POST"]),
    Route("/upload/session/{sid}", yt_upload_put, methods=["PUT"]),
])
