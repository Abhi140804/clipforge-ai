"""Speech recognition.

Engine order (first that is available wins, unless the user pins one):
  1. sidecar   - an .srt/.vtt supplied by the user (always preferred when present)
  2. faster-whisper (local, if the python package is installed)
  3. openai-whisper (local, if installed)
  4. api       - any OpenAI-compatible /audio/transcriptions endpoint (OpenAI, Groq, self-hosted)
  5. none      - the pipeline continues with audio/visual signals only (no captions)

Transcript format used everywhere:
  {"engine", "language", "segments":[{"start","end","text","words":[{"w","s","e"}]}]}
"""
import re
import subprocess
from pathlib import Path

import requests

CHUNK_SECONDS = 1200
SENT_END = re.compile(r"[.!?…]+[\"')\]]*$")


# ---------------------------------------------------------------- helpers
def _ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def distribute_words(text: str, start: float, end: float) -> list[dict]:
    """Approximate word timings inside a cue, proportional to word length."""
    toks = text.split()
    if not toks:
        return []
    weights = [max(1, len(t)) for t in toks]
    total = float(sum(weights))
    dur = max(end - start, 0.05)
    out, t = [], start
    for tok, w in zip(toks, weights):
        d = dur * w / total
        out.append({"w": tok, "s": round(t, 3), "e": round(t + d, 3)})
        t += d
    return out


