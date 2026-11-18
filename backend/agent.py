"""The ClipForge agent.

One central agent with specialised tools (a multi-agent split would add coordination cost for no gain here):

  LLM decides ........ which moments matter, what each clip needs (style), whether a QC report warrants another try,
                       and when the work is done.
  Deterministic tools . transcoding, cutting, face tracking, loudness, captions, QC measurements.

Two ways of driving the same tools:

  * LLM mode  - Claude runs a real tool-use loop (Messages API `tools`). Its reasoning is streamed into the event log.
  * Plan mode - no API key (or the LLM failed): the same tools run in a fixed order. Moment detection, editing choices
                and metadata fall back to heuristics, and the log says so.

Either way `ensure_complete()` runs at the end, so a confused or cut-off LLM can never leave a project half-done.
Nothing is marked `ready` unless the QC system passed it (see producer.render_clip).
"""
import json
import time
import traceback
from pathlib import Path

from . import analysis as A
from . import clips as C
from . import db, editspec, ingest, metadata, producer, settings
from . import transcribe as T
from .config import cfg
from .llm import LLM, LLMError
from .producer import Cancelled

MAX_AGENT_STEPS = 48


class NeedsUpload(Exception):
    """The source cannot be fetched with authorised means; the user must upload the file."""


class AgentFailure(Exception):
    pass


