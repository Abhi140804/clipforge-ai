"""Generate a synthetic two-speaker test video + SRT + ground truth.

No real speech: the audio is a harmonic 'voice' with syllable-rate amplitude modulation, so envelope/silence/energy
analysis and mouth-motion based active-speaker tracking can be exercised end-to-end without proprietary media.
Usage: python make_sample.py OUT_DIR [seconds]
"""
import json
import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np

W, H, FPS, SR = 1280, 720, 30, 48000

SCRIPT = [  # (speaker, text, loud, pause_after)
    ("L", "Why do most startups fail in the first year?", 0.8, 0.5),
    ("R", "I analyzed three hundred companies and the answer honestly shocked me.", 0.9, 1.4),
    ("L", "So what was it, money?", 0.7, 0.4),
    ("R", "It was never about money. It was about talking to customers.", 1.0, 0.6),
    ("R", "Nobody wants to hear this, but the founders who shipped weekly survived.", 0.9, 0.5),
    ("L", "That sounds almost too simple.", 0.6, 0.4),
    ("R", "It is simple! The hard part is doing it every single week without fail.", 1.0, 1.5),
    ("L", "Okay here is a random sentence about the weather being fine today.", 0.4, 0.5),
    ("R", "Let me explain the first rule. Talk to five customers every week.", 0.8, 0.5),
    ("R", "The second rule is to ship something small, because feedback beats planning.", 0.8, 1.2),
    ("L", "I did not expect that to work, wow!", 1.0, 0.4),
    ("R", "Trust me, I have seen it forty times, and the pattern never changes.", 0.9, 0.5),
    ("L", "What happens if you skip the weekly part?", 0.7, 0.5),
    ("R", "Then you drift. Six months later you are building something nobody asked for.", 1.0, 1.3),
    ("L", "That is the scariest thing I have heard all week.", 1.0, 0.5),
    ("R", "And that is why the best advice is the most boring advice you will ever get.", 0.9, 1.0),
    ("L", "Thanks for explaining that so clearly, this was great.", 0.6, 0.6),
    ("R", "My pleasure. Remember, talk to customers and ship every week.", 0.8, 1.0),
]


def build(out: Path, total: float = 150.0):
    out.mkdir(parents=True, exist_ok=True)
    n = int(total * SR)
    audio = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(7)
    audio += (rng.standard_normal(n) * 0.0025).astype(np.float32)  # faint room noise
    t0 = 1.0
    cues, truth, k = [], [], 1
    i = 0
    while t0 < total - 8:
        spk, text, loud, pause = SCRIPT[i % len(SCRIPT)]
        i += 1
        words = text.split()
        dur = max(1.6, len(words) * 0.36)
        if t0 + dur > total - 2:
            break
        f0 = 118 if spk == "R" else 190
        t = np.arange(int(dur * SR)) / SR
        voiced = sum(np.sin(2 * np.pi * f0 * h * t + h) / h for h in range(1, 12))
        syl = 0.55 + 0.45 * np.abs(np.sin(2 * np.pi * 2.6 * t))
        env = np.minimum(1, t / 0.05) * np.minimum(1, (dur - t) / 0.08)
        seg = (0.16 * loud * voiced * syl * env).astype(np.float32)
        a = int(t0 * SR)
        audio[a:a + seg.size] += seg
        cues.append((t0, t0 + dur, text))
        truth.append({"start": t0, "end": t0 + dur, "spk": spk, "text": text, "loud": loud})
        t0 += dur + pause
    peak = np.abs(audio).max()
    audio = audio / peak * 0.85
    wav = out / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())

    def ts(x):
        h, r = divmod(x, 3600)
        m, s = divmod(r, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"

    (out / "sample.srt").write_text("\n".join(f"{j + 1}\n{ts(a)} --> {ts(b)}\n{tx}\n" for j, (a, b, tx) in enumerate(cues)))

    mp4 = out / "sample.mp4"
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                          "-i", str(wav), "-t", str(total), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-b:a", "128k", "-shortest", str(mp4)], stdin=subprocess.PIPE)
    pos = {"L": (330, 330), "R": (950, 330)}
    rad = 120
    for f in range(int(total * FPS)):
        tt = f / FPS
        scene = int(tt // 25)
        base = np.zeros((H, W, 3), np.uint8)
        c1 = np.array([(40 + 50 * scene) % 255, 70, 120 - 10 * scene], np.uint8)
        c2 = np.array([20, (90 + 30 * scene) % 255, 60 + 20 * scene], np.uint8)
        grad = np.linspace(0, 1, H)[:, None, None]
        base[:] = (c1 * (1 - grad) + c2 * grad).astype(np.uint8)
        active = next((c["spk"] for c in truth if c["start"] <= tt <= c["end"]), None)
        for spk, (cx, cy) in pos.items():
            cv2.circle(base, (cx, cy), rad, (170, 200, 235), -1)
            cv2.circle(base, (cx, cy), rad, (60, 80, 120), 3)
            for dx in (-45, 45):
                cv2.circle(base, (cx + dx, cy - 35), 16, (255, 255, 255), -1)
                cv2.circle(base, (cx + dx, cy - 35), 7, (30, 30, 30), -1)
            cv2.line(base, (cx - 70, cy - 70), (cx - 25, cy - 75), (40, 40, 40), 5)
            cv2.line(base, (cx + 25, cy - 75), (cx + 70, cy - 70), (40, 40, 40), 5)
            open_amt = int(6 + 30 * (0.5 + 0.5 * np.sin(2 * np.pi * 5.5 * tt))) if spk == active else 4
            cv2.ellipse(base, (cx, cy + 55), (42, open_amt), 0, 0, 360, (30, 30, 150), -1)
            cv2.rectangle(base, (cx - 90, cy + rad + 20), (cx + 90, H), (80, 60, 50), -1)
        p.stdin.write(base.tobytes())
    p.stdin.close()
    p.wait()
    (out / "truth.json").write_text(json.dumps({"cues": truth, "faces": {k: v for k, v in pos.items()}, "W": W, "H": H}))
    return mp4


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "sample")
    print(build(d, float(sys.argv[2]) if len(sys.argv) > 2 else 150.0))
