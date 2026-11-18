"""Caption engine: word timings -> styled ASS subtitles (burned in by ffmpeg/libass), plus editing helpers.

Times inside `words` are relative to the *rendered output clip* when passed to build_ass().
"""
import re
import subprocess
from functools import lru_cache

from .transcribe import distribute_words


@lru_cache(maxsize=1)
def available_fonts() -> list[str]:
    """Font families libass/fontconfig can really use on this machine, curated to caption-friendly ones first."""
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ["DejaVu Sans"]
    fams = {f.split(",")[0].strip() for f in out.splitlines() if f.strip()}
    preferred = ["Poppins", "Inter", "Montserrat", "Roboto", "Oswald", "Anton", "Bebas Neue", "Open Sans", "Lato", "DejaVu Sans",
                 "Liberation Sans", "FreeSans"]
    got = [f for f in preferred if f in fams]
    return got or sorted(fams)[:10] or ["DejaVu Sans"]


def resolve_font(name: str) -> str:
    fonts = available_fonts()
    return name if name in fonts else fonts[0]


def _bgr(hexcolor: str, alpha: int = 0) -> str:
    h = (hexcolor or "#FFFFFF").lstrip("#")
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def max_chars_for(size: int, width: int, uppercase: bool) -> int:
    return max(8, int(0.84 * width / (size * (0.46 if uppercase else 0.41))))


def group_words(words: list[dict], per_line: int, max_chars: int) -> list[list[int]]:
    """Return groups of word indexes, one group per on-screen caption line."""
    groups, cur, chars = [], [], 0
    for i, w in enumerate(words):
        wl = len(w["w"]) + 1
        gap = words[i]["s"] - words[i - 1]["e"] if i else 0
        if cur and (len(cur) >= per_line or chars + wl > max_chars or gap > 1.0):
            groups.append(cur)
            cur, chars = [], 0
        cur.append(i)
        chars += wl
        if re.search(r"[.!?]$", w["w"]) and len(cur) >= 2:
            groups.append(cur)
            cur, chars = [], 0
    if cur:
        groups.append(cur)
    return groups


def lines_for_editing(words: list[dict], style: dict, width: int = 1080) -> list[dict]:
    size = int(style.get("size", 78))
    mc = max_chars_for(size, width, style.get("uppercase", True))
    out = []
    for g in group_words(words, int(style.get("words_per_line", 3)), mc):
        out.append({"i0": g[0], "i1": g[-1], "start": round(words[g[0]]["s"], 2), "end": round(words[g[-1]]["e"], 2),
                    "text": " ".join(words[i]["w"] for i in g)})
    return out


def apply_line_edits(words: list[dict], edits: list[dict]) -> list[dict]:
    """edits: [{i0,i1,text}] — replace the words in [i0..i1] with new text, keeping the line's time span."""
    by_i0 = {e["i0"]: e for e in edits}
    out, i = [], 0
    while i < len(words):
        e = by_i0.get(i)
        if not e:
            out.append(words[i])
            i += 1
            continue
        seg = words[e["i0"]:e["i1"] + 1]
        new_toks = str(e["text"]).split()
        if len(new_toks) == len(seg):
            out += [dict(w, w=nt) for w, nt in zip(seg, new_toks)]
        elif new_toks:
            nw = distribute_words(" ".join(new_toks), seg[0]["s"], seg[-1]["e"])
            keep_k = any(w.get("k") for w in seg)
            out += [dict(w, k=1) if keep_k and j == 0 else w for j, w in enumerate(nw)]
        i = e["i1"] + 1
    return out


def build_ass(words: list[dict], style: dict, out_w: int, out_h: int, offset: float = 0.0) -> str:
    st = style.get("style", "pop")
    size = int(style.get("size", 78) * out_w / 1080)
    font = resolve_font(style.get("font", "Poppins"))
    upper = bool(style.get("uppercase", True)) and st != "minimal"
    pos = style.get("position", "lower")
    align, margin_v = {"top": (8, 0.16), "middle": (5, 0.0), "lower": (2, 0.27), "bottom": (2, 0.13)}.get(pos, (2, 0.27))
    mv = int(out_h * margin_v)
    primary, hi, kw, outline = (_bgr(style.get(k, d)) for k, d in (("color", "#FFFFFF"), ("highlight", "#FFD400"),
                                                                    ("keyword_color", "#3DDC97"), ("outline", "#000000")))
    border = 3 if st == "classic" else 1
    outline_w = max(2, int(size * (0.07 if st != "minimal" else 0.04)))
    back = _bgr("#000000", 0x66) if st == "classic" else _bgr("#000000", 0xA0)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {out_w}\nPlayResY: {out_h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,"
        "StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Cap,{font},{size},{primary},{primary},{outline if st != 'classic' else back},{back},-1,0,0,0,100,100,0,0,"
        f"{border},{outline_w if st != 'classic' else int(size * 0.18)},{1 if st == 'pop' else 0},{align},{int(out_w * 0.07)},{int(out_w * 0.07)},{mv},1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    if not words:
        return header
    mc = max_chars_for(int(style.get("size", 78)), 1080, upper)
    lines = []
    groups = group_words(words, int(style.get("words_per_line", 3)), mc)
    for gi, g in enumerate(groups):
        gw = [words[i] for i in g]
        txts = [_esc(w["w"]).upper() if upper else _esc(w["w"]) for w in gw]
        nxt_group = words[groups[gi + 1][0]]["s"] + offset if gi + 1 < len(groups) else 1e9   # never overlap the next line
        if st == "pop":
            for j, w in enumerate(gw):
                s = w["s"] + offset
                nxt = gw[j + 1]["s"] + offset if j + 1 < len(gw) else min(w["e"] + offset + 0.22, nxt_group)
                e = max(nxt, s + 0.08)
                parts = []
                for k, (tw, t) in enumerate(zip(gw, txts)):
                    if k == j:
                        parts.append(f"{{\\1c{hi}\\fscx114\\fscy114}}{t}{{\\r}}")
                    elif tw.get("k"):
                        parts.append(f"{{\\1c{kw}}}{t}{{\\r}}")
                    else:
                        parts.append(t)
                lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Cap,,0,0,0,,{' '.join(parts)}")
        else:
            s = gw[0]["s"] + offset
            e = min(gw[-1]["e"] + offset + 0.15, nxt_group)
            parts = [f"{{\\1c{kw}}}{t}{{\\r}}" if (w.get("k") and st == "classic") else t for w, t in zip(gw, txts)]
            lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(lines) + "\n"


def remap_words(words: list[dict], segs: list[tuple[float, float, float]]) -> list[dict]:
    """words: relative to raw clip. segs: (src_start, src_end, out_start). Returns words on the output timeline."""
    out = []
    for w in words:
        mid = (w["s"] + w["e"]) / 2
        for a, b, o in segs:
            if a - 1e-6 <= mid <= b + 1e-6:
                s = max(w["s"], a) - a + o
                e = min(w["e"], b) - a + o
                if e > s:
                    out.append(dict(w, s=round(s, 3), e=round(e, 3)))
                break
    return out
