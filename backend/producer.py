"""Clip production: edit-decision building, render → QC → auto-repair loop, thumbnails, storage helpers."""
import copy
import json
import shutil
from pathlib import Path

from . import assets, db, editor, qc, settings, transcribe as T
from .config import cfg
from .llm import LLM

AUTO_MUSIC_TYPES = {"vlog", "comedy", "educational"}
MAX_REPAIRS = 2


class Cancelled(Exception):
    pass


def edit_fingerprint(edit: dict, words) -> str:
    """Hash of everything that affects the final render; compared with the clip's current state to flag stale renders."""
    import hashlib
    e = {k: v for k, v in edit.items() if k != "_safe"}
    w = json.loads(words) if isinstance(words, str) else words
    return hashlib.sha1(json.dumps([e, w], sort_keys=True, default=str).encode()).hexdigest()[:16]


def project_dir(pid: int) -> Path:
    return cfg.CLIP_DIR / f"p{pid}"


def work_dir(pid: int, cid: int, tag: str = "") -> Path:
    return cfg.WORK_DIR / f"p{pid}" / f"c{cid}{tag}"


def build_edit(prefs: dict, content_type: str, kind: str = "", variant: str = "A") -> dict:
    """Flatten resolved prefs into the per-clip edit-decision document used by the renderer."""
    e = dict(prefs.get("editing", {}))
    e["captions"] = copy.deepcopy(prefs.get("captions", {}))
    e["audio"] = copy.deepcopy(prefs.get("audio", {}))
    m = e["audio"].get("music", "auto")
    if m == "auto":
        want = content_type in AUTO_MUSIC_TYPES and kind not in ("emotional", "controversial", "story")
        e["music_asset"] = assets.MUSIC_FOR.get(content_type, "Calm Pad") if want else None
    elif m in ("none", "", None):
        e["music_asset"] = None
    else:
        e["music_asset"] = m
    e["sfx"] = e["audio"].get("sfx", "auto")
    e["trim"] = {"start_delta": 0.0, "end_delta": 0.0}
    e["crop_offset"], e["crop_zoom"], e["caption_offset"] = 0.0, 1.0, 0.0
    if variant == "B":   # deliberately different treatment of the same moment
        e["zoom"] = "off" if e.get("zoom") != "off" else "punchy"
        e["layout"] = "blur" if e.get("layout") in ("auto", "face") else "face"
        e["captions"]["style"] = "classic" if e["captions"].get("style") == "pop" else "pop"
        e["music_asset"], e["sfx"] = None, "none"
        e["jump_cut_zoom"] = False
    return e


def resolve_prefs(user_id: int, project_prefs: dict) -> dict:
    """user defaults  <  preset  <  project overrides."""
    base = settings.user_default_prefs(user_id)
    preset = project_prefs.get("preset") or base.get("preset", "auto")
    over = {k: v for k, v in project_prefs.items() if k != "preset"}
    merged = settings.deep_merge(base, {"preset": preset})
    if preset in settings.PRESETS:
        merged = settings.deep_merge(merged, settings.PRESETS[preset])
    return settings.deep_merge(merged, over)


def preset_for(content_type: str) -> str:
    return settings.CONTENT_TYPE_PRESET.get(content_type, "clean")


def apply_patch(edit: dict, patch: dict) -> dict:
    """Patch keys: dotted path to set ('audio.denoise'), '+path' to add to a number, '_safe' flag."""
    e = copy.deepcopy(edit)
    for key, val in patch.items():
        if key == "_safe":
            e["_safe"] = True
            continue
        add = key.startswith("+")
        parts = key.lstrip("+").split(".")
        cur = e
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = (float(cur.get(parts[-1], 0) or 0) + float(val)) if add else val
    return e


def asset_resolver(user_id: int):
    def resolve(kind, key):
        if key in (None, "", "none"):
            return None
        assets.wait_ready(90)  # built-in audio is synthesized in the background on first run
        a = assets.get_asset(key, user_id) if str(key).isdigit() else assets.named(kind, str(key))
        if a and a["kind"] == kind and Path(a["path"]).exists():
            return a
        return None
    return resolve


def clip_words_for_render(clip: dict, transcript: dict | None, edit: dict) -> list[dict]:
    """Stored (possibly user-edited) words plus transcript words revealed by trim extensions."""
    words = json.loads(clip["words"]) if isinstance(clip["words"], str) else clip["words"]
    trim = edit.get("trim", {})
    sd, ed = float(trim.get("start_delta", 0)), float(trim.get("end_delta", 0))
    if transcript and (sd < 0 or ed > 0):
        dur = clip["end"] - clip["start"]
        extra = []
        for w in T.all_words(transcript):
            rs, re_ = w["s"] - clip["start"], w["e"] - clip["start"]
            if (re_ <= 0.03 and re_ > sd) or (rs >= dur - 0.03 and rs < dur + ed):
                extra.append({"w": w["w"], "s": round(rs, 3), "e": round(re_, 3)})
        words = sorted(words + extra, key=lambda w: w["s"])
    return words


