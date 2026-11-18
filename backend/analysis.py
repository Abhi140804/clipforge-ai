"""Signal extraction: audio energy, silences, visual motion, scene cuts, face presence.

Everything is computed in streaming passes through ffmpeg so memory stays flat for long VODs.
All per-second arrays share the same index (second 0 .. ceil(duration)).
"""
import subprocess
from pathlib import Path

import cv2
import numpy as np

SR = 8000
HOP = 0.05  # 50 ms frames for the envelope


def audio_envelope(path: str, cancelled=lambda: False) -> np.ndarray:
    """RMS level in dBFS per 50 ms frame."""
    hs = int(SR * HOP)
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out, carry = [], b""
    while True:
        buf = p.stdout.read(hs * 2 * 4000)
        if not buf:
            break
        buf = carry + buf
        n = (len(buf) // (hs * 2)) * hs * 2
        carry = buf[n:]
        x = np.frombuffer(buf[:n], dtype=np.int16).astype(np.float32) / 32768.0
        if x.size:
            fr = x.reshape(-1, hs)
            out.append(20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-6))
        if cancelled():
            p.kill()
            break
    p.wait()
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def find_silences(db: np.ndarray, min_dur: float = 0.35, thr: float | None = None) -> list[tuple[float, float]]:
    if db.size == 0:
        return []
    if thr is None:
        thr = float(np.clip(np.percentile(db, 15) + 5, -60, -35))
    quiet = db < thr
    res, start = [], None
    for i, qv in enumerate(quiet):
        if qv and start is None:
            start = i
        elif not qv and start is not None:
            if (i - start) * HOP >= min_dur:
                res.append((round(start * HOP, 2), round(i * HOP, 2)))
            start = None
    if start is not None and (len(quiet) - start) * HOP >= min_dur:
        res.append((round(start * HOP, 2), round(len(quiet) * HOP, 2)))
    return res


def per_second(db: np.ndarray, n_sec: int) -> np.ndarray:
    """90th percentile of the 50 ms levels within each second (robust 'loudness')."""
    per = int(round(1 / HOP))
    out = np.full(n_sec, -90.0, dtype=np.float32)
    for s in range(n_sec):
        seg = db[s * per:(s + 1) * per]
        if seg.size:
            out[s] = np.percentile(seg, 90)
    return out


def detect_faces(gray):
    from . import detectors
    return detectors.detect_faces(gray)


def detect_face(gray: np.ndarray):
    """Largest face as (cx, cy, w, h) in pixels, or None."""
    faces = detect_faces(gray)
    return max(faces, key=lambda f: f[2] * f[3]) if faces else None


def visual_pass(path: str, width: int, height: int, duration: float, cancelled=lambda: False, face_every: int = 2):
    """1 fps low-res pass → motion, scene cuts, face presence & horizontal position."""
    fw = 320
    fh = max(2, int(round(fw * height / width / 2)) * 2)
    cmd = ["ffmpeg", "-v", "error", "-an", "-sn", "-i", path, "-vf", f"fps=1,scale={fw}:{fh}", "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n_sec = int(np.ceil(duration))
    motion = np.zeros(n_sec, dtype=np.float32)
    face = np.zeros(n_sec, dtype=np.float32)
    face_x = np.full(n_sec, np.nan, dtype=np.float32)
    diffs = np.zeros(n_sec, dtype=np.float32)
    prev, i = None, 0
    from . import detectors
    have_det = detectors.available()
    size = fw * fh
    while i < n_sec:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        g = np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw)
        if prev is not None:
            diffs[i] = float(np.abs(g.astype(np.int16) - prev.astype(np.int16)).mean()) / 255.0
        prev = g
        if have_det and i % face_every == 0:
            f = detect_face(g)
            if f:
                face[i] = 1.0
                face_x[i] = f[0] / fw
        i += 1
        if i % 120 == 0 and cancelled():
            p.kill()
            break
    p.kill()
    p.wait()
    # carry face presence across the skipped seconds
    for s in range(1, n_sec):
        if s % face_every != 0:
            face[s] = face[s - 1]
            face_x[s] = face_x[s - 1]
    motion = diffs
    thr = max(0.08, float(diffs.mean() + 3.5 * diffs.std())) if diffs.size > 5 else 0.2
    cuts = [int(s) for s in np.where(diffs > thr)[0] if s > 0]
    return {"motion": motion, "face": face, "face_x": face_x, "cuts": cuts}


def analyze(path: str, info: dict, cancelled=lambda: False, log=lambda m: None) -> dict:
    dur = info["duration"]
    n_sec = int(np.ceil(dur))
    out = {"duration": dur, "n_sec": n_sec}
    if info.get("has_audio"):
        log("Measuring audio energy and silences")
        db = audio_envelope(path, cancelled)
        sec = per_second(db, n_sec)
        valid = sec[sec > -89]
        mu, sd = (float(valid.mean()), float(valid.std() or 1.0)) if valid.size else (-60.0, 1.0)
        out["energy_db"] = sec
        out["energy_z"] = np.clip((sec - mu) / sd, -3, 4).astype(np.float32)
        out["silences"] = find_silences(db)
        out["mean_db"] = mu
        out["clip_ratio"] = float((db > -0.5).mean()) if db.size else 0.0
        out["db_frames"] = db
    else:
        out.update(energy_db=np.full(n_sec, -90.0, dtype=np.float32), energy_z=np.zeros(n_sec, dtype=np.float32),
                   silences=[], mean_db=-90.0, clip_ratio=0.0, db_frames=np.zeros(0))
    log("Analysing scenes, motion and faces")
    out.update(visual_pass(path, info["width"], info["height"], dur, cancelled))
    out["face_ratio"] = float(out["face"].mean()) if n_sec else 0.0
    return out


def window_stats(an: dict, s: float, e: float) -> dict:
    a, b = max(0, int(s)), min(an["n_sec"], int(np.ceil(e)))
    if b <= a:
        b = min(an["n_sec"], a + 1)
    z = an["energy_z"][a:b]
    mo = an["motion"][a:b]
    fc = an["face"][a:b]
    return {
        "energy_mean": float(z.mean()) if z.size else 0.0,
        "energy_peak": float(z.max()) if z.size else 0.0,
        "energy_std": float(z.std()) if z.size else 0.0,
        "motion": float(mo.mean()) if mo.size else 0.0,
        "face": float(fc.mean()) if fc.size else 0.0,
        "mean_db": float(an["energy_db"][a:b].mean()) if b > a else -90.0,
        "cuts": sum(1 for c in an["cuts"] if a <= c < b),
    }


def downsample_signals(an: dict, points: int = 300) -> dict:
    n = an["n_sec"]
    step = max(1, int(np.ceil(n / points)))
    def ds(x):
        arr = np.nan_to_num(np.asarray(x, dtype=np.float32))
        return [round(float(arr[i:i + step].mean()), 3) for i in range(0, n, step)]
    return {"step": step, "energy": ds(an["energy_z"]), "motion": ds(an["motion"]), "face": ds(an["face"]),
            "cuts": an["cuts"][:500], "silences": [list(s) for s in an["silences"][:2000]], "duration": an["duration"]}
