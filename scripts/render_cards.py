#!/usr/bin/env python3
"""Render the profile cards (animated SVG) into cards/ and write README.md.

    python scripts/render_cards.py            animated cards
    STATIC=1 python scripts/render_cards.py   final frame, no animation, plus
                                              cards/preview.html for screenshots
    SAMPLE=1 python scripts/render_cards.py   fake data, for design preview only

The cards are embedded with <img>, so they use only what works there: an inline
<style> with CSS keyframes, SMIL <animate>, and fonts subset with pyftsubset and
embedded as base64 WOFF data URIs. No scripts, no external requests.
Everything animated is switched off under prefers-reduced-motion.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC") == "1"
SAMPLE = os.environ.get("SAMPLE") == "1"
DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data" / "contributions.json"))
OUT = ROOT / "cards"

# --------------------------------------------------------------------------
# Config. Colours and fonts live here; change them and re-run.
# --------------------------------------------------------------------------
CFG = {
    "login": "ankitks1209",
    "name": "Ankit Singh",
    "tagline": "I build AI tools for chat, money and network security.",
    # Repos shown as cards, in order. Empty list = pinned repos, then most starred.
    "featured": ["Content-Chat-Bot", "Expense-tracker", "NetShield-ai", "ai-revenue-recovery"],
    "max_repos": 4,
    "portrait": True,  # uses assets/portrait.* when present
}

C = {
    "bg": "#F4F7FC",       # card surface
    "panel": "#FFFFFF",    # raised surface
    "ink": "#0E1A2F",      # text
    "muted": "#5B6781",    # secondary text
    "line": "#DCE3F0",     # borders, gridlines
    "empty": "#E6ECF7",    # empty contribution cell
    "accent": "#2D5BFF",   # brand colour (bars, graph, rules)
}

FONTS = {  # key: (family, weight, file)
    "d": ("Instrument Serif", 400, ROOT / "fonts" / "InstrumentSerif-Regular.ttf"),
    "s": ("IBM Plex Sans", 400, ROOT / "fonts" / "IBMPlexSans-Regular.ttf"),
    "b": ("IBM Plex Sans", 500, ROOT / "fonts" / "IBMPlexSans-Medium.ttf"),
}

LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#D4B72C", "HTML": "#E34C26", "TypeScript": "#3178C6",
    "CSS": "#7C4DBA", "Java": "#B07219", "C++": "#F34B7D", "C": "#6E6E6E", "Go": "#00ADD8",
    "Rust": "#C2865A", "Shell": "#4EAA25", "Jupyter Notebook": "#DA5B0B",
}

W = 900  # width of every full-width card

# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


RAMP = [C["empty"]] + [mix(C["empty"], C["accent"], t) for t in (0.32, 0.55, 0.78, 1.0)]

# --------------------------------------------------------------------------
# Fonts: measure with fontTools, subset with pyftsubset, embed as WOFF data URI
# --------------------------------------------------------------------------
_metrics: dict = {}
_faces: dict = {}


def _font(key: str):
    if key not in _metrics:
        path = FONTS[key][2]
        if not path.exists():
            sys.exit(f"Missing font file {path}. Keep the fonts/ folder in the repo.")
        t = TTFont(path)
        _metrics[key] = (t.getBestCmap(), t["hmtx"], t["head"].unitsPerEm)
    return _metrics[key]


def text_width(key: str, s: str, size: float) -> float:
    cmap, hmtx, upem = _font(key)
    return sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in s) * size / upem


def font_face(key: str, chars: str) -> str:
    """@font-face rule with only the glyphs this card uses (via pyftsubset)."""
    family, weight, path = FONTS[key]
    cmap, _, _ = _font(key)
    missing = sorted({c for c in chars if ord(c) not in cmap})
    if missing:
        print(f"  warning: {path.name} has no glyph for {missing}", file=sys.stderr)
    if (key, chars) not in _faces:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "subset.woff"
            subprocess.run(
                [sys.executable, "-m", "fontTools.subset", str(path), f"--text={chars}",
                 "--flavor=woff", "--no-hinting", "--drop-tables+=meta", "--layout-features=kern,liga",
                 f"--output-file={out}"],
                check=True,
            )
            _faces[(key, chars)] = base64.b64encode(out.read_bytes()).decode()
    return (f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:normal;"
            f"src:url(data:font/woff;base64,{_faces[(key, chars)]}) format('woff')}}")


def wrap(key: str, s: str, size: float, max_w: float, lines: int) -> list[str]:
    out, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and text_width(key, trial, size) > max_w:
            out.append(cur)
            cur = word
        else:
            cur = trial
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        last = out[-1]
        while " " in last and text_width(key, last + "...", size) > max_w:
            last = last.rsplit(" ", 1)[0]
        out[-1] = last + "..."
    return out


# --------------------------------------------------------------------------
# SVG building blocks
# --------------------------------------------------------------------------
BASE_CSS = (".fd{font-family:'Instrument Serif',Georgia,'Times New Roman',serif}"
            ".fs,.fb{font-family:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif}"
            ".fb{font-weight:500}")

MOTION_CSS = (
    ".st{display:none}"
    ".f,.g,.p,.v{transform-box:fill-box}"
    ".f{animation:fade .8s ease-out var(--d,0s) backwards}"
    ".g{transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.7,.2,1) var(--d,0s) backwards}"
    ".v{transform-origin:50% 0;animation:vgrow .8s cubic-bezier(.2,.7,.2,1) var(--d,0s) backwards}"
    ".p{transform-origin:50% 50%;animation:pop .5s cubic-bezier(.2,.8,.3,1.2) var(--d,0s) backwards}"
    ".ring{stroke-dasharray:var(--len);animation:draw 1.6s cubic-bezier(.4,0,.2,1) var(--d,0s) backwards}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    "@keyframes vgrow{from{transform:scaleY(0)}}"
    "@keyframes pop{from{opacity:0;transform:scale(.3)}}"
    "@keyframes draw{from{stroke-dashoffset:var(--len)}}"
    "@media (prefers-reduced-motion:reduce){"
    "*{animation:none!important}.mo{display:none}.st{display:inline}}"
)


def anim(d: float | None, kind: str = "f", cls: str = "", extra_style: str = "") -> str:
    """class/style attributes for a CSS-animated element (none in STATIC mode)."""
    k = "" if (STATIC or d is None) else kind
    klass = f"{cls} {k}".strip()
    out = f' class="{klass}"' if klass else ""
    if k:
        out += f' style="--d:{d:.2f}s;{extra_style}"'
    return out


class Card:
    def __init__(self, name: str, w: int, h: int, label: str):
        self.name, self.w, self.h, self.label = name, w, h, label
        self.body: list[str] = []
        self.chars = {k: set(" ") for k in FONTS}
        self.used: set[str] = set()

    def add(self, s: str) -> "Card":
        self.body.append(s)
        return self

    def text(self, x, y, s, font, size, fill, anchor="start", d=None, extra="") -> "Card":
        s = str(s)
        self.chars[font].update(s)
        self.used.add(font)
        return self.add(
            f'<text x="{x:g}" y="{y:g}"{anim(d, "f", "f" + font)} font-size="{size:g}" '
            f'fill="{fill}" text-anchor="{anchor}"{extra}>{html.escape(s)}</text>')

    def surface(self, fill=None, stroke=None, r=16) -> "Card":
        return self.add(
            f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{r}" '
            f'fill="{fill or C["bg"]}" stroke="{stroke or C["line"]}"/>')

    def sweep(self, x, y, w, h, fill, d, rx=0) -> "Card":
        """Bar that wipes in left to right. SMIL, with a static twin for reduced motion."""
        attrs = f'x="{x:g}" y="{y:g}" height="{h:g}" rx="{rx}" fill="{fill}"'
        if STATIC:
            return self.add(f'<rect {attrs} width="{w:g}"/>')
        self.add(f'<g class="mo"><rect {attrs} width="0"><animate attributeName="width" from="0" '
                 f'to="{w:g}" begin="{d:.2f}s" dur="0.9s" fill="freeze" calcMode="spline" '
                 f'keyTimes="0;1" keySplines=".2 .7 .2 1"/></rect></g>')
        return self.add(f'<g class="st"><rect {attrs} width="{w:g}"/></g>')

    def pulse(self, cx, cy, r, color, d) -> "Card":
        """Solid dot with a SMIL ripple. The ripple is decorative and drops out with reduced motion."""
        self.add(f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{color}"/>')
        if STATIC:
            return self
        common = f'dur="2.6s" begin="{d:.2f}s" repeatCount="indefinite" keyTimes="0;.7;1"'
        return self.add(
            f'<g class="mo"><circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{color}" opacity="0">'
            f'<animate attributeName="r" values="{r:g};{r * 3:g};{r * 3:g}" {common}/>'
            f'<animate attributeName="opacity" values=".5;0;0" {common}/></circle></g>')

    def svg(self) -> str:
        faces = "".join(font_face(k, "".join(sorted(self.chars[k]))) for k in sorted(self.used))
        css = BASE_CSS + ("" if STATIC else MOTION_CSS)
        label = html.escape(self.label)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{label}">'
                f'<title>{label}</title><style>{faces}{css}</style>' + "".join(self.body) + "</svg>")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def sample_data() -> dict:
    rnd = random.Random(7)
    end = dt.date.today()
    days = []
    for i in range(365):
        d = end - dt.timedelta(days=364 - i)
        n = 0 if rnd.random() < 0.58 else rnd.randint(1, 5)
        if d.weekday() >= 5:
            n = int(n * 0.4)
        if rnd.random() < 0.03:
            n += rnd.randint(8, 16)
        days.append({"date": d.isoformat(), "count": n})
    repos = [
        ("Content-Chat-Bot", "AI-powered content planning chatbot with FastAPI backend, OpenAI integration, "
         "local mock fallback, persistent chat history, and Markdown export.", "HTML", 0, 1),
        ("TUF", "assigmmnent", "HTML", 0, 0),
        ("Expense-tracker", "ExpenseBot(Enma) - AI-powered Expense Tracker, Type expenses in plain English. "
         "AI extracts the details. Dashboard shows you the trends.", "Python", 0, 0),
        ("NetShield-ai", "An AI-powered network anomaly detection platform that continuously monitors traffic, "
         "predicts intrusions, and generates real-time threat alerts.", "JavaScript", 0, 0),
        ("ai-revenue-recovery", "AI-powered failed-payment revenue recovery agent - Phase 1 foundation with "
         "synthetic data, classification, bounded recovery policies, and decision preview.", "Python", 0, 0),
    ]
    login = CFG["login"]
    return {
        "login": login, "name": CFG["name"], "to": days[-1]["date"], "total": sum(x["count"] for x in days),
        "days": days, "pinned": [],
        "repos": [{"name": n, "description": ds, "language": lg, "stars": st, "forks": fk,
                   "url": f"https://github.com/{login}/{n}"} for n, ds, lg, st, fk in repos],
    }


def load_data() -> dict:
    if SAMPLE:
        print("SAMPLE=1: using fake data. Do not commit these cards.", file=sys.stderr)
        return sample_data()
    if not DATA_FILE.exists():
        sys.exit(f"{DATA_FILE} not found. Run scripts/fetch_data.py first (or SAMPLE=1 for a design preview).")
    return json.loads(DATA_FILE.read_text())


def stats(days: list[dict]) -> dict:
    counts = [d["count"] for d in days]
    dates = [dt.date.fromisoformat(d["date"]) for d in days]
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:  # today may not have happened yet
        i -= 1
    cur = 0
    while i >= 0 and counts[i] > 0:
        cur += 1
        i -= 1
    best = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        best = max(best, run)
    bi = max(range(len(counts)), key=lambda k: (counts[k], k))
    last = dates[-1]
    months, y, m = [], last.year, last.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    totals = {k: 0 for k in months}
    for d, c in zip(dates, counts):
        if (d.year, d.month) in totals:
            totals[(d.year, d.month)] += c
    return {"current": cur, "longest": best, "active": sum(1 for c in counts if c > 0),
            "span": len(counts), "busiest": counts[bi], "busiest_date": dates[bi],
            "months": [(f"{dt.date(y, m, 1):%b}", totals[(y, m)]) for y, m in months], "last": last}


def fmt_date(d: dt.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def portrait_uri() -> str | None:
    if not CFG["portrait"]:
        return None
    for p in sorted((ROOT / "assets").glob("portrait.*")):
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower())
        if mime:
            return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
    return None


# --------------------------------------------------------------------------
# Cards. `t` is the base delay so the whole page plays top to bottom.
# --------------------------------------------------------------------------
def hero(data: dict, t: float) -> Card:
    name, tagline, login = data.get("name") or CFG["name"], CFG["tagline"], data["login"]
    c = Card("hero", W, 280, f"{name}. {tagline}")
    c.add(f'<defs><radialGradient id="gl" cx="84%" cy="50%" r="55%"><stop offset="0" stop-color="{C["accent"]}" '
          f'stop-opacity=".22"/><stop offset="1" stop-color="{C["accent"]}" stop-opacity="0"/></radialGradient>'
          f'<pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" '
          f'fill="{C["ink"]}" opacity=".08"/></pattern></defs>')
    c.surface()
    c.add(f'<rect x=".5" y=".5" width="{W - 1}" height="279" rx="16" fill="url(#dots)"/>')
    c.add(f'<rect x=".5" y=".5" width="{W - 1}" height="279" rx="16" fill="url(#gl)"/>')

    img = portrait_uri()
    avail = (670 if img else 860) - 56 - 24
    c.pulse(61, 66, 4.5, C["accent"], t + 0.2)
    c.text(76, 71, f"@{login}", "s", 15, C["muted"], d=t + 0.1)

    size = 100
    while size > 48 and text_width("d", name, size) > avail:
        size -= 2
    c.text(56, 168, name, "d", size, C["ink"], d=t + 0.25)
    c.sweep(56, 190, 96, 4, C["accent"], t + 0.7, rx=2)

    ts = 20
    while ts > 13 and text_width("s", tagline, ts) > avail:
        ts -= 0.5
    c.text(56, 236, tagline, "s", ts, C["ink"], d=t + 0.9)

    if img:
        cx, cy, r = 770, 140, 78
        circ = 2 * math.pi * (r + 12)
        c.add(f'<defs><clipPath id="cp"><circle cx="{cx}" cy="{cy}" r="{r}"/></clipPath></defs>')
        c.add(f'<g{anim(t + 0.5)}><image href="{img}" x="{cx - r}" y="{cy - r}" width="{2 * r}" height="{2 * r}" '
              f'clip-path="url(#cp)" preserveAspectRatio="xMidYMid slice"/></g>')
        c.add(f'<circle cx="{cx}" cy="{cy}" r="{r + 12}" fill="none" stroke="{C["accent"]}" stroke-width="2" '
              f'transform="rotate(-90 {cx} {cy})"{anim(t + 0.3, "ring", extra_style=f"--len:{circ:.1f};")}'
              f' stroke-dasharray="{circ:.1f}"/>')
    return c


def tile(i: int, label: str, value: str, caption: str, t: float) -> Card:
    c = Card(f"tile-{i}", 292, 124, f"{label}: {value} {caption}")
    c.surface(r=14)
    c.text(22, 34, label, "b", 14, C["muted"], d=t)
    c.text(22, 84, value, "d", 52, C["ink"], d=t + 0.1)
    c.text(22, 106, caption, "s", 13, C["muted"], d=t + 0.2)
    # link affordance: arrow in the corner
    c.add(f'<path d="M256 26 L270 26 L270 40 M270 26 L254 42" fill="none" stroke="{C["accent"]}" '
          f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"{anim(t + 0.3)}/>')
    c.sweep(0.5, 120, 291, 3.5, C["accent"], t + 0.25)
    return c


def nice_max(m: int) -> int:
    for cand in (4, 6, 8, 10, 12, 16, 20, 24, 30, 40, 50, 60, 80, 100, 120, 160, 200, 300, 400, 500,
                 600, 800, 1000, 1500, 2000, 3000, 4000, 5000, 6000, 8000, 10000):
        if m <= cand:
            return cand
    return int(math.ceil(m / 1000) * 1000)


def activity(st: dict, t: float) -> Card:
    months = st["months"]
    total = sum(v for _, v in months)
    h = 366
    c = Card("activity", W, h, f"Contributions per month: {total:,} in the last 12 months. "
             f"Current streak {st['current']} days, {st['active']} active days, busiest day {st['busiest']}.")
    c.surface()
    c.text(40, 62, "Contributions per month", "d", 34, C["ink"], d=t)
    c.text(40, 88, f"{total:,} in the last 12 months", "s", 14, C["muted"], d=t + 0.1)

    x0, x1, ytop, ybase = 78, 600, 132, 308
    ymax = nice_max(max(v for _, v in months))
    for frac in (0, 0.5, 1):
        y = ybase - frac * (ybase - ytop)
        dash = "" if frac == 0 else ' stroke-dasharray="3 5"'
        c.add(f'<line x1="{x0 - 8}" x2="{x1}" y1="{y:g}" y2="{y:g}" stroke="{C["line"]}"{dash}/>')
        c.text(x0 - 16, y + 4, f"{int(ymax * frac)}", "s", 11, C["muted"], anchor="end", d=t + 0.2)
    slot = (x1 - x0) / 12
    bw = 28
    peak = max(range(12), key=lambda k: months[k][1])
    for k, (lab, v) in enumerate(months):
        bx = x0 + slot * k + (slot - bw) / 2
        bh = max(3, v / ymax * (ybase - ytop))
        d = t + 0.35 + k * 0.07
        fill = C["accent"] if k == peak else mix(C["empty"], C["accent"], 0.55)
        c.add(f'<rect x="{bx:.1f}" y="{ybase - bh:.1f}" width="{bw}" height="{bh:.1f}" rx="5"{anim(d, "g")} fill="{fill}"/>')
        c.text(bx + bw / 2, ybase - bh - 8, v, "b" if k == peak else "s", 12,
               C["accent"] if k == peak else C["muted"], anchor="middle", d=d + 0.5)
        c.text(bx + bw / 2, ybase + 24, lab, "s", 12, C["muted"], anchor="middle", d=t + 0.3)

    c.add(f'<line x1="640" x2="640" y1="108" y2="338" stroke="{C["line"]}"/>')
    blocks = [
        ("Current streak", str(st["current"]), "days", f"longest {st['longest']} days"),
        ("Active days", str(st["active"]), f"of {st['span']}", f"{round(100 * st['active'] / st['span'])}% of days"),
        ("Busiest day", str(st["busiest"]), "contributions", fmt_date(st["busiest_date"])),
    ]
    for k, (lab, val, unit, cap) in enumerate(blocks):
        top, d = 102 + k * 84, t + 0.9 + k * 0.25
        c.text(668, top + 16, lab, "b", 14, C["muted"], d=d)
        if k == 2:
            c.pulse(668 + text_width("b", lab, 14) + 14, top + 11.5, 4, C["accent"], d + 0.4)
        c.text(668, top + 56, val, "d", 46, C["ink"], d=d + 0.1)
        c.text(668 + text_width("d", val, 46) + 8, top + 56, unit, "s", 14, C["muted"], d=d + 0.1)
        c.text(668, top + 72, cap, "s", 12, C["muted"], d=d + 0.2)
    return c


def icon_star(x, y, fill):
    pts = "6,0.5 7.6,4.3 11.7,4.6 8.5,7.2 9.5,11.2 6,9 2.5,11.2 3.5,7.2 0.3,4.6 4.4,4.3"
    return f'<polygon transform="translate({x:g} {y:g})" points="{pts}" fill="{fill}"/>'


def icon_fork(x, y, color):
    return (f'<g transform="translate({x:g} {y:g})" fill="none" stroke="{color}" stroke-width="1.4" stroke-linecap="round">'
            f'<circle cx="3" cy="2" r="1.8"/><circle cx="9" cy="2" r="1.8"/><circle cx="6" cy="10" r="1.8"/>'
            f'<path d="M3 3.8V5.4Q3 6.8 4.5 6.8H7.5Q9 6.8 9 5.4V3.8M6 6.8V8.2"/></g>')


def repo_card(r: dict, idx: int, t: float) -> Card:
    login = CFG["login"]
    c = Card(f"repo-{slug(r['name'])}", W, 176,
             f"{r['name']}: {r.get('description') or 'no description'}")
    c.surface()
    c.add(f'<rect x="0" y="28" width="4" height="118" rx="2" fill="{C["accent"]}"{anim(t, "v")}/>')
    c.text(36, 42, f"{login} / repository", "s", 13, C["muted"], d=t + 0.1)
    c.text(36, 84, r["name"], "d", 40, C["ink"], d=t + 0.2)
    lines = wrap("s", r.get("description") or "No description yet.", 15, 780, 2)
    for k, ln in enumerate(lines):
        c.text(36, 112 + k * 21, ln, "s", 15, C["muted"], d=t + 0.35 + k * 0.1)
    y = 154
    d = t + 0.5
    lang = r.get("language")
    x = 36
    if lang:
        col = LANG_COLORS.get(lang, C["muted"])
        c.add(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{col}"{anim(d)}/>')
        c.text(x + 16, y, lang, "s", 13, C["ink"], d=d)
        x += 16 + text_width("s", lang, 13) + 28
    c.add(f'<g{anim(d)}>{icon_star(x, y - 11, C["muted"])}</g>')
    stars = str(r.get("stars", 0))
    c.text(x + 18, y, stars, "s", 13, C["ink"], d=d)
    x += 18 + text_width("s", stars, 13) + 24
    c.add(f'<g{anim(d)}>{icon_fork(x, y - 11, C["muted"])}</g>')
    c.text(x + 18, y, str(r.get("forks", 0)), "s", 13, C["ink"], d=d)
    return c


def graph(days: list[dict], st: dict, t: float) -> Card:
    by_date = {d["date"]: d["count"] for d in days}
    end = st["last"]
    sidx = lambda d: (d.weekday() + 1) % 7  # Sunday = 0
    start = end - dt.timedelta(days=sidx(end)) - dt.timedelta(weeks=52)
    nz = sorted(v for v in by_date.values() if v > 0)
    q = [nz[int(len(nz) * f)] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]

    def level(n: int) -> int:
        return 0 if n == 0 else 1 if n <= q[0] else 2 if n <= q[1] else 3 if n <= q[2] else 4

    total = sum(by_date.values())
    h, gx, gy, cell, gap = 262, 26, 100, 13, 3
    c = Card("graph", W, h, f"Contribution graph: {total:,} contributions in the last year")
    c.surface()
    c.text(26, 50, "Contribution graph", "d", 34, C["ink"], d=t)
    c.text(874, 48, f"{total:,} contributions in the last year", "s", 14, C["muted"], anchor="end", d=t + 0.1)

    last_label = -9
    for col in range(53):
        cs = start + dt.timedelta(weeks=col)
        if col == 0 or cs.month != (cs - dt.timedelta(weeks=1)).month:
            if col - last_label >= 3 and col <= 50:
                c.text(gx + col * (cell + gap), gy - 10, f"{cs:%b}", "s", 12, C["muted"], d=t + 0.2)
                last_label = col
        cells = []
        for row in range(7):
            day = cs + dt.timedelta(days=row)
            if day > end:
                break
            n = by_date.get(day.isoformat(), 0)
            cells.append(f'<rect x="{gx + col * (cell + gap)}" y="{gy + row * (cell + gap)}" width="{cell}" '
                         f'height="{cell}" rx="3" fill="{RAMP[level(n)]}"/>')
        c.add(f'<g{anim(t + 0.3 + col * 0.03, "p")}>' + "".join(cells) + "</g>")

    ly = gy + 7 * (cell + gap) + 30
    c.text(26, ly, f"updated {fmt_date(end)}", "s", 12, C["muted"], d=t + 1.9)
    more_w = text_width("s", "More", 12)
    x = 874 - more_w
    c.text(874, ly, "More", "s", 12, C["muted"], anchor="end", d=t + 1.9)
    x -= 8 + 5 * (cell + 3) - 3
    for lv in range(5):
        c.add(f'<rect x="{x + lv * (cell + 3)}" y="{ly - 11}" width="{cell}" height="{cell}" rx="3" '
              f'fill="{RAMP[lv]}"{anim(t + 1.9)}/>')
    c.text(x - 8, ly, "Less", "s", 12, C["muted"], anchor="end", d=t + 1.9)
    return c


# --------------------------------------------------------------------------
# README
# --------------------------------------------------------------------------
def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pick_repos(data: dict) -> list[dict]:
    by_name = {r["name"]: r for r in data["repos"]}
    names = [n for n in CFG["featured"] if n in by_name] or [n for n in data.get("pinned", []) if n in by_name]
    if not names:
        names = [r["name"] for r in sorted(data["repos"], key=lambda r: -r.get("stars", 0))]
    return [by_name[n] for n in names[: CFG["max_repos"]]]


def readme(data: dict, tiles: list[tuple[str, str]], repos: list[dict], alts: list[str]) -> str:
    name = data.get("name") or CFG["name"]
    out = ["<!-- Generated by scripts/render_cards.py. Edits here are overwritten. -->",
           f'<p align="center">\n  <img src="cards/hero.svg" width="100%" alt="{html.escape(name)}: {html.escape(CFG["tagline"])}">\n</p>',
           '<p align="center">']
    for i, (url, alt) in enumerate(tiles, 1):
        out.append(f'  <a href="{url}"><img src="cards/tile-{i}.svg" width="32.5%" alt="{html.escape(alt)}"></a>')
    out.append("</p>")
    out.append(f'<p align="center">\n  <img src="cards/activity.svg" width="100%" alt="{html.escape(alts[0])}">\n</p>')
    for r in repos:
        out.append(f'<p align="center">\n  <a href="{r["url"]}"><img src="cards/repo-{slug(r["name"])}.svg" '
                   f'width="100%" alt="{html.escape(r["name"])}"></a>\n</p>')
    out.append(f'<p align="center">\n  <img src="cards/graph.svg" width="100%" alt="{html.escape(alts[1])}">\n</p>')
    return "\n".join(out) + "\n"


def main() -> None:
    data = load_data()
    days, login = data["days"], data["login"]
    st = stats(days)
    repos = pick_repos(data)
    langs: dict[str, int] = {}
    for r in data["repos"]:
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
    top = sorted(langs, key=lambda k: -langs[k])
    base = f"https://github.com/{login}"

    # Page timeline (seconds): hero, tiles, chart, repos, graph, one after the other.
    t_hero, t_tiles, t_chart = 0.0, 1.4, 2.4
    t_repos = t_chart + 2.6
    t_graph = t_repos + 0.75 * len(repos) + 0.4

    tile_specs = [
        ("Projects", str(len(data["repos"])), "public repositories", f"{base}?tab=repositories"),
        ("Contributions", f"{data['total']:,}", "in the last year", base),
        ("Languages", str(len(langs)), ", ".join(top[:3]) or "none yet",
         f"{base}?tab=repositories" + (f"&language={quote(top[0].lower())}" if top else "")),
    ]
    cards = [hero(data, t_hero)]
    for i, (lab, val, cap, _) in enumerate(tile_specs, 1):
        cards.append(tile(i, lab, val, cap, t_tiles + (i - 1) * 0.18))
    act = activity(st, t_chart)
    cards.append(act)
    for k, r in enumerate(repos):
        cards.append(repo_card(r, k, t_repos + k * 0.75))
    gr = graph(days, st, t_graph)
    cards.append(gr)

    OUT.mkdir(exist_ok=True)
    keep = {f"{c.name}.svg" for c in cards}
    for old in OUT.glob("*.svg"):
        if old.name not in keep:
            old.unlink()
    for c in cards:
        svg = c.svg()
        (OUT / f"{c.name}.svg").write_text(svg, encoding="utf-8")
        print(f"cards/{c.name}.svg  {len(svg) / 1024:.1f} KB")

    md = readme(data, [(u, f"{l}: {v} {cp}") for (l, v, cp, u) in tile_specs], repos, [act.label, gr.label])
    (ROOT / "README.md").write_text(md, encoding="utf-8")
    if STATIC:
        page = md.replace('src="cards/', 'src="')
        (OUT / "preview.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Profile preview</title>'
            '<body style="margin:24px auto;max-width:900px;background:#0d1117;line-height:0">' + page, encoding="utf-8")
        print("STATIC=1: final frame written; open cards/preview.html to screenshot. Don't commit preview.html.")


if __name__ == "__main__":
    main()