def parse_subtitles(text: str) -> list[dict]:
    """Parse SRT or WebVTT into segments (word timings are approximated)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    segs = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, b = lines[ti].split("-->")[:2]
        try:
            s, e = _ts(a.split()[0]), _ts(b.split()[0])
        except (ValueError, IndexError):
            continue
        body = " ".join(lines[ti + 1:])
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\{[^}]*\}", "", body).strip()
        if body and e > s:
            segs.append({"start": s, "end": e, "text": body, "words": distribute_words(body, s, e)})
    return segs


def normalize(segments: list[dict], engine: str, language: str = "") -> dict:
    """Make timings monotonic and make sure every segment has words."""
    out, last_end = [], 0.0
    for seg in sorted(segments, key=lambda x: x["start"]):
        words = seg.get("words") or distribute_words(seg["text"], seg["start"], seg["end"])
        clean = []
        for w in words:
            s = max(float(w["s"]), last_end if clean else 0.0)
            e = max(float(w["e"]), s + 0.02)
            clean.append({"w": str(w["w"]).strip(), "s": round(s, 3), "e": round(e, 3)})
            last_end = e
        clean = [w for w in clean if w["w"]]
        if clean:
            out.append({"start": clean[0]["s"], "end": clean[-1]["e"], "text": " ".join(w["w"] for w in clean), "words": clean})
    return {"engine": engine, "language": language, "segments": out}


def all_words(tr: dict) -> list[dict]:
    return [w for seg in (tr or {}).get("segments", []) for w in seg["words"]]


def words_between(tr: dict, s: float, e: float, tol: float = 0.03) -> list[dict]:
    """Words that really belong to [s, e] (tolerance avoids float-edge leakage of neighbouring words)."""
    return [w for w in all_words(tr) if w["e"] > s + tol and w["s"] < e - tol]


def sentences(tr: dict, gap: float = 0.8, max_len: float = 25.0) -> list[dict]:
    """Group words into sentence-like units (terminal punctuation, long pause, or length cap)."""
    sents, cur = [], []
    for w in all_words(tr):
        if cur and (w["s"] - cur[-1]["e"] > gap or w["e"] - cur[0]["s"] > max_len):
            sents.append(cur)
            cur = []
        cur.append(w)
        if SENT_END.search(w["w"]):
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)
    return [{"start": s[0]["s"], "end": s[-1]["e"], "text": " ".join(x["w"] for x in s),
             "terminal": bool(SENT_END.search(s[-1]["w"]))} for s in sents if s]


# ---------------------------------------------------------------- engines
def _extract_audio(video: str, out: Path, start: float = 0, dur: float | None = None, rate: int = 16000) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start)]
    if dur:
        cmd += ["-t", str(dur)]
    cmd += ["-i", video, "-vn", "-ac", "1", "-ar", str(rate), "-c:a", "aac", "-b:a", "48k", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"audio extraction failed: {r.stderr[-300:]}")


def _have(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def available_engines(ucfg: dict) -> dict:
    return {
        "sidecar": True,
        "faster_whisper": _have("faster_whisper"),
        "openai_whisper": _have("whisper"),
        "api": bool(ucfg.get("transcribe_api_key")),
    }


def _api_transcribe(video: str, work: Path, ucfg: dict, language: str, duration: float, log) -> dict:
    url = ucfg["transcribe_base_url"].rstrip("/") + "/audio/transcriptions"
    headers = {"Authorization": f"Bearer {ucfg['transcribe_api_key']}"}
    segments, detected = [], language or ""
    n_chunks = max(1, int(duration // CHUNK_SECONDS) + (1 if duration % CHUNK_SECONDS > 1 else 0))
    for k in range(n_chunks):
        off = k * CHUNK_SECONDS
        chunk = work / f"asr_{k}.m4a"
        _extract_audio(video, chunk, off, CHUNK_SECONDS)
        log(f"Transcribing audio chunk {k + 1}/{n_chunks} via API")
        data = [("model", ucfg["transcribe_model"]), ("response_format", "verbose_json"),
                ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
        if language and language != "auto":
            data.append(("language", language))
        resp = None
        for attempt in range(5):
            try:
                with open(chunk, "rb") as fh:
                    resp = requests.post(url, headers=headers, data=data, files={"file": (chunk.name, fh)}, timeout=600)
            except (requests.ConnectionError, requests.Timeout):
                resp = None
            if resp is not None and resp.status_code == 200:
                break
            if resp is not None and resp.status_code in (401, 403):
                raise RuntimeError("Transcription API rejected the credentials.")
            if resp is not None and resp.status_code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f"Transcription API error {resp.status_code}: {resp.text[:200]}")
            import time
            time.sleep(min(2 ** attempt * 2, 30))
        if resp is None or resp.status_code != 200:
            raise RuntimeError("Transcription API unavailable after retries.")
        js = resp.json()
        detected = detected or js.get("language", "")
        words = [{"w": w["word"], "s": w["start"] + off, "e": w["end"] + off} for w in js.get("words", [])]
        segs = js.get("segments") or []
        if segs:
            for sg in segs:
                s, e = sg["start"] + off, sg["end"] + off
                ws = [w for w in words if w["s"] >= s - 0.01 and w["s"] < e]
                segments.append({"start": s, "end": e, "text": sg["text"].strip(), "words": ws})
        elif words:
            segments.append({"start": words[0]["s"], "end": words[-1]["e"], "text": " ".join(w["w"] for w in words), "words": words})
        elif js.get("text"):
            segments.append({"start": off, "end": off + CHUNK_SECONDS, "text": js["text"], "words": []})
        chunk.unlink(missing_ok=True)
    return normalize(segments, "api:" + ucfg["transcribe_model"], detected)


def _faster_whisper(video: str, ucfg: dict, language: str, model_name: str) -> dict:
    from faster_whisper import WhisperModel  # type: ignore
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, info = model.transcribe(video, word_timestamps=True, vad_filter=True,
                                  language=None if language in ("", "auto") else language)
    out = []
    for s in segs:
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(),
                    "words": [{"w": w.word.strip(), "s": w.start, "e": w.end} for w in (s.words or [])]})
    return normalize(out, "faster-whisper:" + model_name, info.language)


def _openai_whisper(video: str, ucfg: dict, language: str, model_name: str) -> dict:
    import whisper  # type: ignore
    model = whisper.load_model(model_name)
    res = model.transcribe(video, word_timestamps=True, language=None if language in ("", "auto") else language)
    out = []
    for s in res["segments"]:
        out.append({"start": s["start"], "end": s["end"], "text": s["text"].strip(),
                    "words": [{"w": w["word"].strip(), "s": w["start"], "e": w["end"]} for w in s.get("words", [])]})
    return normalize(out, "whisper:" + model_name, res.get("language", ""))


def transcribe(video: str, work: Path, ucfg: dict, duration: float, sidecar: str | None = None,
               language: str = "auto", log=lambda m: None) -> dict | None:
    """Returns a transcript or None when no engine is available / usable."""
    work.mkdir(parents=True, exist_ok=True)
    if sidecar and Path(sidecar).exists():
        segs = parse_subtitles(Path(sidecar).read_text(errors="replace"))
        if segs:
            log(f"Using supplied subtitle file ({len(segs)} cues). Word timing is approximated inside each cue.")
            return normalize(segs, "sidecar", language if language != "auto" else "")
        log("Supplied subtitle file had no readable cues; trying speech recognition instead.")
    pinned = ucfg.get("transcribe_engine", "auto")
    eng = available_engines(ucfg)
    order = [pinned] if pinned not in ("auto", "") else ["faster_whisper", "openai_whisper", "api"]
    errors = []
    for name in order:
        if not eng.get(name):
            continue
        try:
            log(f"Running speech recognition with {name}")
            if name == "faster_whisper":
                return _faster_whisper(video, ucfg, language, ucfg.get("local_whisper_model", "base"))
            if name == "openai_whisper":
                return _openai_whisper(video, ucfg, language, ucfg.get("local_whisper_model", "base"))
            if name == "api":
                return _api_transcribe(video, work, ucfg, language, duration, log)
        except Exception as e:  # engine failure -> try next
            errors.append(f"{name}: {e}")
            log(f"{name} failed: {e}")
    if errors:
        raise RuntimeError("Speech recognition failed: " + "; ".join(errors))
    return None
