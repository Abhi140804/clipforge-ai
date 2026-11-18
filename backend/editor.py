"""Editing engine: raw cut -> analysis -> edit decisions -> deterministic ffmpeg render.

Order of operations for one clip
  1. cut the source range to a high-quality intermediate (frame-accurate)
  2. analyse the intermediate: audio envelope, face / active-speaker track, luma
  3. decide: pauses to remove, layout (face-follow / blurred-fit / centre), zoom punches, SFX, denoise, colour
  4. render video (layout -> jump cuts -> punch zoom -> colour -> burned captions) and audio (clean -> loudness -> music+duck -> SFX)
  5. mux
Decisions are content-driven and logged in the returned info dict.
"""
import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from . import analysis as A
from . import captions as C

QUALITY = {"final": (1080, 1920, 18, "veryfast"), "preview": (540, 960, 27, "ultrafast")}
FPS = 30


class RenderError(Exception):
    pass


def ff(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RenderError("ffmpeg timed out")
    if r.returncode != 0:
        raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-600:]}")
    return r


def even(x: float) -> int:
    return int(round(x / 2.0)) * 2


# ------------------------------------------------------------------ step 1
def extract_raw(src: str, start: float, dur: float, has_audio: bool, out: Path) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}"]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0", "-vf", f"fps={FPS},format=yuv420p",
            "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out)]
    ff(cmd)


# ------------------------------------------------------------------ step 2: tracking
def track_faces(raw: Path, W: int, H: int, dur: float, detector=None, fps: int = 5, cancelled=lambda: False) -> dict:
    """Face + active-speaker tracking at `fps`. Returns per-sample x targets (normalised), face ratio and mean luma.

    Active speaker: with 2+ faces, the face whose mouth region (lower 45% of the box) changes most over time wins,
    with hysteresis so the camera does not ping-pong.
    """
    detector = detector or A.detect_faces
    fw = 320
    fh = even(fw * H / W)
    cmd = ["ffmpeg", "-v", "error", "-an", "-i", str(raw), "-vf", f"fps={fps},scale={fw}:{fh}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = int(math.ceil(dur * fps))
    size = fw * fh
    tracks: list[dict] = []   # {x, ema}
    targets: list[float | None] = []
    luma, face_frames, prev = [], 0, None
    cur_id, since = None, 0
    for k in range(n):
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        g = np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw)
        luma.append(float(g.mean()))
        faces = detector(g) if k % 1 == 0 else []
        cands = []
        for (cx, cy, w, h) in faces:
            m = 0.0
            if prev is not None:
                y0, y1 = int(cy + h * 0.05), int(min(fh, cy + h / 2))
                x0, x1 = int(max(0, cx - w / 2)), int(min(fw, cx + w / 2))
                if y1 > y0 and x1 > x0:
                    m = float(np.abs(g[y0:y1, x0:x1].astype(np.int16) - prev[y0:y1, x0:x1].astype(np.int16)).mean())
            cands.append((cx, m))
        # associate with tracks by nearest x
        for cx, m in cands:
            best = min(tracks, key=lambda t: abs(t["x"] - cx), default=None)
            if best is None or abs(best["x"] - cx) > fw * 0.18:
                tracks.append({"x": cx, "ema": m, "last": k})
            else:
                best["x"], best["ema"], best["last"] = cx, 0.6 * best["ema"] + 0.4 * m, k
        live = [t for t in tracks if k - t["last"] <= fps * 2]
        tracks = live
        seen = [t for t in tracks if t["last"] == k]
        if seen:
            face_frames += 1
            top = max(seen, key=lambda t: t["ema"])
            if len(seen) == 1:
                cur_id, since = id(seen[0]), 0
                chosen = seen[0]
            else:
                cur = next((t for t in seen if id(t) == cur_id), None)
                if cur is None:
                    chosen, cur_id, since = top, id(top), 0
                else:
                    since += 1
                    if top is not cur and top["ema"] > 1.6 * max(cur["ema"], 0.3) and since > fps * 0.8:
                        chosen, cur_id, since = top, id(top), 0
                    else:
                        chosen = cur
            targets.append(chosen["x"] / fw)
        else:
            targets.append(None)
        prev = g
        if k % 50 == 0 and cancelled():
            break
    p.kill()
    p.wait()
    return {"targets": targets, "fps": fps, "face_ratio": face_frames / max(1, len(targets)),
            "luma": float(np.mean(luma)) if luma else 110.0, "n": len(targets)}


