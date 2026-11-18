# ClipForge AI

Paste a video link or upload a file; a tool-using agent transcribes it, finds the best moments, cuts and edits vertical 1080x1920 clips (captions, face-follow framing, silence removal, music ducking), quality-checks every render, writes titles/hashtags/descriptions, and queues uploads to YouTube behind human approval.

## Run
```
pip install uvicorn starlette sse-starlette python-multipart jinja2 pillow numpy opencv-python-headless pyjwt requests
# ffmpeg + ffprobe must be on PATH
cp .env.example .env
python run.py            # http://localhost:8000 ; first visit creates the owner account
```
Optional: `pip install faster-whisper` (local speech recognition), `pip install yt-dlp` (YouTube/Twitch/Kick links).
Tests: `pytest tests` (spins up the app plus a mock Anthropic/Google/YouTube server; about 3 minutes).

## Architecture
- `backend/agent.py` - the agent: an LLM tool loop (ingest, transcribe, analyze_signals, detect_moments, select_clips, render, metadata) with a deterministic fallback plan when no API key is set.
- `backend/editor.py`, `captions.py`, `qc.py` - deterministic FFmpeg/OpenCV rendering and the quality gate (resolution, aspect, duration, black frames, loudness, clipping, caption sync). QC failures trigger automatic repair patches; a clip that still fails is marked failed, never completed.
- `backend/jobs.py` - SQLite-backed job queue + worker threads (videos process in parallel and never block the API), retries, cleanup, publish scheduler.
- `backend/youtube.py` - Google OAuth + resumable uploads via the official Data API; daily quota accounting; native scheduling; human approval by default, auto-publish opt-in and capped.
- `backend/app.py` - Starlette JSON API; `frontend/` - dependency-free dark dashboard (no build step).

## What you must configure
| Feature | Needed |
|---|---|
| LLM moment analysis, metadata, translation | `ANTHROPIC_API_KEY` (without it: heuristic mode, clearly labelled in the UI) |
| Speech recognition | `faster-whisper` installed, or `TRANSCRIBE_API_KEY`; or upload an .srt/.vtt |
| YouTube/Twitch/Kick links | `yt-dlp` installed (otherwise paste a direct URL or upload) |
| YouTube publishing | Google OAuth client ID/secret + redirect URI `<BASE_URL>/api/youtube/callback` |

## Notes and limits
- Only process videos you own or are authorised to use; DRM/restricted sources fall back to manual upload.
- Built-in music/SFX are synthesized locally (no copyrighted audio); you can upload your own licensed tracks.
- Instagram and TikTok are export targets (download the MP4); direct publishing is YouTube only.
- Verified in this build against mock servers: OAuth flow, resumable upload with retry, scheduling, quota guard. Not verified against live Google, Anthropic, or yt-dlp targets: no credentials or network were available.