def render_clip(clip_id: int, mode: str = "final", repair: bool = True, log=lambda m: None, cancelled=lambda: False,
                llm: LLM | None = None) -> dict:
    """Render (and for final mode, QC + repair) one clip. Updates the clips row. Returns {'ok', 'qc', 'info'}."""
    clip = db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
    proj = db.one("SELECT * FROM projects WHERE id=?", (clip["project_id"],))
    if not proj or not proj["video_path"] or not Path(proj["video_path"]).exists():
        raise editor.RenderError("The source video is no longer available (it may have been cleaned up). Re-upload it to re-render.")
    src_info = {"duration": proj["duration"], "width": proj["width"], "height": proj["height"],
                **db.jl(proj["meta"], {}).get("probe", {})}
    src_info["has_audio"] = db.jl(proj["meta"], {}).get("probe", {}).get("has_audio", True)
    tr = None
    tp = Path(cfg.WORK_DIR) / f"p{proj['id']}" / "transcript.json"
    if tp.exists():
        tr = db.jl(tp.read_text())
    edit = db.jl(clip["edit"], {})
    resolver = asset_resolver(clip["user_id"])
    pdir = project_dir(proj["id"])
    pdir.mkdir(parents=True, exist_ok=True)
    suffix = "" if mode == "final" else "_preview"
    out = pdir / f"c{clip_id}{suffix}.mp4"
    context = None
    ctx_words = None
    attempts, last_qc, info = [], None, {}
    max_rounds = (MAX_REPAIRS + 1) if (repair and mode == "final") else 1
    prev_status = clip["status"]
    if mode == "final":
        db.update("clips", clip_id, status="rendering", error=None, updated_at=db.now())
    for rnd in range(max_rounds):
        if cancelled():
            raise Cancelled()
        wd = work_dir(proj["id"], clip_id, "" if mode == "final" else "_p")
        shutil.rmtree(wd, ignore_errors=True)
        words = clip_words_for_render({**clip, "words": clip["words"]}, tr, edit)
        rclip = {"start": clip["start"], "end": clip["end"], "words": words}
        try:
            info = editor.render(proj["video_path"], src_info, rclip, edit, out, wd, mode, resolver, log, cancelled,
                                 force_safe=bool(edit.get("_safe")))
        except editor.RenderError as ex:
            if str(ex) == "Cancelled":
                raise Cancelled()
            attempts.append({"round": rnd, "error": str(ex)[:300]})
            log(f"Render error (attempt {rnd + 1}): {str(ex)[:200]}")
            if rnd + 1 < max_rounds:
                edit = apply_patch(edit, {"_safe": True})
                continue
            if mode == "final":
                db.update("clips", clip_id, status="failed", error=str(ex)[:500], updated_at=db.now())
            return {"ok": False, "qc": last_qc, "info": info, "error": str(ex)}
        info["voice_path"] = str(wd / "voice_pre.wav")
        ws = info["source_range"]
        info["clip_words"] = [w for w in T.all_words(tr) if w["e"] > ws[0] + 0.03 and w["s"] < ws[1] - 0.03] if tr else []
        if tr:
            allw = T.all_words(tr)
            info["clip_words"] = [dict(w) for w in info["clip_words"]]
            context = {"before": " ".join(w["w"] for w in allw if w["e"] <= ws[0])[-600:],
                       "text": " ".join(w["w"] for w in info["clip_words"]),
                       "after": " ".join(w["w"] for w in allw if w["s"] >= ws[1])[:600]}
        if mode != "final":
            db.update("clips", clip_id, preview_path=str(out), updated_at=db.now())
            shutil.rmtree(wd, ignore_errors=True)
            return {"ok": True, "qc": None, "info": info}
        log("Running quality checks")
        last_qc = qc.check_clip(out, edit, info, mode, llm, context, expect_audio=src_info.get("has_audio", True))
        attempts.append({"round": rnd, "passed": last_qc["passed"], "failed": [c["name"] for c in last_qc["checks"] if c["status"] == "fail"]})
        if last_qc["passed"]:
            break
        if rnd + 1 >= max_rounds:
            break
        patches = last_qc["patches"]
        if not patches:
            break
        for p in patches:
            log(f"Repairing '{p['check']}': {p['why']}")
            edit = apply_patch(edit, p["patch"])
    shutil.rmtree(work_dir(proj["id"], clip_id), ignore_errors=True)
    if last_qc and not last_qc["passed"]:
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in last_qc["checks"] if c["status"] == "fail")
        db.update("clips", clip_id, status="failed", error=("Quality check failed: " + failed)[:600], edit=db.jd(edit),
                  qc=db.jd({**last_qc, "attempts": attempts}), updated_at=db.now())
        return {"ok": False, "qc": last_qc, "info": info, "error": failed}
    dur = float(info["out_duration"])
    thumb = pdir / f"c{clip_id}.jpg"
    try:
        editor.make_thumbnail(out, thumb, min(dur * 0.35, max(0.5, dur - 0.5)))
    except editor.RenderError:
        thumb = None
    db.update("clips", clip_id, status="ready", error=None, file_path=str(out), thumb_path=str(thumb) if thumb else None,
              duration=round(dur, 2), edit=db.jd(edit),
              qc=db.jd({**last_qc, "attempts": attempts, "edit_hash": edit_fingerprint({k: v for k, v in edit.items()}, clip["words"]), "decisions": info.get("decisions", []), "layout": info.get("layout"),
                        "music": info.get("music"), "sfx": info.get("sfx"), "source_range": info.get("source_range")}),
              updated_at=db.now())
    return {"ok": True, "qc": last_qc, "info": info}