def camera_path(targets, fps, W, cw, manual_offset=0.0) -> list[tuple[float, float]]:
    """Virtual camera operator: dead-band + speed limit, then simplified to a few keyframes (t, x_left)."""
    if not targets:
        return [(0.0, (W - cw) / 2)]
    arr = np.array([np.nan if t is None else t * W for t in targets], dtype=np.float64)
    if np.all(np.isnan(arr)):
        arr[:] = W / 2
    # fill gaps: hold last known value, then back-fill the head
    last = np.nan
    for i in range(len(arr)):
        if np.isnan(arr[i]):
            arr[i] = last
        else:
            last = arr[i]
    first = arr[~np.isnan(arr)][0]
    arr[np.isnan(arr)] = first
    k = 5
    pad = np.pad(arr, (k // 2, k // 2), mode="edge")
    med = np.array([np.median(pad[i:i + k]) for i in range(len(arr))])
    cam, moving, dead, vmax = med[0], False, 0.10 * cw, 0.45 * cw
    pts = []
    for i, tgt in enumerate(med):
        err = tgt - cam
        if abs(err) > 0.6 * cw:          # speaker switch: cut to the new speaker instead of panning across empty space
            cam, moving, err = tgt, False, 0.0
        if not moving and abs(err) > dead:
            moving = True
        if moving:
            step = np.clip(err * 0.3, -vmax / fps, vmax / fps)
            cam += step
            if abs(tgt - cam) < dead * 0.3:
                moving = False
        cam = float(np.clip(cam, cw / 2, W - cw / 2))
        x = float(np.clip(cam - cw / 2 + manual_offset * (W - cw) / 2, 0, W - cw))
        pts.append((i / fps, x))
    return rdp(pts, 3.0)


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (t0, x0), (t1, x1) = pts[0], pts[-1]
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        t, x = pts[i]
        xl = x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x0
        d = abs(x - xl)
        if d > best:
            best, idx = d, i
    if best > eps:
        return rdp(pts[:idx + 1], eps)[:-1] + rdp(pts[idx:], eps)
    return [pts[0], pts[-1]]


def lerp_expr(pts: list[tuple[float, float]]) -> str:
    if len(pts) == 1:
        return f"{pts[0][1]:.1f}"
    expr = f"{pts[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(pts, pts[1:]))):
        if t1 - t0 < 1e-6:
            continue
        expr = f"if(lt(t,{t1:.3f}),{x0:.2f}+({x1 - x0:.3f})*(t-{t0:.3f})/{t1 - t0:.3f},{expr})"
    return expr


# ------------------------------------------------------------------ step 3: decisions
def plan_segments(dur: float, words: list[dict], db: np.ndarray, ecfg: dict) -> list[tuple[float, float]]:
    """Keep-ranges (raw timeline) after removing pauses; leading/trailing dead air is trimmed too."""
    full = [(0.0, dur)]
    if not ecfg.get("remove_pauses", True) or dur < 6:
        return full
    thr, keep = float(ecfg.get("pause_threshold", 0.7)), float(ecfg.get("pause_keep", 0.22))
    cuts: list[tuple[float, float]] = []
    per = 1 / A.HOP
    def lvl(a, b):
        seg = db[int(a * per):int(b * per)]
        return float(np.percentile(seg, 80)) if seg.size else -90.0
    if words:
        speech = [lvl(w["s"], w["e"]) for w in words if w["e"] - w["s"] > 0.05]
        speech_med = float(np.median(speech)) if speech else -30.0
        head = words[0]["s"] - 0.15
        if head > 0.25:
            cuts.append((0.0, head))
        tail = words[-1]["e"] + 0.35
        if dur - tail > 0.3:
            cuts.append((tail, dur))
        for a, b in zip(words, words[1:]):
            gs, ge = a["e"], b["s"]
            if ge - gs >= thr and lvl(gs, ge) < speech_med - 14:   # keep laughter / music / loud non-speech
                cuts.append((gs + keep / 2, ge - keep / 2))
    elif db.size:
        for s, e in A.find_silences(db, min_dur=thr):
            if e - s >= thr:
                if s < 0.2:
                    cuts.append((0.0, max(0.0, e - 0.12)))
                elif e > dur - 0.2:
                    cuts.append((s + 0.3, dur))
                else:
                    cuts.append((s + keep / 2, e - keep / 2))
    cuts = [(a, b) for a, b in cuts if b - a > 0.15]
    if len(cuts) > 59:
        cuts = sorted(sorted(cuts, key=lambda c: c[1] - c[0], reverse=True)[:59])
    cuts.sort()
    segs, pos = [], 0.0
    for a, b in cuts:
        if a - pos > 0.25:
            segs.append((pos, a))
        pos = max(pos, b)
    if dur - pos > 0.25:
        segs.append((pos, dur))
    total = sum(b - a for a, b in segs)
    if not segs or total < 3.0 or total < 0.45 * dur:
        return full
    return segs


def seg_map(segs):
    out, o = [], 0.0
    for a, b in segs:
        out.append((a, b, o))
        o += b - a
    return out, o


def raw_to_out(smap, t):
    for a, b, o in smap:
        if a <= t <= b:
            return o + (t - a)
    return None


def pick_emphasis(words, db, smap, out_dur, mode: str):
    """Moments that deserve a zoom punch / SFX, on the output timeline."""
    if mode == "off":
        return []
    cap = {"subtle": max(1, int(out_dur / 12)), "punchy": max(1, int(out_dur / 7))}.get(mode, max(1, int(out_dur / 12)))
    cap = min(cap, 6 if mode == "punchy" else 3)
    per = 1 / A.HOP
    base = float(np.median(db)) if db.size else -40.0
    cands = []
    if words:
        for w in words:
            mid = (w["s"] + w["e"]) / 2
            ot = raw_to_out(smap, mid)
            if ot is None:
                continue
            seg = db[int(w["s"] * per):int(w["e"] * per) + 1]
            peak = float(seg.max()) if seg.size else base
            cands.append((peak - base + (3.0 if w.get("k") else 0.0), ot))
    else:
        for i in range(2, len(db) - 2, 4):
            ot = raw_to_out(smap, i * A.HOP)
            if ot is not None:
                cands.append((float(db[i:i + 4].max()) - base, ot))
    chosen = []
    for sc, t in sorted(cands, reverse=True):
        if t < 0.9 or t > out_dur - 1.4:
            continue
        if all(abs(t - c) >= 3.5 for c in chosen):
            chosen.append(t)
        if len(chosen) >= cap:
            break
    return sorted(chosen)


# ------------------------------------------------------------------ step 4: graphs
def video_graph(*, W, H, OW, OH, layout, path_pts, cw, ch, y, segs, jump_zoom, punches, amp, color, luma, ass: bool) -> str:
    g = []
    if layout in ("face", "center"):
        g.append(f"[0:v]crop=w={cw}:h={ch}:x='{lerp_expr(path_pts)}':y={y},scale={OW}:{OH}:flags=lanczos,setsar=1[lv]")
    else:
        g.append("[0:v]split=2[bg][fg]")
        g.append(f"[bg]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},boxblur=24:3,eq=brightness=-0.10:saturation=0.9[bgb]")
        g.append(f"[fg]scale={OW}:-2:flags=lanczos[fgs]")
        g.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[lv]")
    n = len(segs)
    zw, zh = even(OW * 1.07), even(OH * 1.07)
    if n == 1:
        a, b = segs[0]
        g.append(f"[lv]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[vc]")
    else:
        g.append(f"[lv]split={n}" + "".join(f"[s{i}]" for i in range(n)))
        for i, (a, b) in enumerate(segs):
            z = f",scale={zw}:{zh}:flags=bicubic,crop={OW}:{OH}" if (jump_zoom and i % 2 == 1) else ""
            z += ",setsar=1"
            g.append(f"[s{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS{z}[t{i}]")
        g.append("".join(f"[t{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
    cur = "vc"
    if punches and amp > 0:
        zexpr = "+".join(f"{amp:.3f}*pow(sin(PI*clip((it-{t - 0.15:.3f})/0.95,0,1)),2)" for t in punches)
        g.append(f"[{cur}]zoompan=z='1+{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={OW}x{OH}:fps={FPS}[vz]")
        cur = "vz"
    if color:
        bright = 0.05 if luma < 85 else (-0.04 if luma > 175 else 0.0)
        g.append(f"[{cur}]eq=contrast=1.05:saturation=1.10:brightness={bright:.3f},unsharp=5:5:0.35:5:5:0.0[vcol]")
        cur = "vcol"
    if ass:
        g.append(f"[{cur}]ass=captions.ass[vs]")
        cur = "vs"
    g.append(f"[{cur}]format=yuv420p[vout]")
    return ";\n".join(g)


def voice_graph(segs, denoise: bool, enhance: bool) -> str:
    n = len(segs)
    parts = []
    if n == 1:
        a, b = segs[0]
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ac]")
    else:
        parts.append(f"[0:a]asplit={n}" + "".join(f"[a{i}]" for i in range(n)))
        for i, (a, b) in enumerate(segs):
            d = max(0.01, min(0.012, (b - a) / 3))
            parts.append(f"[a{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={d},afade=t=out:st={max(0, b - a - d):.3f}:d={d}[x{i}]")
        parts.append("".join(f"[x{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac]")
    chain = ["highpass=f=70"]
    if denoise:
        chain.append("afftdn=nr=12:nf=-30:tn=1")
    if enhance:
        chain += ["acompressor=threshold=0.09:ratio=3:attack=15:release=180:makeup=2",
                  "equalizer=f=180:t=q:w=1:g=-2", "equalizer=f=3000:t=q:w=1.2:g=2.5"]
    chain += ["aresample=48000", "aformat=channel_layouts=stereo"]
    parts.append("[ac]" + ",".join(chain) + "[vo]")
    return ";\n".join(parts)


def measure_loudness(path: Path) -> dict | None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        return None
    try:
        js = json.loads(m.group(0))
        vals = {k: float(js[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
        return vals if math.isfinite(vals["input_i"]) else None
    except (ValueError, KeyError):
        return None


def normalize_voice(src: Path, dst: Path, target: float) -> dict:
    m = measure_loudness(src)
    if not m:  # silence or unmeasurable
        ff(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:a", "pcm_s16le", str(dst)])
        return {"normalized": False}
    af = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    ff(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", af + ",aresample=48000", "-c:a", "pcm_s16le", str(dst)])
    return {"normalized": True, "before_lufs": m["input_i"], "target": target}


# ------------------------------------------------------------------ main
def render(src: str, src_info: dict, clip: dict, edit: dict, out_path: Path, work: Path, mode: str = "final",
           resolve_asset=lambda kind, key: None, log=lambda m: None, cancelled=lambda: False,
           detector=None, force_safe: bool = False) -> dict:
    """Render one clip. `clip` needs start/end (source seconds) and words (relative to clip start)."""
    OW, OH, crf, preset = QUALITY[mode]
    work.mkdir(parents=True, exist_ok=True)
    decisions = []
    trim = edit.get("trim", {})
    S = max(0.0, clip["start"] + float(trim.get("start_delta", 0)))
    E = min(src_info["duration"], clip["end"] + float(trim.get("end_delta", 0)))
    if E - S < 3:
        raise RenderError("Clip would be shorter than 3 seconds after trimming.")
    dur = E - S
    shift = S - clip["start"]
    words = [dict(w, s=round(w["s"] - shift, 3), e=round(w["e"] - shift, 3)) for w in clip.get("words", [])
             if w["e"] - shift > 0 and w["s"] - shift < dur]
    for w in words:
        w["s"], w["e"] = max(0.0, w["s"]), min(dur, w["e"])

    raw = work / "raw.mkv"
    log(f"Cutting {S:.1f}s–{E:.1f}s from the source")
    extract_raw(src, S, dur, src_info.get("has_audio", True), raw)
    if cancelled():
        raise RenderError("Cancelled")
    W, H = src_info["width"], src_info["height"]
    aspect = W / H

    # --- audio analysis
    db = A.audio_envelope(str(raw)) if src_info.get("has_audio", True) else np.zeros(int(dur / A.HOP))
    snr = float(np.percentile(db, 90) - np.percentile(db, 10)) if db.size else 60.0

    # --- layout decision
    lay_pref = edit.get("layout", "auto")
    need_track = lay_pref in ("auto", "face") and aspect > 0.7 and not force_safe
    track = track_faces(raw, W, H, dur, detector, cancelled=cancelled) if need_track else \
        {"targets": [], "fps": 5, "face_ratio": 0.0, "luma": 110.0, "n": 0}
    layout = lay_pref
    if layout == "auto":
        layout = "face" if (track["face_ratio"] >= 0.5 and aspect > 1.2) else ("center" if aspect <= 0.7 else "blur")
    if layout == "face" and track["face_ratio"] < 0.2:
        decisions.append(f"Face layout requested but faces visible only {track['face_ratio']:.0%} of the time; using blurred-background fit")
        layout = "blur"
    if force_safe:
        layout = "blur" if aspect > 0.7 else "center"
    if layout in ("face", "center") and aspect <= 0.62:
        pass
    crop_zoom = float(np.clip(edit.get("crop_zoom", 1.0), 1.0, 2.0))
    ch = even(H / crop_zoom)
    cw = even(ch * 9 / 16)
    if cw > W:
        cw, ch = even(W), even(W * 16 / 9)
        ch = min(ch, even(H))
    y = int(max(0, (H - ch) * (0.25 if crop_zoom > 1 else 0.0)))
    off = float(np.clip(edit.get("crop_offset", 0.0), -1, 1))
    if layout == "face":
        path_pts = camera_path(track["targets"], track["fps"], W, cw, off)
        decisions.append(f"Face-follow framing ({track['face_ratio']:.0%} face visibility, {len(path_pts)} camera keyframes)")
    else:
        path_pts = [(0.0, float(np.clip((W - cw) / 2 + off * (W - cw) / 2, 0, max(0, W - cw))))]
        decisions.append("Blurred-background fit (wide/no-face footage)" if layout == "blur" else "Centre crop")

    # --- pause removal / jump cuts
    segs = [(0.0, dur)] if force_safe else plan_segments(dur, words, db, edit)
    smap, out_dur = seg_map(segs)
    if len(segs) > 1:
        decisions.append(f"Removed pauses: {len(segs) - 1} cuts, {dur - out_dur:.1f}s trimmed")
    out_words = C.remap_words(words, smap)

    # --- zoom / sfx decisions
    zmode = edit.get("zoom", "auto")
    zmode = "subtle" if zmode == "auto" else zmode
    if force_safe:
        zmode = "off"
    punches = pick_emphasis(words, db, smap, out_dur, zmode)
    amp = {"subtle": 0.045, "punchy": 0.09}.get(zmode, 0.0)
    if punches:
        decisions.append(f"{len(punches)} zoom {'punch' if zmode == 'punchy' else 'accent'}es on emphasised moments")
    jump_zoom = bool(edit.get("jump_cut_zoom", True)) and len(segs) > 1 and not force_safe

    # --- captions
    cap = edit.get("captions", {})
    ass_on = bool(cap.get("enabled", True)) and bool(out_words)
    if ass_on:
        (work / "captions.ass").write_text(C.build_ass(out_words, cap, OW, OH, float(edit.get("caption_offset", 0.0))), encoding="utf-8")
    elif cap.get("enabled", True):
        decisions.append("No transcript available for this clip: rendered without captions")

    # --- video render
    graph = video_graph(W=W, H=H, OW=OW, OH=OH, layout=layout, path_pts=path_pts, cw=cw, ch=ch, y=y, segs=segs,
                        jump_zoom=jump_zoom, punches=punches, amp=amp, color=bool(edit.get("color", True)) and not force_safe,
                        luma=track["luma"], ass=ass_on)
    (work / "video.fg").write_text(graph)
    vid = work / "video.mp4"
    log("Rendering video")
    ff(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-filter_complex_script", "video.fg", "-map", "[vout]", "-an",
        "-r", str(FPS), "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p", "video.mp4"], cwd=work)
    if cancelled():
        raise RenderError("Cancelled")

    # --- audio render
    au = edit.get("audio", {})
    denoise = bool(au.get("denoise", True)) and snr < 38 and not force_safe
    if au.get("denoise", True) and not denoise:
        decisions.append(f"Audio already clean (dynamic range {snr:.0f} dB): noise reduction skipped")
    elif denoise:
        decisions.append("Noise reduction applied")
    (work / "voice.fg").write_text(voice_graph(segs, denoise, bool(au.get("voice_enhance", True)) and not force_safe))
    log("Processing audio")
    ff(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-filter_complex_script", "voice.fg", "-map", "[vo]", "-c:a", "pcm_s16le", "voice_pre.wav"], cwd=work)
    norm_info = {"normalized": False}
    voice = work / "voice_pre.wav"
    if au.get("normalize", True):
        norm_info = normalize_voice(voice, work / "voice_norm.wav", float(au.get("target_lufs", -14.0)))
        if norm_info["normalized"]:
            voice = work / "voice_norm.wav"
            decisions.append(f"Loudness normalised from {norm_info['before_lufs']:.1f} to {au.get('target_lufs', -14.0)} LUFS")

    music_path, music_name = None, None
    mkey = edit.get("music_asset")
    if mkey not in (None, "", "none"):
        a = resolve_asset("music", mkey)
        if a:
            music_path, music_name = a["path"], a["name"]
    sfx_list = []
    if not force_safe:
        user_sfx = edit.get("sfx_list")
        if isinstance(user_sfx, list):
            for it in user_sfx:
                a = resolve_asset("sfx", it.get("asset"))
                if a and 0 <= float(it.get("t", 0)) < out_dur:
                    sfx_list.append((float(it["t"]), a["path"], a["name"]))
        elif edit.get("sfx", "auto") == "auto" and zmode == "punchy":
            for i, t in enumerate(punches[:4]):
                a = resolve_asset("sfx", "Whoosh" if i % 2 == 0 else "Pop")
                if a:
                    sfx_list.append((max(0.0, t - 0.25), a["path"], a["name"]))
    final_audio = work / "final_audio.m4a"
    if music_path or sfx_list:
        ins = ["-i", str(voice)]
        fg = ["[0:a]asplit=2[v1][v2]"]
        k = 1
        mix_in = ["[v1]"]
        if music_path:
            ins += ["-stream_loop", "-1", "-i", music_path]
            mdb = float(au.get("music_db", -18.0))
            fg.append(f"[{k}:a]atrim=0:{out_dur:.3f},asetpts=PTS-STARTPTS,loudnorm=I={float(au.get('target_lufs', -14.0)) + mdb:.1f}:TP=-3:LRA=7,"
                      f"afade=t=in:d=1.2,afade=t=out:st={max(0, out_dur - 1.5):.3f}:d=1.5,aformat=channel_layouts=stereo[m]")
            if au.get("ducking", True):
                fg.append("[m][v2]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[md]")
                mix_in.append("[md]")
            else:
                fg.append("[v2]anullsink")
                mix_in.append("[m]")
            k += 1
            decisions.append(f"Background music '{music_name}' at {mdb:.0f} dB with {'speech ducking' if au.get('ducking', True) else 'no ducking'}")
        else:
            fg.append("[v2]anullsink")
        for (t, p, nm) in sfx_list:
            ins += ["-i", p]
            ms = int(t * 1000)
            fg.append(f"[{k}:a]adelay={ms}|{ms},volume=-9dB,aformat=channel_layouts=stereo[sf{k}]")
            mix_in.append(f"[sf{k}]")
            k += 1
        if sfx_list:
            decisions.append(f"{len(sfx_list)} sound effects: " + ", ".join(sorted({n for _, _, n in sfx_list})))
        fg.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:duration=first:normalize=0,alimiter=limit=0.89:level=disabled[mx]")
        (work / "mix.fg").write_text(";\n".join(fg))
        ff(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex_script", "mix.fg", "-map", "[mx]", "-t", f"{out_dur:.3f}",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(final_audio)], cwd=work)
    else:
        ff(["ffmpeg", "-y", "-v", "error", "-i", str(voice), "-af", "alimiter=limit=0.89:level=disabled", "-t", f"{out_dur:.3f}",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(final_audio)])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ff(["ffmpeg", "-y", "-v", "error", "-i", str(vid), "-i", str(final_audio), "-c", "copy", "-movflags", "+faststart",
        "-shortest", str(out_path)])
    return {"out_duration": round(out_dur, 3), "layout": layout, "segments": [(round(a, 3), round(b, 3)) for a, b in segs],
            "smap": [(round(a, 3), round(b, 3), round(o, 3)) for a, b, o in smap], "punches": [round(p, 2) for p in punches],
            "decisions": decisions, "face_ratio": round(track["face_ratio"], 3), "captions_burned": ass_on,
            "music": music_name, "sfx": [(round(t, 2), n) for t, _, n in sfx_list], "denoise": denoise, "snr": round(snr, 1),
            "norm": norm_info, "source_range": [round(S, 3), round(E, 3)], "out_words": out_words, "safe_mode": force_safe,
            "caption_style": cap.get("style")}


# ------------------------------------------------------------------ thumbnails
def make_thumbnail(video: Path, out: Path, t: float) -> None:
    ff(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", "-vf", "scale=540:-2", str(out)])


def thumbnail_with_text(video: Path, out: Path, text: str, t: float) -> None:
    from PIL import Image, ImageDraw, ImageFont
    tmp = out.with_suffix(".base.jpg")
    ff(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(tmp)])
    im = Image.open(tmp).convert("RGB")
    W, H = im.size
    font_file = subprocess.run(["fc-match", "-f", "%{file}", "Poppins:bold"], capture_output=True, text=True).stdout or ""
    try:
        font = ImageFont.truetype(font_file, int(W * 0.11))
    except Exception:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(im)
    words, lines, cur = text.upper().split(), [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textlength(test, font=font) > W * 0.88 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    y = int(H * 0.62)
    for ln in lines[:4]:
        x = (W - d.textlength(ln, font=font)) / 2
        d.text((x, y), ln, font=font, fill="#FFD400", stroke_width=max(3, W // 100), stroke_fill="black")
        y += int(font.size * 1.15) if hasattr(font, "size") else 40
    im.save(out, quality=90)
    tmp.unlink(missing_ok=True)
