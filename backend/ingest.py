"""Video ingestion: URL validation, platform detection, metadata, authorized download, probing.

Only fetches what is reachable without circumventing anything: no DRM, no login-gated
content. If a platform URL cannot be fetched (e.g. yt-dlp not installed, or the platform
refuses), the project moves to `needs_upload` so the user can upload the file instead.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from .config import cfg
from .security import check_url

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
ALLOWED_FORMATS = ("mov", "mp4", "matroska", "webm", "m4a", "3gp")
MAX_DURATION = 12 * 3600


class IngestError(Exception):
    def __init__(self, msg: str, needs_upload: bool = False):
        super().__init__(msg)
        self.needs_upload = needs_upload


# ------------------------------------------------------------------ detection
def detect_platform(url: str) -> tuple[str, str | None]:
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = u.path
    if host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if path == "/watch":
            v = parse_qs(u.query).get("v", [None])[0]
            if v and re.fullmatch(r"[\w-]{11}", v):
                return "youtube", v
        m = re.match(r"^/(shorts|live|embed)/([\w-]{11})", path)
        if m:
            return "youtube", m.group(2)
    if host == "youtu.be":
        m = re.match(r"^/([\w-]{11})", path)
        if m:
            return "youtube", m.group(1)
    if host == "twitch.tv":
        m = re.match(r"^/videos/(\d+)", path)
        if m:
            return "twitch", m.group(1)
    if host == "kick.com":
        m = re.search(r"/videos?/([0-9a-fA-F-]{36})", path)
        if m:
            return "kick", m.group(1)
    if Path(path).suffix.lower() in VIDEO_EXTS:
        return "direct", None
    return "unknown", None


# ------------------------------------------------------------------ yt-dlp
def ytdlp_cmd() -> list[str] | None:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    import importlib.util
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


def _ytdlp_info(url: str) -> dict:
    cmd = ytdlp_cmd() + ["--dump-single-json", "--no-playlist", "--no-warnings", "--skip-download", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise IngestError("Timed out reading video metadata.", needs_upload=True)
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        low = r.stderr.lower()
        if "drm" in low:
            raise IngestError("This video is DRM-protected and cannot be processed. Upload a file you own instead.", True)
        if "sign in" in low or "login" in low or "private" in low or "members" in low:
            raise IngestError("This video requires sign-in or is private. Upload a file you own instead.", True)
        raise IngestError(f"Could not read video metadata: {err[0][:200]}", True)
    js = json.loads(r.stdout)
    if js.get("is_live"):
        raise IngestError("Live streams are not supported; wait for the VOD or upload the recording.", True)
    if any(f.get("has_drm") for f in js.get("formats", []) if f.get("has_drm") is True) and not any(
            not f.get("has_drm") for f in js.get("formats", [])):
        raise IngestError("This video is DRM-protected and cannot be processed.", True)
    return js


# ------------------------------------------------------------------ info
def _head(url: str):
    """HEAD/GET with manual redirects so every hop passes the SSRF check."""
    cur = url
    for _ in range(5):
        err = check_url(cur)
        if err:
            raise IngestError(err)
        try:
            r = requests.get(cur, stream=True, allow_redirects=False, timeout=20, headers={"User-Agent": "ClipForge/1.0"})
        except requests.RequestException as e:
            raise IngestError(f"Could not reach that URL ({type(e).__name__}). Check the link or upload the file instead.", needs_upload=True)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            r.close()
            cur = urljoin(cur, r.headers["location"])
            continue
        return r, cur
    raise IngestError("Too many redirects.")


def fetch_info(url: str) -> dict:
    err = check_url(url)
    if err:
        raise IngestError(err)
    platform, vid = detect_platform(url)
    info = {"platform": platform, "id": vid, "title": "", "duration": None, "uploader": "", "thumbnail": "",
            "downloadable": False, "reason": ""}
    if platform == "unknown":
        # maybe a direct link without extension
        r, _ = _head(url)
        ctype = r.headers.get("content-type", "")
        r.close()
        if r.status_code == 200 and ctype.startswith("video/"):
            platform = info["platform"] = "direct"
        else:
            raise IngestError("Unsupported link. Use a YouTube, Twitch VOD, Kick video or direct video URL, or upload a file.")
    if platform == "direct":
        r, final = _head(url)
        ctype, clen = r.headers.get("content-type", ""), r.headers.get("content-length")
        r.close()
        if r.status_code != 200:
            raise IngestError(f"The server returned HTTP {r.status_code} for that URL.", True)
        if not (ctype.startswith("video/") or ctype == "application/octet-stream" or Path(urlparse(final).path).suffix.lower() in VIDEO_EXTS):
            raise IngestError(f"That URL is not a video (content-type {ctype or 'unknown'}).", True)
        if clen and int(clen) > cfg.MAX_DOWNLOAD_MB * 1024 * 1024:
            raise IngestError(f"File is larger than the {cfg.MAX_DOWNLOAD_MB} MB download limit.", True)
        info.update(title=Path(urlparse(final).path).name or "video", downloadable=True,
                    size=int(clen) if clen else None, final_url=final)
        return info
    if ytdlp_cmd():
        js = _ytdlp_info(url)
        info.update(title=js.get("title", ""), duration=js.get("duration"), uploader=js.get("uploader", ""),
                    thumbnail=js.get("thumbnail", ""), downloadable=True)
        return info
    # No yt-dlp: metadata only where an official public endpoint exists.
    info["reason"] = ("yt-dlp is not installed on the server, so ClipForge cannot download from "
                      f"{platform.title()} links. Download the video you own and upload it instead.")
    if platform == "youtube":
        try:
            r = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=10)
            if r.status_code == 200:
                js = r.json()
                info.update(title=js.get("title", ""), uploader=js.get("author_name", ""), thumbnail=js.get("thumbnail_url", ""))
        except requests.RequestException:
            pass
    return info


# ------------------------------------------------------------------ download
def download(url: str, info: dict, dest_dir: Path, progress=lambda p: None, cancelled=lambda: False) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not info.get("downloadable"):
        raise IngestError(info.get("reason") or "This link cannot be downloaded.", True)
    if info["platform"] == "direct":
        return _download_direct(info.get("final_url", url), dest_dir, progress, cancelled)
    cmd = ytdlp_cmd() + ["--no-playlist", "--no-warnings", "--newline", "--max-filesize", f"{cfg.MAX_DOWNLOAD_MB}M",
                         "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                         "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en", "--sub-format", "vtt",
                         "-o", str(dest_dir / "%(id)s.%(ext)s"), url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in p.stdout:
        tail = (tail + [line.strip()])[-8:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            progress(float(m.group(1)) / 100)
        if cancelled():
            p.kill()
            raise IngestError("Cancelled.")
    p.wait()
    if p.returncode != 0:
        msg = " ".join(tail).lower()
        if "drm" in msg:
            raise IngestError("This video is DRM-protected and cannot be processed.", True)
        raise IngestError("Download failed: " + (tail[-1] if tail else "unknown error"), True)
    vids = [f for f in dest_dir.iterdir() if f.suffix.lower() in VIDEO_EXTS]
    if not vids:
        raise IngestError("Download finished but no video file was produced.", True)
    return max(vids, key=lambda f: f.stat().st_size)


def _download_direct(url: str, dest_dir: Path, progress, cancelled) -> Path:
    r, final = _head(url)
    if r.status_code != 200:
        raise IngestError(f"The server returned HTTP {r.status_code}.", True)
    name = re.sub(r"[^\w.-]", "_", Path(urlparse(final).path).name) or "video.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXTS:
        name += ".mp4"
    out = dest_dir / name
    total = int(r.headers.get("content-length") or 0)
    cap = cfg.MAX_DOWNLOAD_MB * 1024 * 1024
    try:
        _stream_to_file(r, out, total, cap, progress, cancelled)
    except requests.RequestException as e:
        out.unlink(missing_ok=True)
        raise IngestError(f"Download interrupted ({type(e).__name__}). Try again or upload the file instead.", True)
    return out


def _stream_to_file(r, out, total, cap, progress, cancelled):
    got = 0
    with open(out, "wb") as fh:
        for chunk in r.iter_content(1024 * 1024):
            got += len(chunk)
            if got > cap:
                fh.close()
                out.unlink(missing_ok=True)
                raise IngestError(f"Download exceeded the {cfg.MAX_DOWNLOAD_MB} MB limit.", True)
            fh.write(chunk)
            if total:
                progress(got / total)
            if cancelled():
                fh.close()
                out.unlink(missing_ok=True)
                raise IngestError("Cancelled.")


# ------------------------------------------------------------------ probe
def probe(path: str | Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise IngestError("File is not a readable video.")
    js = json.loads(r.stdout)
    vs = next((s for s in js.get("streams", []) if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    if not vs:
        raise IngestError("File contains no video stream.")
    aus = next((s for s in js["streams"] if s.get("codec_type") == "audio"), None)
    fmt = js.get("format", {})
    w, h = int(vs["width"]), int(vs["height"])
    rot = 0
    for sd in vs.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(vs.get("tags", {}).get("rotate", 0) or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w
    num, _, den = (vs.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    dur = float(fmt.get("duration") or vs.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "fps": fps or 30.0, "vcodec": vs.get("codec_name"),
            "acodec": aus.get("codec_name") if aus else None, "has_audio": aus is not None,
            "format": fmt.get("format_name", ""), "size": int(fmt.get("size") or 0), "rotation": rot}


def validate_media(path: str | Path) -> dict:
    info = probe(path)
    if not any(f in info["format"] for f in ALLOWED_FORMATS):
        raise IngestError(f"Unsupported container ({info['format']}). Use MP4, MOV, MKV or WebM.")
    if info["duration"] <= 1:
        raise IngestError("Video is too short or its duration could not be read.")
    if info["duration"] > MAX_DURATION:
        raise IngestError("Video is longer than the 12 hour limit.")
    return info
