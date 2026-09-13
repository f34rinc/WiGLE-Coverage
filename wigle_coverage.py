#!/usr/bin/env python3
"""
wigle_coverage.py - turn your WiGLE exports into a coverage map that shows where
you HAVE walked and, more usefully, recommends where you HAVEN'T: the blank cells
on the frontier of your footprint (walk outward) and the holes inside it (streets
you skipped). Output is a self-contained local Leaflet HTML map.

DRAG-AND-DROP (Windows): drop one or more .kml / WiGLE .csv files - or the whole
folder - onto this script. It unions everything, builds the map, and opens it in
your browser. Coverage is most meaningful from the UNION of all your exports, so
dragging the whole "WiGLE data" folder (or selecting all the KMLs) is the sweet
spot; a single run-KML just maps that one run.

CLI:
    python wigle_coverage.py "<dir or file(s)>" [--cell-size 100] [--min-obs 2]
                             [--hole-threshold 5] [--out map.html] [--no-open]

PRIVACY: inputs and the generated map carry real GPS - they stay LOCAL and are
git-ignored. Nothing here is uploaded or published.

stdlib only. Leaflet + CARTO/Esri basemap tiles load in your browser at view time.
"""
import os
import re
import sys
import json
import math
import glob
import argparse
import datetime
import webbrowser

# ---- config defaults --------------------------------------------------------
CELL_SIZE_M   = 100     # grid cell edge in metres (~one city block)
MIN_OBS       = 2       # APs in a cell before it counts as "covered" (filters strays)
HOLE_THRESHOLD = 5      # covered 8-neighbours at/above this => "hole", else "edge"
EARTH_M_PER_DEG = 111320.0
# -----------------------------------------------------------------------------


# ---- geometry / grid (pure, unit-tested) ------------------------------------
def meters_to_deg(cell_m, latitude):
    """Cell edge in metres -> (dlat, dlon) in degrees at the given latitude.
    Longitude degrees shrink away from the equator, so dlon > dlat here."""
    dlat = cell_m / EARTH_M_PER_DEG
    dlon = cell_m / (EARTH_M_PER_DEG * math.cos(math.radians(abs(latitude))))
    return dlat, dlon


def cell_of(lat, lon, dlat, dlon):
    """Grid cell (row, col) a coordinate falls in."""
    return (math.floor(lat / dlat), math.floor(lon / dlon))


def build_coverage(points, dlat, dlon):
    """points: iterable of (lat, lon) -> dict {(row,col): observation_count}."""
    cov = {}
    for lat, lon in points:
        key = cell_of(lat, lon, dlat, dlon)
        cov[key] = cov.get(key, 0) + 1
    return cov


def covered_cells(coverage, min_obs):
    """Set of cells with at least `min_obs` observations."""
    return {cell for cell, n in coverage.items() if n >= min_obs}


_NEIGHBORS = [(dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)]


def recommend(coverage, min_obs, hole_threshold):
    """Blank cells worth walking next: the uncovered 8-neighbours of your covered
    cells, labelled 'hole' (surrounded - a skipped street) or 'edge' (the expanding
    frontier), ranked holes-first then by covered-neighbour count. Scans only cells
    adjacent to coverage - O(covered), not O(bounding box) - so it stays fast even
    when coverage is spread across a whole metro."""
    covered = covered_cells(coverage, min_obs)
    if not covered:
        return []
    candidates = set()
    for (r, c) in covered:
        for dr, dc in _NEIGHBORS:
            nb = (r + dr, c + dc)
            if nb not in covered:
                candidates.add(nb)
    recs = []
    for (r, c) in candidates:
        n = sum(((r + dr, c + dc) in covered) for dr, dc in _NEIGHBORS)
        recs.append({"cell": (r, c), "label": "hole" if n >= hole_threshold else "edge",
                     "covered_neighbors": n})
    recs.sort(key=lambda x: (x["label"] != "hole", -x["covered_neighbors"]))
    return recs


# ---- input parsing ----------------------------------------------------------
_KML_COORD = re.compile(r"<coordinates>\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")


