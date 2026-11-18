"""Audio assets.

Built-in music and sound effects are *synthesized locally with ffmpeg* (sine/noise generators), so there is no
third-party copyright involved. They are deliberately simple. Users can upload their own licensed tracks in the
Editor / Settings; uploaded files are the user's responsibility.
"""
import subprocess
import threading

from . import db
from .config import cfg


def _chord_expr(progression, seconds_each=4.0, vol=0.05):
    """Sum of sines for each chord, windowed so chords breathe in and out (no clicks)."""
    total = seconds_each * len(progression)
    terms = []
    for k, freqs in enumerate(progression):
        ph = f"(mod(t,{total})-{k * seconds_each})"
        win = f"between(mod(t,{total}),{k * seconds_each},{(k + 1) * seconds_each})*pow(sin(PI*{ph}/{seconds_each}),0.6)"
        tone = "+".join(f"sin(2*PI*{f}*t)" for f in freqs)
        terms.append(f"{win}*({tone})")
    return f"{vol}*(" + "+".join(terms) + ")*(0.75+0.25*sin(2*PI*0.17*t))"


def _synth(name: str, args: list[str], out) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"asset synthesis failed for {name}: {r.stderr[-300:]}")


BUILTIN = {
    "music": {
        "Calm Pad": lambda out: _synth("calm", ["-f", "lavfi", "-i",
            "aevalsrc='" + _chord_expr([(110, 130.81, 164.81, 220), (87.31, 130.81, 174.61, 220), (130.81, 164.81, 196, 261.63),
                                        (98, 123.47, 146.83, 196)], 4.0, 0.07) + "':s=44100:d=64",
            "-af", "lowpass=f=2500,aecho=0.8:0.6:350:0.3,volume=1.2", "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
        "Upbeat Pulse": lambda out: _synth("pulse", ["-f", "lavfi", "-i",
            "aevalsrc='0.55*sin(2*PI*52*mod(t,0.5)*(1+2.5*exp(-28*mod(t,0.5))))*exp(-7*mod(t,0.5))"
            "+0.07*(random(0)*2-1)*exp(-45*mod(t+0.25,0.5))"
            "+0.18*sin(2*PI*(55*pow(2,floor(mod(t,4)/1)*0.0833))*t)*between(mod(t,0.5),0.0,0.35)*exp(-3*mod(t,0.5))"
            "+0.05*sin(2*PI*440*t)*between(mod(t,2),0,0.12)':s=44100:d=64",
            "-af", "highpass=f=40,lowpass=f=9000", "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
        "Tension Drone": lambda out: _synth("drone", ["-f", "lavfi", "-i",
            "aevalsrc='0.10*(sin(2*PI*55*t)+0.6*sin(2*PI*55.6*t)+0.4*sin(2*PI*82.4*t)+0.25*sin(2*PI*110.4*t))"
            "*(0.7+0.3*sin(2*PI*0.11*t))':s=44100:d=64",
            "-af", "lowpass=f=900,aecho=0.8:0.7:500:0.35", "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
    },
    "sfx": {
        "Whoosh": lambda out: _synth("whoosh", ["-f", "lavfi", "-i", "anoisesrc=d=0.7:c=pink:r=44100:a=0.9",
            "-af", "highpass=f=300,lowpass=f=5000,afade=t=in:d=0.3,afade=t=out:st=0.3:d=0.4,volume=1.4", "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
        "Pop": lambda out: _synth("pop", ["-f", "lavfi", "-i",
            "aevalsrc='0.8*sin(2*PI*(700+500*exp(-40*t))*t)*exp(-22*t)':s=44100:d=0.25",
            "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
        "Ding": lambda out: _synth("ding", ["-f", "lavfi", "-i",
            "aevalsrc='0.5*(sin(2*PI*1318.5*t)+0.5*sin(2*PI*1760*t)+0.25*sin(2*PI*2637*t))*exp(-5*t)':s=44100:d=1.0",
            "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
        "Impact": lambda out: _synth("impact", ["-f", "lavfi", "-i",
            "aevalsrc='0.9*sin(2*PI*(48+90*exp(-18*t))*t)*exp(-6*t)+0.25*(random(0)*2-1)*exp(-30*t)':s=44100:d=0.8",
            "-ac", "2", "-c:a", "aac", "-b:a", "128k"], out),
    },
}


READY = threading.Event()


def wait_ready(timeout: float = 90.0) -> bool:
    return READY.wait(timeout)


def ensure_builtin() -> None:
    try:
        _ensure_builtin()
    finally:
        READY.set()  # even on failure: renders then proceed without built-in audio rather than hang


def _ensure_builtin() -> None:
    cfg.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for kind, items in BUILTIN.items():
        for name, make in items.items():
            row = db.one("SELECT id, path FROM assets WHERE builtin=1 AND kind=? AND name=?", (kind, name))
            path = cfg.ASSET_DIR / f"builtin_{kind}_{name.lower().replace(' ', '_')}.m4a"
            if row and path.exists():
                continue
            if not path.exists():
                try:
                    make(path)
                except RuntimeError:
                    continue  # one failing generator must not break startup
            if not row:
                db.run("INSERT INTO assets(user_id,kind,name,path,builtin,created_at) VALUES(NULL,?,?,?,1,?)",
                       (kind, name, str(path), db.now()))


def list_assets(user_id: int) -> list[dict]:
    return db.q("SELECT id,kind,name,builtin,created_at FROM assets WHERE builtin=1 OR user_id=? ORDER BY builtin DESC, id", (user_id,))


def get_asset(asset_id, user_id: int):
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        return None
    return db.one("SELECT * FROM assets WHERE id=? AND (builtin=1 OR user_id=?)", (asset_id, user_id))


def named(kind: str, name: str):
    return db.one("SELECT * FROM assets WHERE builtin=1 AND kind=? AND name=?", (kind, name))


MUSIC_FOR = {"podcast": "Calm Pad", "educational": "Calm Pad", "other": "Calm Pad", "vlog": "Upbeat Pulse",
             "gaming": "Upbeat Pulse", "comedy": "Upbeat Pulse", "emotional": "Calm Pad", "controversial": "Tension Drone"}
