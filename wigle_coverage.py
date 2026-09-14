#!/usr/bin/env python3
"""
wigle_coverage.py - turn your WiGLE exports into a coverage map that shows where
you HAVE walked and, more usefully, recommends where you HAVEN'T: the blank cells
on the frontier of your footprint (walk outward) and the holes inside it (streets
you skipped). Output is a self-contained local Leaflet HTML map.

DRAG-AND-DROP (Windows): drop .kml / WiGLE .csv files, a .sqlite backup, or the whole
folder onto this script. KML/CSV give coverage; a SQLite backup adds your path (and,
by itself, is a full coverage+path map). It unions everything, builds the map, and
opens it. Dragging the whole "WiGLE data" folder is the sweet spot - it auto-uses the
KMLs for coverage and the newest .sqlite backup for the track.

NO ARGS / MENU: run with nothing (or -i / --menu) for an interactive terminal menu
to set options and pick a run. With no path given, the tool reads a "data" folder
beside this script (./data) - drop your KML/CSV + .sqlite backup there once and just
run it. Override the folder with --data DIR.

CLI:
    python wigle_coverage.py "<dir or file(s)>" [--cell-size 50] [--min-obs 2]
                             [--hole-threshold 5] [--track "WiGLE Database Backup"]
                             [--data DIR] [-i] [--out map.html] [--no-open]

YOUR ACTUAL PATH: pass --track pointing at a WiGLE SQLite backup and the map adds a
toggle-able blue polyline of where you actually walked (raw GPS fixes from its
`location` table, split into segments on time gaps). The cells come from the KML/CSV;
the track from the SQLite. The backup also works as a positional/dropped input - on
its own it drives BOTH coverage and path (the entire-DB view).

HISTORICAL RUNS (SQLite only, since a KML has no timestamps):
    --list-runs                list the sessions in the backup (index, date, span, fixes)
    --run N                    map just that session's coverage + path
    --date 2026-09-13          map just that local date
    (with only --track and no run flag: the ENTIRE-DB view - all your history at once)

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
CELL_SIZE_M   = 50      # grid cell edge in metres (~half a block; near the GPS floor)
MIN_OBS       = 2       # APs in a cell before it counts as "covered" (filters strays)
HOLE_THRESHOLD = 5      # covered 8-neighbours at/above this => "hole", else "edge"
TRACK_GAP_MIN  = 5      # minutes; a larger gap between fixes starts a new track segment
TRACK_MIN_MOVE_M = 5    # drop track fixes closer than this to the last kept one (jitter)
RUN_GAP_MIN    = 30     # minutes of quiet that separates one run/session from the next
EARTH_M_PER_DEG = 111320.0
# Default folder read when no path/--track is given: a "data" folder beside this script
# (git-ignored). Drop your KML/CSV + .sqlite backup here and just run the tool.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Optional map API key: a git-ignored map_key.txt beside this script. When present it
# unlocks the keyed OSM (Stadia) basemap in the map's layer switcher; when absent that
# option simply isn't offered. Your own key, never committed - see map_key.txt.example.
MAP_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_key.txt")


def read_map_key():
    """The API key from map_key.txt (first non-comment, non-blank line), or None."""
    try:
        with open(MAP_KEY_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return None


# ---- console colour (cross-platform; degrades to plain text) -----------------
class _C:
    def __init__(self, on):
        e = (lambda s: s if on else "")
        self.reset = e("\033[0m"); self.b = e("\033[1m"); self.dim = e("\033[2m")
        self.cyan = e("\033[36m"); self.yellow = e("\033[33m")
        self.green = e("\033[32m"); self.red = e("\033[31m")


def setup_color():
    """Enable ANSI colour when writing to a real terminal (honours NO_COLOR); on
    Windows this flips the console into virtual-terminal mode."""
    on = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
    if on and os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_...VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            on = False
    return _C(on)


C = _C(False)          # replaced by setup_color() at startup
_RULE_W = 56


def _rule(char="=", label=""):
    if label:
        head = f"-- {label} "
        return C.dim + head + "-" * max(0, _RULE_W - len(head)) + C.reset
    return C.dim + char * _RULE_W + C.reset
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


def _is_sqlite(path):
    """True if the file starts with the SQLite magic (handles a backup with no
    extension, like 'WiGLE Database Backup')."""
    try:
        with open(path, "rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def expand_inputs(paths):
    """Turn dropped args (files, folders, globs) into a flat list of input files -
    KML/CSV for coverage and SQLite backups for track/run views. A folder is
    scanned for all of them so drag-dropping the WiGLE data folder 'just works'."""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for pat in ("*.kml", "*.csv", "*.sqlite", "*.db", "*Database Backup*"):
                out += glob.glob(os.path.join(p, pat))
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


# ---- track (your actual path, from the WiGLE SQLite backup) -----------------
def parse_sqlite_track(path):
    """Timestamped GPS fixes from a WiGLE SQLite backup's `location` table, sorted
    by time (epoch ms). Returns [(time_ms, lat, lon), ...]. Opened read-only; skips
    time=0 / null-island / out-of-range rows."""
    import sqlite3
    import urllib.request
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT time, lat, lon FROM location "
            "WHERE time > 0 AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 "
            "AND NOT (lat = 0 AND lon = 0) ORDER BY time").fetchall()
    finally:
        con.close()
    return [(int(t), float(la), float(lo)) for (t, la, lo) in rows]


def build_track_segments(rows, gap_ms, min_move_deg):
    """Split time-sorted (time, lat, lon) fixes into polyline segments: break on a
    time gap (so separate walks don't join with a straight line) and drop fixes that
    barely moved (GPS jitter). Returns [[[lat,lon],...], ...]."""
    segs, cur, last_t, last = [], [], None, None
    for (t, la, lo) in rows:
        if last_t is not None and t - last_t > gap_ms:
            if len(cur) >= 2:
                segs.append(cur)
            cur, last = [], None
        if last is None or abs(la - last[0]) >= min_move_deg or abs(lo - last[1]) >= min_move_deg:
            cur.append([la, lo])
            last = (la, lo)
        last_t = t
    if len(cur) >= 2:
        segs.append(cur)
    return segs


def sessionize(fixes, gap_ms):
    """Group time-sorted (time, lat, lon) fixes into runs (sessions), splitting on a
    gap larger than gap_ms. Returns [[(t,lat,lon), ...], ...] - one list per run."""
    runs, cur, last_t = [], [], None
    for f in fixes:
        if last_t is not None and f[0] - last_t > gap_ms:
            if cur:
                runs.append(cur)
            cur = []
        cur.append(f)
        last_t = f[0]
    if cur:
        runs.append(cur)
    return runs


def filter_by_date(fixes, date_str):
    """Keep fixes whose LOCAL date equals date_str (YYYY-MM-DD)."""
    import datetime
    day = datetime.date.fromisoformat(date_str)
    return [f for f in fixes
            if datetime.datetime.fromtimestamp(f[0] / 1000).date() == day]


def _fmt_run(run):
    """One-line summary of a run: date, local start-end, duration, fix count."""
    import datetime
    s = datetime.datetime.fromtimestamp(run[0][0] / 1000)
    e = datetime.datetime.fromtimestamp(run[-1][0] / 1000)
    dur = (run[-1][0] - run[0][0]) / 60000.0
    return f"{s:%Y-%m-%d}  {s:%H:%M}-{e:%H:%M}  {dur:4.0f}m  {len(run):>7,} fixes"


def _load_fixes(path):
    """parse_sqlite_track with a friendly error. Returns (fixes, error_or_None)."""
    try:
        return parse_sqlite_track(path), None
    except Exception as exc:
        return None, str(exc)


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
const esri = (svc, mz) => L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/'+svc+'/MapServer/tile/{z}/{y}/{x}',
  {maxZoom: mz||19, attribution:'Tiles &copy; Esri'});
const baseStreet = esri('World_Street_Map', 19);
const baseGray   = esri('Canvas/World_Light_Gray_Base', 16);
const baseSat    = esri('World_Imagery', 19);
// imagery + label/road overlays = "hybrid" for orienting in dense highrise blocks
const baseHybrid = L.layerGroup([esri('World_Imagery', 19),
                                 esri('Reference/World_Transportation', 19),
                                 esri('Reference/World_Boundaries_and_Places', 19)]);
baseStreet.addTo(map);
const layerCtl = L.control.layers({'Streets (Esri)': baseStreet, 'Light gray': baseGray,
                  'Satellite': baseSat, 'Satellite + labels': baseHybrid},
  null, {position:'topright'}).addTo(map);
// keyed OSM basemap (Stadia Alidade Smooth) - only offered when a map key is present
if (D.mapKey){
  const osm = L.tileLayer(
    'https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}.png?api_key='+encodeURIComponent(D.mapKey),
    {maxZoom:20, attribution:'&copy; <a href="https://stadiamaps.com/" target="_blank">Stadia Maps</a>'
      +' &copy; <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a>'
      +' &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>'});
  layerCtl.addBaseLayer(osm, 'OSM smooth (Stadia)');
  osm.addTo(map); map.removeLayer(baseStreet);   // default to it when a key is set
}

function bounds(r,c){ return [[r*D.dlat, c*D.dlon], [(r+1)*D.dlat, (c+1)*D.dlon]]; }
function covColor(n){ return n>=30?'#0b525b':n>=10?'#1c7c8c':n>=3?'#5aa7a7':'#a9d6d6'; }
function center(b){ return [(b[0][0]+b[1][0])/2, (b[0][1]+b[1][1])/2]; }
function maplink(la,lo){ return la.toFixed(5)+', '+lo.toFixed(5)
  +'<br><a href="https://www.openstreetmap.org/?mlat='+la.toFixed(5)+'&mlon='+lo.toFixed(5)
  +'#map=18/'+la.toFixed(5)+'/'+lo.toFixed(5)
  +'" target="_blank" rel="noopener">Open in OpenStreetMap</a>'; }

// coverage (where you've been)
D.covered.forEach(([r,c,n])=>{
  const b=bounds(r,c), ct=center(b);
  L.rectangle(b, {stroke:false, fillColor:covColor(n), fillOpacity:.55})
   .bindPopup('Covered &middot; '+n+' networks<br>'+maplink(ct[0],ct[1])).addTo(map);
});
// recommendations (where to go next)
const RC = {hole:'#dc2626', edge:'#f59e0b'};
D.recs.forEach(([r,c,label,nb])=>{
  const b=bounds(r,c), ct=center(b);
  L.rectangle(b, {color:RC[label], weight:2, fillColor:RC[label], fillOpacity:.35})
   .bindPopup('<b>'+(label==='hole'?'Hole (skipped)':'Edge (frontier)')+'</b><br>'
     +nb+' covered neighbours<br>'+maplink(ct[0],ct[1])).addTo(map);
});

// your actual path (from the SQLite location table), split into time-gap segments
if (D.track && D.track.length){
  const trackLines = D.track.map(seg =>
    L.polyline(seg, {color:'#111827', weight:2, opacity:.85}));
  const trk = L.layerGroup(trackLines).addTo(map);
  layerCtl.addOverlay(trk, 'Your track');
  // live colour picker for the track line
  const picker = L.control({position:'bottomleft'});
  picker.onAdd = function(){
    const d = L.DomUtil.create('div', 'legend');
    d.innerHTML = '<b>Track colour</b>';
    const sel = L.DomUtil.create('select', '', d);
    [['#111827','Black'],['#2563eb','Blue'],['#0891b2','Cyan'],['#16a34a','Green'],
     ['#7c3aed','Purple'],['#db2777','Magenta'],['#dc2626','Red'],['#f59e0b','Orange']]
      .forEach(([hex,name])=>{ const o=document.createElement('option'); o.value=hex; o.text=name; sel.add(o); });
    L.DomEvent.disableClickPropagation(d);
    L.DomEvent.disableScrollPropagation(d);
    sel.addEventListener('change', function(){
      trackLines.forEach(pl => pl.setStyle({color: sel.value}));
      const sw = document.getElementById('trkSw');
      if (sw) sw.style.background = sel.value;
    });
    return d;
  };
  picker.addTo(map);
}

map.fitBounds(D.fit);

const lg = L.control({position:'bottomright'});
lg.onAdd = function(){ const d=L.DomUtil.create('div','legend');
  d.innerHTML = '<b>__TITLE__</b>'
   + '<div><span class="sw" style="background:#0b525b"></span>covered (dense &rarr; light)</div>'
   + '<div><span class="sw" style="background:#dc2626"></span>hole &ndash; skipped street</div>'
   + '<div><span class="sw" style="background:#f59e0b"></span>edge &ndash; walk outward</div>'
   + (D.track && D.track.length ? '<div><span class="sw" id="trkSw" style="background:#111827"></span>your track</div>' : '')
   + '<div style="margin-top:4px;color:#555">'+D.covered.length+' covered cells &middot; '
   + D.recs.length+' suggestions</div>';
  return d; };
lg.addTo(map);
</script></body></html>"""


def render_html(coverage, recs, dlat, dlon, min_obs, out_path, track_segments=None, map_key=None):
    covered = covered_cells(coverage, min_obs)
    cov_list = [[r, c, coverage[(r, c)]] for (r, c) in covered]
    rec_list = [[x["cell"][0], x["cell"][1], x["label"], x["covered_neighbors"]] for x in recs]
    all_cells = list(covered) + [x["cell"] for x in recs]
    rows = [r for r, _ in all_cells] or [0]
    cols = [c for _, c in all_cells] or [0]
    fit = [[min(rows) * dlat, min(cols) * dlon],
           [(max(rows) + 1) * dlat, (max(cols) + 1) * dlon]]
    data = {"dlat": dlat, "dlon": dlon, "covered": cov_list, "recs": rec_list,
            "fit": fit, "track": track_segments or [], "mapKey": map_key or ""}
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
    epilog = (
        "modes (auto-detected from what you give it):\n"
        "  coverage   drop KML/CSV or a folder          ->  where you've been + gaps to fill\n"
        "  history    give a .sqlite backup by itself   ->  whole history: coverage + your path\n"
        "  one run    add  --run N   or  --date DATE    ->  just that one walk\n"
        "  list runs  --list-runs                       ->  numbered index of sessions to pick\n"
        "  menu       -i  (or run with no arguments)    ->  set it all interactively\n"
    )
    p = argparse.ArgumentParser(
        prog="wigle_coverage.py",
        description="Map your WiGLE coverage and recommend where to walk next.",
        epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)

    g = p.add_argument_group("input  (what to read)")
    g.add_argument("paths", nargs="*",
                   help="KML/CSV, a .sqlite backup, a folder, or globs; empty = the ./data folder")
    g.add_argument("--track", "--backup", "--db", dest="track", metavar="BACKUP",
                   help="the WiGLE .sqlite backup - adds your path, and is the source for the run views")
    g.add_argument("--data", metavar="DIR",
                   help=f"folder to read when no path is given (default: {DATA_DIR})")

    g = p.add_argument_group("views  (which slice to map)")
    g.add_argument("--list-runs", action="store_true", help="list the runs in the backup, then exit")
    g.add_argument("--run", type=int, metavar="N", help="map only run N (see --list-runs)")
    g.add_argument("--date", metavar="YYYY-MM-DD", help="map only the fixes from this local date")
    g.add_argument("--run-gap", type=float, default=RUN_GAP_MIN, metavar="MIN",
                   help=f"minutes of gap that separates one run from the next (default {RUN_GAP_MIN})")

    g = p.add_argument_group("grid + track tuning")
    g.add_argument("--cell-size", type=float, default=CELL_SIZE_M, metavar="M",
                   help=f"cell edge in metres (default {CELL_SIZE_M}; below ~25 = GPS scatter)")
    g.add_argument("--min-obs", type=int, default=MIN_OBS, metavar="N",
                   help=f"networks in a cell for it to count as covered (default {MIN_OBS})")
    g.add_argument("--hole-threshold", type=int, default=HOLE_THRESHOLD, metavar="N",
                   help=f"covered neighbours for a hole vs an edge (default {HOLE_THRESHOLD})")
    g.add_argument("--track-gap", type=float, default=TRACK_GAP_MIN, metavar="MIN",
                   help=f"minutes that break the path into segments (default {TRACK_GAP_MIN})")

    g = p.add_argument_group("interface + output")
    g.add_argument("-i", "--menu", action="store_true", help="interactive menu instead of flags")
    g.add_argument("--out", metavar="FILE", help="output HTML path (default: beside the input)")
    g.add_argument("--no-open", action="store_true", help="don't auto-open the map in a browser")
    return p.parse_args()


def run(args):
    # Default to the ./data folder when nothing was passed.
    paths = list(args.paths)
    if not paths and not args.track:
        d = getattr(args, "data", None) or DATA_DIR
        if os.path.isdir(d):
            paths = [d]
    # Gather inputs, then split: SQLite backups (positional or --track) vs KML/CSV.
    inputs = expand_inputs(paths)
    sqlite_files = [f for f in inputs if _is_sqlite(f)]
    cover_files = [f for f in inputs if f not in sqlite_files]
    sqlite_path = args.track
    if not sqlite_path and sqlite_files:
        sqlite_files.sort(key=os.path.getmtime, reverse=True)   # newest backup wins
        sqlite_path = sqlite_files[0]
        if len(sqlite_files) > 1:
            print(f"using newest SQLite backup: {os.path.basename(sqlite_path)}")

    # --list-runs: enumerate sessions in the backup and exit (no map)
    if args.list_runs:
        if not sqlite_path:
            print("--list-runs needs a SQLite backup - drag it in or pass --track.")
            return None
        fixes, err = _load_fixes(sqlite_path)
        if err:
            print(f"couldn't read backup: {err}")
            return None
        runs = sessionize(fixes, args.run_gap * 60_000)
        if not runs:
            print("No timestamped fixes in that backup.")
            return None
        print(f"\n{C.b}{len(runs)} run(s){C.reset} in {os.path.basename(sqlite_path)}  "
              f"{C.dim}(split on >{args.run_gap:.0f} min gaps){C.reset}")
        print(_rule("-"))
        print(f"  {C.dim}  #  date          time         dur      fixes{C.reset}")
        for i, rn in enumerate(runs, 1):
            print(f"  {C.cyan}[{i:>3}]{C.reset} {_fmt_run(rn)}")
        print(_rule("-"))
        print(f"view one:  {C.yellow}--run N{C.reset}  or  {C.yellow}--date YYYY-MM-DD{C.reset}")
        return None

    # ---- choose the coverage source + track fixes ----
    points, track_fixes, base_dir, tag = None, None, None, ""

    if args.run or args.date:                       # single-run view (SQLite only)
        if not sqlite_path:
            print("--run/--date needs a SQLite backup via --track.")
            return None
        allfixes, err = _load_fixes(sqlite_path)
        if err:
            print(f"couldn't read backup: {err}")
            return None
        if args.date:
            sel, tag, what = filter_by_date(allfixes, args.date), f"_{args.date}", f"date {args.date}"
        else:
            runs = sessionize(allfixes, args.run_gap * 60_000)
            if not (1 <= args.run <= len(runs)):
                print(f"--run {args.run} out of range (1..{len(runs)}). Try --list-runs.")
                return None
            sel, tag, what = runs[args.run - 1], f"_run{args.run}", f"run {args.run}"
        if not sel:
            print(f"No fixes for {what}.")
            return None
        if cover_files:
            print("  (run mode: ignoring KML/CSV args - coverage comes from the SQLite session)")
        print(f"{what}: {_fmt_run(sel)}")
        points = [(la, lo) for (_, la, lo) in sel]
        track_fixes = sel
        base_dir = os.path.dirname(os.path.abspath(sqlite_path))

    else:
        if cover_files:                             # coverage from KML/CSV (household/whole)
            print(f"reading {len(cover_files)} file(s)...")
            raw = []
            for f in cover_files:
                n0 = len(raw)
                raw.extend(parse_any(f))
                print(f"  {os.path.basename(f)}: {len(raw) - n0:,} points")
            # Drop invalid/null-island coords - one (0,0) fix would stretch the map to the ocean.
            points = [(la, lo) for (la, lo) in raw
                      if -90 <= la <= 90 and -180 <= lo <= 180 and not (la == 0 and lo == 0)]
            dd = len(raw) - len(points)
            if dd:
                print(f"  dropped {dd:,} invalid/zero coordinates")
            base_dir = os.path.dirname(os.path.abspath(cover_files[0]))
            if sqlite_path:
                print(f"reading track from {os.path.basename(sqlite_path)}...")
                track_fixes, err = _load_fixes(sqlite_path)
                if err:
                    print(f"  !! couldn't read track ({err}); rendering without it")
        elif sqlite_path:                           # entire-DB view straight from the backup
            print(f"entire-DB view from {os.path.basename(sqlite_path)}...")
            track_fixes, err = _load_fixes(sqlite_path)
            if err:
                print(f"couldn't read backup: {err}")
                return None
            points = [(la, lo) for (_, la, lo) in track_fixes]
            base_dir = os.path.dirname(os.path.abspath(sqlite_path))
        else:
            print("No input. Drag in a WiGLE KML/CSV or a .sqlite backup "
                  '(or pass paths / --track "<backup>").')
            return None

    if not points:
        print("No usable coordinates found.")
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

    track_segs = None
    if track_fixes:
        track_segs = build_track_segments(
            track_fixes, args.track_gap * 60_000, TRACK_MIN_MOVE_M / EARTH_M_PER_DEG)

    out = args.out or os.path.join(
        base_dir or os.getcwd(), f"wigle_coverage{tag}_{datetime.date.today():%Y%m%d}.html")
    render_html(coverage, recs, dlat, dlon, args.min_obs, out,
                track_segments=track_segs, map_key=read_map_key())

    print(_rule("="))
    print(f"  {len(points):,} points  ->  {C.b}{len(covered):,}{C.reset} covered cells (~{args.cell_size:.0f} m)")
    print(f"  recommend: {C.red}{holes} holes{C.reset} (skipped) + {C.yellow}{edges} edges{C.reset} (frontier)")
    if track_segs:
        print(f"  track: {sum(len(s) for s in track_segs):,} points in {len(track_segs)} segment(s)")
    print(f"  {C.green}map:{C.reset} {out}")
    print(_rule("="))
    return out


def _detect_data(data_dir):
    """(kml/csv files, sqlite backups) found in a folder."""
    if not os.path.isdir(data_dir):
        return [], []
    inp = expand_inputs([data_dir])
    sq = [f for f in inp if _is_sqlite(f)]
    return [f for f in inp if f not in sq], sq


def _menu_status(st):
    kml, sq = _detect_data(st["data"])
    if kml or sq:
        bits = []
        if kml:
            bits.append(f"{len(kml)} KML/CSV")
        if sq:
            bits.append("backup: " + os.path.basename(max(sq, key=os.path.getmtime)))
        found = " | ".join(bits)
    else:
        found = C.red + "empty - drop KML/CSV or a .sqlite backup here" + C.reset
    mode = ("run %d" % st["run"] if st["mode"] == "run"
            else "date %s" % st["date"] if st["mode"] == "date" else "entire-DB / union")
    b, r, g = C.b, C.reset, C.green
    print()
    print(f" {C.b}{C.cyan}wigle-coverage{r}")
    print(_rule("="))
    print(f"  {b}data {r} | {st['data']}")
    print(f"  {b}found{r} | {found}")
    print(f"  {b}grid {r} | cell {g}{st['cell_size']:.0f} m{r} | min-obs {st['min_obs']} | hole {st['hole_threshold']}")
    print(f"  {b}mode {r} | {g}{mode}{r}")


def _menu_help():
    y, r = C.yellow, C.reset
    print(_rule(label="view - which slice to map"))
    print(f"  {y}all{r}            whole history / union   {C.dim}(default){r}")
    print(f"  {y}runs{r}           list the sessions in the backup")
    print(f"  {y}run{r} N          just run N              {y}date{r} YYYY-MM-DD   just that date")
    print(_rule(label="tune"))
    print(f"  {y}cell{r} N         grid size (m)   {y}min{r} N   min obs   {y}hole{r} N   hole threshold")
    print(f"  {y}data{r} <path>    read a different folder")
    print(_rule(label="go"))
    print(f"  {y}go{r} (or Enter)  build + open the map    {y}help{r}   commands    {y}q{r}   quit")
    print(_rule("="))


def _menu_namespace(st, list_runs=False):
    return argparse.Namespace(
        paths=[st["data"]], track=None, data=None,
        cell_size=st["cell_size"], min_obs=st["min_obs"], hole_threshold=st["hole_threshold"],
        run_gap=st["run_gap"], track_gap=st["track_gap"], list_runs=list_runs,
        run=st["run"] if st["mode"] == "run" else None,
        date=st["date"] if st["mode"] == "date" else None,
        out=None, no_open=True, menu=False)


def interactive_menu(args):
    st = {"data": args.data or DATA_DIR, "cell_size": args.cell_size, "min_obs": args.min_obs,
          "hole_threshold": args.hole_threshold, "run_gap": args.run_gap,
          "track_gap": args.track_gap, "mode": "all", "run": None, "date": None}
    _menu_status(st)
    _menu_help()
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip().strip('"').strip("'")
        try:
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("", "go", "map", "open"):
                out = run(_menu_namespace(st))
                if out:
                    try:
                        webbrowser.open("file://" + os.path.abspath(out))
                    except Exception:
                        pass
            elif cmd == "runs":
                run(_menu_namespace(st, list_runs=True))
            elif cmd == "all":
                st["mode"], st["run"], st["date"] = "all", None, None
                _menu_status(st)
            elif cmd == "run" and arg:
                st["mode"], st["run"], st["date"] = "run", int(arg), None
                _menu_status(st)
            elif cmd == "date" and arg:
                st["mode"], st["date"], st["run"] = "date", arg, None
                _menu_status(st)
            elif cmd == "cell" and arg:
                st["cell_size"] = float(arg)
                _menu_status(st)
            elif cmd == "min" and arg:
                st["min_obs"] = int(arg)
                _menu_status(st)
            elif cmd == "hole" and arg:
                st["hole_threshold"] = int(arg)
                _menu_status(st)
            elif cmd == "data" and arg:
                st["data"] = arg
                _menu_status(st)
            elif cmd in ("help", "h", "?"):
                _menu_help()
            else:
                print("  unknown command - type 'help'")
        except ValueError:
            print("  that option needs a number")


def main():
    global C
    C = setup_color()
    args = parse_args()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)   # ensure the drop folder exists
    except OSError:
        pass
    # No target and nothing to do (or -i) -> interactive menu.
    if args.menu or not (args.paths or args.track or args.list_runs or args.run or args.date):
        interactive_menu(args)
        return
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
