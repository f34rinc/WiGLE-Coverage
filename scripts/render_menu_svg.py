#!/usr/bin/env python3
"""Render the interactive menu into a self-contained SVG 'terminal screenshot' for the README.

Runs the tool's own `_menu_status` + `_menu_help` with color ON, captures the real ANSI output,
and turns it into a static SVG that renders inline on GitHub (no image host, no fonts/scripts to
fetch). Regenerate whenever the menu text or palette changes:

    python scripts/render_menu_svg.py        # writes docs/terminal-menu.svg

The panel is drawn from the actual code, so the screenshot can't drift from what the tool prints.
The `found` line is stubbed to a representative example so no real data folder is needed.
"""
import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wigle_coverage as w  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "terminal-menu.svg")

# ---- dark-terminal palette (maps the tool's ANSI codes to hex) --------------
FG_DEFAULT = "#c9d1d9"
FG_BOLD    = "#e6edf3"          # bold, uncolored text reads a touch brighter
DIM        = "#768390"
COLORS = {"31": "#f47067", "32": "#57ab5a", "33": "#e3b341", "35": "#bc8cff", "36": "#39c5cf"}

FS, CHARW, LINEH = 13, 7.81, 20        # monospace: advance ~0.6em
PADX, PADTOP, PADBOT, TITLEH = 20, 14, 18, 34
FONT = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"


def capture_menu():
    """The real menu output, with ANSI color on and a representative 'found' line."""
    w.C = w._C(True)                                   # force color codes
    # A real (empty) backup file so the panel's getmtime/basename work; name is the example shown.
    bk = os.path.join(tempfile.mkdtemp(), "WiGLE Database Backup.sqlite")
    open(bk, "w").close()
    w._detect_data = lambda d: (["a.kml", "b.kml", "c.kml"], [bk])     # stub: example only
    st = {"data": "./data", "cell_size": 50, "min_obs": 2, "hole_threshold": 5,
          "run_gap": 30, "track_gap": 5, "mode": "all", "run": None, "date": None,
          "pois": True, "poi_source": "overture"}
    buf = io.StringIO()
    with redirect_stdout(buf):
        w._menu_status(st)
        w._menu_help()
    return buf.getvalue().rstrip("\n") + "\n> █"   # add a faux prompt + cursor


def parse_ansi(text):
    """ANSI string -> list of lines, each a list of (text, fg, bold, dim) runs."""
    state = {"fg": FG_DEFAULT, "bold": False, "dim": False}
    lines, cur = [], []
    for tok in re.split(r"(\x1b\[[0-9;]*m)", text):
        if not tok:
            continue
        if tok.startswith("\x1b["):
            for code in tok[2:-1].split(";"):
                if code in ("", "0"):
                    state.update(fg=FG_DEFAULT, bold=False, dim=False)
                elif code == "1":
                    state["bold"] = True
                elif code == "2":
                    state["dim"] = True
                elif code in COLORS:
                    state["fg"] = COLORS[code]
            continue
        parts = tok.split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append(cur)
                cur = []
            if part:
                cur.append((part, dict(state)))
    lines.append(cur)
    return lines


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_svg(lines):
    maxlen = max((sum(len(t) for t, _ in ln) for ln in lines), default=40)
    width = round(PADX * 2 + maxlen * CHARW)
    height = round(TITLEH + PADTOP + len(lines) * LINEH + PADBOT)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}" font-size="{FS}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#1c2128" '
        f'stroke="#30363d"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{TITLEH}" rx="9" fill="#161b22"/>',
        f'<rect x="1" y="{TITLEH - 9}" width="{width - 2}" height="10" fill="#161b22"/>',
        f'<line x1="0" y1="{TITLEH}" x2="{width}" y2="{TITLEH}" stroke="#30363d"/>',
    ]
    for i, (cx, col) in enumerate(((18, "#ff5f56"), (38, "#ffbd2e"), (58, "#27c93f"))):
        out.append(f'<circle cx="{cx}" cy="{TITLEH // 2}" r="6" fill="{col}"/>')
    out.append(f'<text x="{width // 2}" y="{TITLEH // 2 + 4}" text-anchor="middle" '
               f'fill="{DIM}" font-size="12">python wigle_coverage.py</text>')

    for row, runs in enumerate(lines):
        y = TITLEH + PADTOP + (row + 1) * LINEH - 5
        spans = []
        for txt, s in runs:
            if s["fg"] == FG_DEFAULT:                  # uncolored: dim > bold > plain
                fill, opacity = (DIM, "") if s["dim"] else \
                                ((FG_BOLD, "") if s["bold"] else (FG_DEFAULT, ""))
            else:                                      # colored: keep hue, fade if dim
                fill, opacity = s["fg"], (' fill-opacity="0.75"' if s["dim"] else "")
            attrs = f'fill="{fill}"' + (' font-weight="700"' if s["bold"] else "") + opacity
            spans.append(f'<tspan {attrs}>{esc(txt)}</tspan>')
        out.append(f'<text x="{PADX}" y="{y}" xml:space="preserve">{"".join(spans)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    svg = to_svg(parse_ansi(capture_menu()))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {OUT}  ({len(svg):,} bytes)")


if __name__ == "__main__":
    main()