def parse_kml(path):
    """Yield (lat, lon) from every <Point><coordinates>lon,lat[,alt]. WiGLE KML
    placemarks are Points, so this captures one location per logged network."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    for lon, lat in _KML_COORD.findall(text):     # KML order is lon,lat
        yield float(lat), float(lon)


def parse_wigle_csv(path):
    """Yield (lat, lon) from a WiGLE .csv (CurrentLatitude / CurrentLongitude)."""
    with open(path, encoding="utf-8", errors="ignore") as fh:
        lines = fh.read().splitlines()
    hdr_idx = next((i for i, ln in enumerate(lines)
                    if "CurrentLatitude" in ln and "CurrentLongitude" in ln), None)
    if hdr_idx is None:
        return
    cols = [c.strip() for c in lines[hdr_idx].split(",")]
    try:
        la, lo = cols.index("CurrentLatitude"), cols.index("CurrentLongitude")
    except ValueError:
        return
    for ln in lines[hdr_idx + 1:]:
        parts = ln.split(",")
        if len(parts) <= max(la, lo):
            continue
        try:
            lat, lon = float(parts[la]), float(parts[lo])
        except ValueError:
            continue
        if lat or lon:
            yield lat, lon


def parse_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".kml", ".kmz"):
        return parse_kml(path)
    if ext == ".csv":
        return parse_wigle_csv(path)
    return iter(())


def expand_inputs(paths):
    """Turn dropped args (files, folders, globs) into a flat list of KML/CSV files."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for ext in ("*.kml", "*.csv"):
                out += glob.glob(os.path.join(p, ext))
        elif any(ch in p for ch in "*?[]"):
            out += glob.glob(p)
        elif os.path.isfile(p):
            out.append(p)
        else:
            print(f"  !! not found: {p}", file=sys.stderr)
    # de-dupe while preserving order
    seen, uniq = set(), []
    for f in out:
        k = os.path.abspath(f).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


# ---- HTML rendering ---------------------------------------------------------
def _cov_color(count):
    if count >= 30:
        return "#0b525b"
    if count >= 10:
        return "#1c7c8c"
    if count >= 3:
        return "#5aa7a7"
    return "#a9d6d6"


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
 integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="anonymous"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
 integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin="anonymous"></script>
<style>
  html,body,#map{height:100%;margin:0}
  .legend{background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);
          font:13px system-ui,sans-serif;line-height:1.5}
  .legend b{display:block;margin-bottom:4px}
  .sw{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:-1px;border:1px solid #0003}
</style></head><body><div id="map"></div>
<script>
const D = __DATA__;
const map = L.map('map');
// OSM's own servers block file:// requests and CARTO now nags without an API key,
// so use Esri's keyless, nag-free basemaps (fine from a local file).
const baseStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:19, attribution:'Tiles &copy; Esri'});
const baseGray = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:16, attribution:'Tiles &copy; Esri'});
const baseSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:19, attribution:'Tiles &copy; Esri, Maxar, Earthstar Geographics'});
baseStreet.addTo(map);
L.control.layers({'Streets (Esri)': baseStreet, 'Light gray': baseGray, 'Satellite': baseSat},
  null, {position:'topright'}).addTo(map);

function bounds(r,c){ return [[r*D.dlat, c*D.dlon], [(r+1)*D.dlat, (c+1)*D.dlon]]; }
function covColor(n){ return n>=30?'#0b525b':n>=10?'#1c7c8c':n>=3?'#5aa7a7':'#a9d6d6'; }

// coverage (where you've been)
D.covered.forEach(([r,c,n])=>{
  L.rectangle(bounds(r,c), {stroke:false, fillColor:covColor(n), fillOpacity:.55})
   .bindPopup('Covered &middot; '+n+' networks').addTo(map);
});
// recommendations (where to go next)
const RC = {hole:'#dc2626', edge:'#f59e0b'};
D.recs.forEach(([r,c,label,nb])=>{
  const b = bounds(r,c);
  L.rectangle(b, {color:RC[label], weight:2, fillColor:RC[label], fillOpacity:.35})
   .bindPopup('<b>'+(label==='hole'?'Hole (skipped)':'Edge (frontier)')+'</b><br>'
     +nb+' covered neighbours<br>center '
     +((b[0][0]+b[1][0])/2).toFixed(5)+', '+((b[0][1]+b[1][1])/2).toFixed(5)).addTo(map);
});

map.fitBounds(D.fit);

const lg = L.control({position:'bottomright'});
lg.onAdd = function(){ const d=L.DomUtil.create('div','legend');
  d.innerHTML = '<b>__TITLE__</b>'
   + '<div><span class="sw" style="background:#0b525b"></span>covered (dense &rarr; light)</div>'
   + '<div><span class="sw" style="background:#dc2626"></span>hole &ndash; skipped street</div>'
   + '<div><span class="sw" style="background:#f59e0b"></span>edge &ndash; walk outward</div>'
   + '<div style="margin-top:4px;color:#555">'+D.covered.length+' covered cells &middot; '
   + D.recs.length+' suggestions</div>';
  return d; };