TOOL_SPECS = [
    {"name": "ingest_source", "description": "Fetch/validate the source video and return its metadata (duration, resolution, platform, title). Always first.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "transcribe_audio", "description": "Speech-to-text with word timestamps (uses a supplied subtitle file, local Whisper or the configured API). "
     "If no engine is available the pipeline continues without a transcript.",
     "input_schema": {"type": "object", "properties": {"language": {"type": "string", "description": "ISO code or 'auto'"}}}},
    {"name": "analyze_signals", "description": "Measure audio energy, silences, scene cuts, motion and face presence over the whole video.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "detect_moments", "description": "Generate and score candidate moments (hook, relevance, engagement, emotion, completeness, quality, standalone). "
     "Returns the top candidates with transcript snippets. Uses LLM understanding of the transcript when use_llm is true.",
     "input_schema": {"type": "object", "properties": {"use_llm": {"type": "boolean"}}}},
    {"name": "select_clips", "description": "Pick the final non-overlapping, non-repetitive clips from the candidates and create them. "
     "Returns clip ids with timestamps, score and reason.",
     "input_schema": {"type": "object", "properties": {"max_clips": {"type": "integer"}, "min_score": {"type": "number"}}}},
    {"name": "edit_clip", "description": "Change the edit decisions of one clip BEFORE rendering (or to fix a QC problem). Do not apply every effect to every clip: "
     "emotional/serious moments usually want no music and subtle zoom; gaming wants blurred layout; talking heads want face layout. "
     "Allowed keys and values: " + editspec.describe_for_agent(),
     "input_schema": {"type": "object", "properties": {"clip_id": {"type": "integer"}, "changes": {"type": "object"}}, "required": ["clip_id", "changes"]}},
    {"name": "discard_clip", "description": "Drop a clip that on review would mislead out of context or is a near-duplicate.",
     "input_schema": {"type": "object", "properties": {"clip_id": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["clip_id", "reason"]}},
    {"name": "render_clip", "description": "Render a clip to 1080x1920 and run the QC system (resolution, aspect, caption sync/readability, audio levels/clarity, "
     "black frames, decode errors, context). Failed checks are auto-repaired up to twice. Returns the QC report; the clip is 'ready' only if QC passed.",
     "input_schema": {"type": "object", "properties": {"clip_id": {"type": "integer"}}, "required": ["clip_id"]}},
    {"name": "generate_metadata", "description": "Write 5 titles, hooks, hashtags, description, thumbnail text, summary and niche for a rendered clip.",
     "input_schema": {"type": "object", "properties": {"clip_id": {"type": "integer"}}, "required": ["clip_id"]}},
    {"name": "finish", "description": "Declare the project done with a 1-2 sentence summary. Call only after every kept clip is rendered and has metadata.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]

SYSTEM = """You are ClipForge, an autonomous video-repurposing agent. Turn one long video into polished vertical short-form clips \
(YouTube Shorts / Reels / TikTok) by calling tools. Work in this order: ingest_source, transcribe_audio, analyze_signals, detect_moments, \
select_clips, then per clip: (optionally edit_clip) -> render_clip -> generate_metadata, then finish.

Principles:
- Understand the content first; choose edits that fit it. Never apply every effect to every clip.
- Prefer clips that stand alone and end naturally. If a selected clip would mislead out of context, discard_clip it.
- Read QC reports. If a check failed after the automatic repairs, try ONE targeted fix with edit_clip and render_clip again; otherwise accept the failure honestly.
- Never invent facts about the video. Be efficient: do not call tools you do not need, and keep your own commentary to one short sentence per step.
- The user's explicit preferences (duration, caption style, music, layout) override your taste."""


class Agent:
    def __init__(self, project_id: int):
        self.pid = project_id
        self.proj = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not self.proj:
            raise AgentFailure("Project not found")
        self.uid = self.proj["user_id"]
        self.ucfg = settings.effective(self.uid)
        self.llm = LLM.for_user(self.ucfg)
        self.prefs_in = db.jl(self.proj["prefs"], {})
        self.work = cfg.WORK_DIR / f"p{self.pid}"
        self.work.mkdir(parents=True, exist_ok=True)
        # state shared between tools
        self.info = None
        self.tr = None
        self.tr_done = False
        self.an = None
        self.cands = None
        self.vtype = ""
        self.content_type = "other"
        self.selected = False
        self.finished = None
        self.discarded: set[int] = set()
        self.meta_done: set[int] = set()
        self._last_cancel_check = 0.0
        self._cancel = False
        self.tools = {
            "ingest_source": self.t_ingest, "transcribe_audio": self.t_transcribe, "analyze_signals": self.t_analyze,
            "detect_moments": self.t_detect, "select_clips": self.t_select, "edit_clip": self.t_edit,
            "discard_clip": self.t_discard, "render_clip": self.t_render, "generate_metadata": self.t_metadata,
            "finish": self.t_finish,
        }
        self.base_prefs = self._base_prefs()

    # ------------------------------------------------------------ infrastructure
    def log(self, msg: str, level: str = "info", tool: str | None = None, data=None):
        db.run("INSERT INTO events(project_id,ts,level,tool,message,data) VALUES(?,?,?,?,?,?)",
               (self.pid, db.now(), level, tool, msg[:1500], db.jd(data) if data is not None else None))

    def stage(self, name: str, progress: float):
        db.update("projects", self.pid, stage=name, progress=round(min(1.0, max(0.0, progress)), 3), updated_at=db.now())

    def cancelled(self) -> bool:
        if self._cancel:
            return True
        t = time.time()
        if t - self._last_cancel_check > 0.5:
            self._last_cancel_check = t
            r = db.one("SELECT cancel_requested FROM projects WHERE id=?", (self.pid,))
            self._cancel = bool(r and r["cancel_requested"])
        return self._cancel

    def check_cancel(self):
        if self.cancelled():
            raise Cancelled()

    def _base_prefs(self) -> dict:
        """user defaults < preset < project overrides. 'auto' preset is resolved after content-type detection."""
        return settings.deep_merge(settings.user_default_prefs(self.uid), self.prefs_in)

    def prefs_for(self, content_type: str) -> dict:
        base = settings.user_default_prefs(self.uid)
        preset = self.prefs_in.get("preset") or base.get("preset", "auto")
        if preset == "auto":
            preset = producer.preset_for(content_type)
        over = {k: v for k, v in self.prefs_in.items() if k != "preset"}
        merged = settings.deep_merge(base, settings.PRESETS.get(preset, {}))
        merged = settings.deep_merge(merged, over)
        merged["preset"] = preset
        return merged

    def refresh_project(self):
        self.proj = db.one("SELECT * FROM projects WHERE id=?", (self.pid,))

    # ------------------------------------------------------------ tools
    def t_ingest(self, **_):
        self.stage("ingest", 0.02)
        self.refresh_project()
        p = self.proj
        meta = db.jl(p["meta"], {})
        if p["video_path"] and Path(p["video_path"]).exists() and meta.get("probe"):
            self.info = meta["probe"]
        elif p["source_type"] == "upload":
            if not p["video_path"] or not Path(p["video_path"]).exists():
                raise NeedsUpload("The uploaded file is missing. Please upload it again.")
            self.info = ingest.validate_media(p["video_path"])
        else:
            url = p["source_url"]
            try:
                finfo = ingest.fetch_info(url)
                self.log(f"Platform: {finfo['platform']}; title: {finfo.get('title') or 'n/a'}", tool="ingest_source")
                dest = cfg.SOURCE_DIR / f"p{self.pid}"
                path = ingest.download(url, finfo, dest, progress=lambda f: self.stage("ingest", 0.02 + 0.08 * f),
                                       cancelled=self.cancelled)
                self.info = ingest.validate_media(path)
            except ingest.IngestError as e:
                if str(e) == "Cancelled.":
                    raise Cancelled()
                if e.needs_upload:
                    raise NeedsUpload(str(e))
                raise AgentFailure(str(e))
            sidecar = next(iter(sorted(Path(path).parent.glob(f"{Path(path).stem}*.vtt"))), None)
            meta.update(platform_info={k: finfo.get(k) for k in ("platform", "id", "uploader", "thumbnail")})
            db.update("projects", self.pid, video_path=str(path), sidecar_path=str(sidecar) if sidecar else None,
                      title=p["title"] or finfo.get("title") or Path(path).stem, updated_at=db.now())
        meta["probe"] = self.info
        db.update("projects", self.pid, duration=self.info["duration"], width=self.info["width"], height=self.info["height"],
                  meta=db.jd(meta), updated_at=db.now())
        self.refresh_project()
        self.log(f"Source ready: {self.info['duration']:.0f}s, {self.info['width']}x{self.info['height']}, "
                 f"{'with' if self.info['has_audio'] else 'NO'} audio", tool="ingest_source")
        return {"duration": round(self.info["duration"], 1), "resolution": f"{self.info['width']}x{self.info['height']}",
                "has_audio": self.info["has_audio"], "title": self.proj["title"], "platform": self.proj["platform"]}

    def _need_info(self):
        if not self.info:
            raise AgentFailure("ingest_source has not been called yet")

    def t_transcribe(self, language: str | None = None, **_):
        self._need_info()
        self.stage("transcribe", 0.12)
        tp = self.work / "transcript.json"
        if tp.exists():
            self.tr = db.jl(tp.read_text())
            self.tr_done = True
            self.log("Reusing saved transcript", tool="transcribe_audio")
        else:
            lang = language or self.base_prefs.get("language", "auto")
            try:
                self.tr = T.transcribe(self.proj["video_path"], self.work / "asr", self.ucfg, self.info["duration"],
                                       sidecar=self.proj["sidecar_path"], language=lang,
                                       log=lambda m: self.log(m, tool="transcribe_audio"))
            except RuntimeError as e:
                self.tr = None
                self.log(f"{e}. Continuing without a transcript.", "warn", "transcribe_audio")
            self.tr_done = True
            if self.tr:
                tp.write_text(json.dumps(self.tr, ensure_ascii=False))
        self.check_cancel()
        if not self.tr or not T.all_words(self.tr):
            self.tr = None
            self.log("No transcript: moments will be chosen from audio/visual signals only and captions are unavailable. "
                     "Install faster-whisper, set a transcription API key in Settings, or upload an .srt/.vtt with the video.",
                     "warn", "transcribe_audio")
            return {"engine": "none", "words": 0, "note": "no transcript available"}
        words = T.all_words(self.tr)
        self.log(f"Transcript: {len(words)} words, language {self.tr.get('language') or 'unknown'}, engine {self.tr['engine']}",
                 tool="transcribe_audio")
        return {"engine": self.tr["engine"], "language": self.tr.get("language"), "words": len(words),
                "start_of_transcript": " ".join(w["w"] for w in words[:40])}

    def t_analyze(self, **_):
        self._need_info()
        self.stage("analyze", 0.3)
        self.an = A.analyze(self.proj["video_path"], self.info, cancelled=self.cancelled,
                            log=lambda m: self.log(m, tool="analyze_signals"))
        self.check_cancel()
        (self.work / "signals.json").write_text(json.dumps(A.downsample_signals(self.an)))
        from . import detectors
        self.log(f"Signals: {len(self.an['silences'])} pauses, {len(self.an['cuts'])} scene cuts, faces in "
                 f"{self.an['face_ratio']:.0%} of frames (detector: {detectors.backend_name() if detectors.available() else 'none'})",
                 tool="analyze_signals")
        return {"face_ratio": round(self.an["face_ratio"], 2), "silences": len(self.an["silences"]), "scene_cuts": len(self.an["cuts"]),
                "mean_loudness_db": round(self.an["mean_db"], 1), "clipping_ratio": round(self.an["clip_ratio"], 4)}

    def t_detect(self, use_llm: bool = True, **_):
        self._need_info()
        if self.an is None:
            self.t_analyze()
        self.stage("detect", 0.46)
        prefs = self.base_prefs
        cands = C.find_candidates(self.tr, self.an, self.info, prefs, lambda m: self.log(m, tool="detect_moments"))
        vt = ""
        if use_llm and self.llm.available and self.tr and prefs.get("llm_depth", "standard") != "off":
            try:
                cands, vt = C.llm_discover(self.llm, self.tr, cands, self.an, self.info, prefs, self.proj["title"],
                                           lambda m: self.log(m, tool="detect_moments"))
            except LLMError as e:
                self.log(f"LLM moment analysis unavailable ({e}); using heuristic scoring", "warn", "detect_moments")
        elif use_llm and not self.llm.available:
            self.log("No LLM key configured: moments are scored with transcript/audio/visual heuristics only (see Settings).",
                     "warn", "detect_moments")
        self.cands, self.vtype = cands, vt
        ct = prefs.get("content_type", "auto")
        self.content_type = ct if ct not in ("auto", "") else (vt if vt in settings.CONTENT_TYPE_PRESET else C.guess_content_type(self.tr, self.an))
        self.log(f"Content type: {self.content_type}", tool="detect_moments")
        top = sorted(cands, key=lambda c: c["score"], reverse=True)
        summary = [{"start": c["start"], "end": c["end"], "score": c["score"], "dims": c["dims"], "source": c["source"],
                    "text": (c["f"]["_text"][:110] + "…") if c["f"]["_text"] else ""} for c in top[:30]]
        (self.work / "candidates.json").write_text(json.dumps(summary))
        meta = db.jl(self.proj["meta"], {})
        meta["content_type"] = self.content_type
        db.update("projects", self.pid, meta=db.jd(meta), updated_at=db.now())
        return {"content_type": self.content_type, "candidates": len(cands), "top": summary[:10]}

    def _rel_words(self, s: float, e: float) -> list[dict]:
        if not self.tr:
            return []
        allw = T.all_words(self.tr)
        rel = [{"w": w["w"], "s": round(w["s"] - s, 3), "e": round(w["e"] - s, 3)}
               for w in allw if w["e"] > s + 0.03 and w["s"] < e - 0.03]
        return C.keyword_flags(rel, allw) if rel else []

    def t_select(self, max_clips: int | None = None, min_score: float | None = None, **_):
        if self.cands is None:
            self.t_detect()
        prefs = self.prefs_for(self.content_type)
        if max_clips:
            prefs["max_clips"] = max(1, min(int(max_clips), 12))
        if min_score is not None:
            prefs["min_score"] = float(min_score)
        chosen, note = C.select_clips(self.cands, prefs, self.an)
        if note:
            self.log(note, "warn", "select_clips")
        if db.one("SELECT 1 AS x FROM clips WHERE project_id=?", (self.pid,)):  # re-run: replace previous selection
            db.run("DELETE FROM clips WHERE project_id=?", (self.pid,))
        out = []
        for i, c in enumerate(chosen):
            words = self._rel_words(c["start"], c["end"])
            variants = ["A"] + (["B"] if int(prefs.get("variants", 1)) >= 2 and i < 2 else [])
            for v in variants:
                edit = producer.build_edit(prefs, self.content_type, c["kind"], v)
                if not words:
                    edit["captions"]["enabled"] = False
                cid = db.run(
                    "INSERT INTO clips(project_id,user_id,idx,variant,start,end,duration,score,score_detail,reason,kind,status,edit,words,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.pid, self.uid, i + 1, v, c["start"], c["end"], round(c["end"] - c["start"], 2), c["score"],
                     db.jd(c["dims"]), c["reason"] + (" (Alternative edit of the same moment.)" if v == "B" else ""), c["kind"], "pending",
                     db.jd(edit), db.jd(words), db.now(), db.now()))
                out.append({"clip_id": cid, "variant": v, "start": c["start"], "end": c["end"], "score": c["score"],
                            "kind": c["kind"], "reason": c["reason"], "preview_text": (c["f"]["_text"][:140])})
        self.selected = True
        self.log(f"Selected {len(chosen)} moments ({len(out)} clips incl. variants), scores "
                 f"{', '.join(str(round(c['score'])) for c in chosen)}", tool="select_clips")
        if not out:
            raise AgentFailure("No suitable moments were found in this video (it may be too short or have no usable audio).")
        return {"clips": out}

    def _clip(self, clip_id: int) -> dict:
        c = db.one("SELECT * FROM clips WHERE id=? AND project_id=?", (clip_id, self.pid))
        if not c:
            raise AgentFailure(f"Unknown clip_id {clip_id}")
        return c

    def t_edit(self, clip_id: int, changes: dict, **_):
        clip = self._clip(clip_id)
        patch, applied, rejected = editspec.validate_edit(changes, self.uid, max_trim=3.0)
        if patch:
            edit = producer.apply_patch(db.jl(clip["edit"], {}), patch)
            db.update("clips", clip_id, edit=db.jd(edit), updated_at=db.now())
            self.log(f"Edit decisions for clip {clip_id}: {json.dumps(applied)}", tool="edit_clip")
        return {"applied": applied, "rejected": rejected}

    def t_discard(self, clip_id: int, reason: str = "", **_):
        self._clip(clip_id)
        db.run("DELETE FROM clips WHERE id=?", (clip_id,))
        self.discarded.add(clip_id)
        self.log(f"Discarded clip {clip_id}: {reason}", tool="discard_clip")
        return {"discarded": clip_id}

    def t_render(self, clip_id: int, **_):
        clip = self._clip(clip_id)
        self.stage("render", self._render_progress(clip_id))
        self.log(f"Rendering clip {clip['idx']}{clip['variant']} ({clip['start']:.1f}s–{clip['end']:.1f}s)", tool="render_clip")
        res = producer.render_clip(clip_id, "final", True, lambda m: self.log(m, tool="render_clip"), self.cancelled, self.llm)
        q = res.get("qc") or {}
        fails = [{"check": c["name"], "detail": c["detail"]} for c in q.get("checks", []) if c["status"] == "fail"]
        warns = [{"check": c["name"], "detail": c["detail"]} for c in q.get("checks", []) if c["status"] == "warn"]
        if res["ok"]:
            self.log(f"Clip {clip['idx']}{clip['variant']} passed QC" + (f" with {len(warns)} warning(s)" if warns else ""), tool="render_clip")
        else:
            self.log(f"Clip {clip['idx']}{clip['variant']} FAILED: {res.get('error', '')[:300]}", "error", "render_clip")
        return {"ok": res["ok"], "failed_checks": fails, "warnings": warns, "error": res.get("error")}

    def _render_progress(self, clip_id: int) -> float:
        rows = db.q("SELECT id, status FROM clips WHERE project_id=? ORDER BY id", (self.pid,))
        done = sum(1 for r in rows if r["status"] in ("ready", "failed"))
        return 0.55 + 0.38 * done / max(1, len(rows))

    def t_metadata(self, clip_id: int, **_):
        clip = self._clip(clip_id)
        if clip["status"] != "ready":
            return {"error": "Render the clip successfully first."}
        self.stage("metadata", 0.95)
        words = db.jl(clip["words"], [])
        text = " ".join(w["w"] for w in words)
        meta = metadata.generate(self.llm, text, clip["duration"], self.content_type, clip["kind"], self.proj["title"])
        db.update("clips", clip_id, metadata=db.jd(meta), updated_at=db.now())
        self.meta_done.add(clip_id)
        self.log(f"Metadata for clip {clip['idx']}{clip['variant']} written by {meta['generated_by']}: “{meta['titles'][0]}”",
                 tool="generate_metadata")
        return {"titles": meta["titles"], "hashtags": meta["hashtags"][:6], "category": meta["category"], "generated_by": meta["generated_by"]}

    def t_finish(self, summary: str = "", **_):
        self.finished = summary or "done"
        return {"ok": True}

    # ------------------------------------------------------------ dispatch
    def call_tool(self, name: str, args: dict) -> dict:
        fn = self.tools.get(name)
        if not fn:
            return {"error": f"unknown tool {name}"}
        self.check_cancel()
        t0 = time.time()
        for attempt in range(2):
            try:
                out = fn(**(args or {}))
                self.log(f"{name} ok ({time.time() - t0:.1f}s)", "debug", name)
                return out
            except (Cancelled, NeedsUpload):
                raise
            except AgentFailure as e:
                return {"error": str(e)}
            except TypeError as e:  # bad arguments from the LLM
                return {"error": f"bad arguments: {e}"}
            except (ConnectionError, TimeoutError, OSError) as e:
                if attempt == 0:
                    self.log(f"{name} hit a transient error ({e}); retrying", "warn", name)
                    time.sleep(2)
                    continue
                return {"error": f"{name} failed: {e}"}
            except Exception as e:  # tool bug / unexpected: report to the agent, keep the log
                self.log(f"{name} crashed: {e}", "error", name, {"trace": traceback.format_exc()[-1500:]})
                return {"error": f"{name} failed: {type(e).__name__}: {e}"}
        return {"error": f"{name} failed"}

    # ------------------------------------------------------------ drivers
    def objective(self) -> str:
        p = self.proj
        shown = {k: self.base_prefs.get(k) for k in ("duration", "max_clips", "min_score", "variants", "preset", "content_type", "language")}
        shown["captions"] = {k: self.base_prefs["captions"].get(k) for k in ("enabled", "style", "position")}
        shown["music"] = self.base_prefs["audio"].get("music")
        return (f"Project '{p['title'] or 'untitled'}' from {p['platform']} ({'upload' if p['source_type'] == 'upload' else p['source_url']}).\n"
                f"User preferences (honour them): {json.dumps(shown)}\nProduce the clips and metadata, then call finish.")

    def run_llm_loop(self):
        messages = [{"role": "user", "content": self.objective()}]
        for step in range(MAX_AGENT_STEPS):
            self.check_cancel()
            resp = self.llm.messages(SYSTEM, messages, max_tokens=1500, tools=TOOL_SPECS)
            content = resp.get("content", [])
            messages.append({"role": "assistant", "content": content})
            for b in content:
                if b.get("type") == "text" and b["text"].strip():
                    self.log(b["text"].strip(), "thought")
            uses = [b for b in content if b.get("type") == "tool_use"]
            if not uses:
                break
            results = []
            for u in uses:
                out = self.call_tool(u["name"], u.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(out, default=str)[:7000],
                                **({"is_error": True} if "error" in out and len(out) == 1 else {})})
            messages.append({"role": "user", "content": results})
            if self.finished is not None:
                break
        else:
            self.log("Agent reached its step limit; completing remaining work with the default plan.", "warn")

    def ensure_complete(self):
        """Deterministic safety net: finish whatever the agent did not."""
        if not self.info:
            self.call_tool("ingest_source", {})
        if not self.tr_done:
            self.call_tool("transcribe_audio", {})
        if self.an is None:
            self.t_analyze()
        if self.cands is None:
            self.t_detect(use_llm=True)
        if not self.selected:
            self.t_select()
        for c in db.q("SELECT id FROM clips WHERE project_id=? AND status IN ('pending','rendering','failed') AND error IS NULL", (self.pid,)):
            self.t_render(c["id"])
        for c in db.q("SELECT id FROM clips WHERE project_id=? AND status='ready'", (self.pid,)):
            if not db.jl(db.one("SELECT metadata FROM clips WHERE id=?", (c["id"],))["metadata"], {}).get("titles"):
                self.t_metadata(c["id"])

    def finalize(self):
        rows = db.q("SELECT id,status,score,duration FROM clips WHERE project_id=?", (self.pid,))
        ready = [r for r in rows if r["status"] == "ready"]
        if not ready:
            err = "No clip passed quality control." if rows else "No clips were produced."
            failed = db.q("SELECT error FROM clips WHERE project_id=? AND error IS NOT NULL LIMIT 1", (self.pid,))
            if failed:
                err += " " + failed[0]["error"][:300]
            db.update("projects", self.pid, status="failed", stage="failed", error=err, updated_at=db.now())
            self.log(err, "error")
            return
        meta = db.jl(db.one("SELECT meta FROM projects WHERE id=?", (self.pid,))["meta"], {})
        meta["llm_usage"] = {"calls": self.llm.calls, "input_tokens": self.llm.input_tokens, "output_tokens": self.llm.output_tokens,
                             "model": self.llm.model if self.llm.available else None}
        meta["agent_summary"] = self.finished or f"Produced {len(ready)} clip(s) with the default plan."
        db.update("projects", self.pid, status="completed", stage="done", progress=1.0, error=None, meta=db.jd(meta), updated_at=db.now())
        self.log(f"Done: {len(ready)} ready clip(s)" + (f", {len(rows) - len(ready)} failed" if len(rows) > len(ready) else ""), "info")
        self.auto_queue()

    def auto_queue(self):
        """Automated publishing is opt-in (Settings). Even then the daily cap and QC gate apply."""
        from . import youtube
        if not self.ucfg["auto_publish"] or not youtube.connected(self.uid):
            return
        cap = self.ucfg["max_publish_per_day"]
        used = db.one("SELECT COUNT(*) n FROM publish_items WHERE user_id=? AND created_at>? AND status!='cancelled'",
                      (self.uid, db.now() - 86400))["n"]
        rows = db.q("SELECT * FROM clips WHERE project_id=? AND status='ready' AND variant='A' ORDER BY score DESC", (self.pid,))
        for c in rows:
            if used >= cap:
                break
            youtube.queue_clip(self.uid, c, approved=True)
            used += 1
            self.log(f"Auto-publish enabled: queued clip {c['idx']} for upload", tool="publish")

    def run(self):
        db.update("projects", self.pid, status="running", stage="starting", progress=0.01, error=None, updated_at=db.now())
        self.log("Agent started" + (f" (LLM: {self.llm.model})" if self.llm.available else " (no LLM key: deterministic plan)"))
        try:
            if self.llm.available:
                try:
                    self.run_llm_loop()
                except LLMError as e:
                    self.log(f"LLM agent loop stopped: {e}. Completing with the default plan.", "warn")
            self.check_cancel()
            self.ensure_complete()
            self.finalize()
        except Cancelled:
            db.update("projects", self.pid, status="cancelled", stage="cancelled", updated_at=db.now())
            self.log("Cancelled by user", "warn")
        except NeedsUpload as e:
            db.update("projects", self.pid, status="needs_upload", stage="needs_upload", error=str(e), updated_at=db.now())
            self.log(str(e), "warn")
        except (AgentFailure, ingest.IngestError) as e:
            db.update("projects", self.pid, status="failed", stage="failed", error=str(e)[:600], updated_at=db.now())
            self.log(str(e), "error")
        except Exception as e:
            db.update("projects", self.pid, status="failed", stage="failed", error=f"Unexpected error: {e}"[:600], updated_at=db.now())
            self.log(f"Unexpected error: {e}", "error", data={"trace": traceback.format_exc()[-2000:]})
        finally:
            self.an = None