lg.addTo(map);
</script></body></html>"""


def render_html(coverage, recs, dlat, dlon, min_obs, out_path):
    covered = covered_cells(coverage, min_obs)
    cov_list = [[r, c, coverage[(r, c)]] for (r, c) in covered]
    rec_list = [[x["cell"][0], x["cell"][1], x["label"], x["covered_neighbors"]] for x in recs]
    all_cells = list(covered) + [x["cell"] for x in recs]
    rows = [r for r, _ in all_cells] or [0]
    cols = [c for _, c in all_cells] or [0]
    fit = [[min(rows) * dlat, min(cols) * dlon],
           [(max(rows) + 1) * dlat, (max(cols) + 1) * dlon]]
    data = {"dlat": dlat, "dlon": dlon, "covered": cov_list, "recs": rec_list, "fit": fit}
    title = "WiGLE coverage &amp; frontier"
    html = _HTML.replace("__DATA__", json.dumps(data)).replace("__TITLE__", title)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


# ---- drag-and-drop / console plumbing ---------------------------------------
_SHELLS = {"cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe",
           "zsh.exe", "fish.exe", "mintty.exe", "wt.exe", "windowsterminal.exe"}


def _launched_standalone():
    """True when double-clicked / drag-dropped on Windows (an explorer ancestor
    is reached before any shell), so the console would vanish on exit."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (0, -1):
            return False
        parent, name = {}, {}
        e = PE(); e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            parent[e.th32ProcessID] = e.th32ParentProcessID
            name[e.th32ProcessID] = e.szExeFile.decode(errors="ignore").lower()
            ok = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        pid, seen = os.getpid(), set()
        while pid in parent and pid not in seen:
            seen.add(pid)
            pid = parent[pid]
            nm = name.get(pid, "")
            if nm in _SHELLS:
                return False
            if nm == "explorer.exe":
                return True
    except Exception:
        pass
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Map WiGLE coverage and recommend where to walk next.")
    p.add_argument("paths", nargs="*",
                   help="KML/CSV file(s), a folder, or globs (drag-drop friendly)")
    p.add_argument("--cell-size", type=float, default=CELL_SIZE_M,
                   help=f"grid cell edge in metres (default {CELL_SIZE_M})")
    p.add_argument("--min-obs", type=int, default=MIN_OBS,
                   help=f"networks in a cell before it counts as covered (default {MIN_OBS})")
    p.add_argument("--hole-threshold", type=int, default=HOLE_THRESHOLD,
                   help=f"covered neighbours for a 'hole' vs 'edge' (default {HOLE_THRESHOLD})")
    p.add_argument("--out", help="output HTML path (default: beside the first input)")
    p.add_argument("--no-open", action="store_true", help="don't auto-open the map")
    return p.parse_args()


def run(args):
    files = expand_inputs(args.paths)
    if not files:
        print("No KML/CSV inputs. Drop the 'WiGLE data' folder (or files) onto this "
              "script, or pass paths on the command line.")
        return None
    print(f"reading {len(files)} file(s)...")
    raw = []
    for f in files:
        n0 = len(raw)
        raw.extend(parse_any(f))
        print(f"  {os.path.basename(f)}: {len(raw) - n0:,} points")
    # Drop invalid / null-island coordinates - a single (0,0) or out-of-range fix
    # would otherwise stretch the map extent from Rio to the Atlantic.
    points = [(la, lo) for (la, lo) in raw
              if -90 <= la <= 90 and -180 <= lo <= 180 and not (la == 0 and lo == 0)]
    dropped = len(raw) - len(points)
    if dropped:
        print(f"  dropped {dropped:,} invalid/zero coordinates")
    if not points:
        print("No usable coordinates found in those files.")
        return None

    mean_lat = sum(p[0] for p in points) / len(points)
    dlat, dlon = meters_to_deg(args.cell_size, mean_lat)
    print(f"gridding {len(points):,} points into ~{args.cell_size:.0f} m cells...")
    coverage = build_coverage(points, dlat, dlon)
    covered = covered_cells(coverage, args.min_obs)
    print(f"  {len(covered):,} covered cells; finding recommendations...")
    recs = recommend(coverage, args.min_obs, args.hole_threshold)
    holes = sum(1 for r in recs if r["label"] == "hole")
    edges = len(recs) - holes
    print(f"  {holes:,} holes + {edges:,} edges; rendering map...")

    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(files[0])),
        f"wigle_coverage_{datetime.date.today():%Y%m%d}.html")
    render_html(coverage, recs, dlat, dlon, args.min_obs, out)

    print(f"\n  {len(points):,} points  ->  {len(covered):,} covered cells "
          f"(~{args.cell_size:.0f} m)")
    print(f"  recommendations: {holes} holes (skipped) + {edges} edges (frontier)")
    print(f"  map: {out}")
    return out


def main():
    args = parse_args()
    out = run(args)
    if out and not args.no_open:
        try:
            webbrowser.open("file://" + os.path.abspath(out))
        except Exception:
            pass
    if _launched_standalone():
        try:
            input("\nDone. Press Enter to close.")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
