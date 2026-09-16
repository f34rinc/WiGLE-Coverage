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

TARGETS (on by default; --no-pois to skip): name the businesses inside each 'hole' via
OpenStreetMap (Overpass) so you get a hit-list of specific places to aim a future run at.
They show up four ways: in the hole popups; in an in-map "Targets" panel (click a row to
fly to that hole); in a standalone, printable <map>_targets.html field sheet (linked from
that panel); and in a plain <map>_targets.txt. The printable sheet groups holes by postcode
(then neighborhood, then "unlocated") and shows each business's street address when OSM has
one - falling back to the coordinate when it doesn't. The list is trimmed for usefulness -
--max-pois-per-hole (default 4) and --max-pois (default 100, richest holes first); extras
show as "+N more". SOURCE: --poi-source overture (default - Overture Maps places, far better
business coverage worldwide; needs `pip install duckdb`, one query over your area, cached;
--overture-confidence trims low-confidence rows) or osm (no setup). Without duckdb the default
auto-falls back to OSM, so it still works out of the box. With OSM, the lookup is gentle:
it queries ONE small grid tile at a time
(only tiles that contain a hole), each cached locally (./.poi_cache, ~30 days) and politely
paced - never one big citywide scan. Mirrors: overpass-api.de first (the reliable reference
instance), kumi.systems as fallback, with a short per-tile timeout, polite backoff-and-retry when
a mirror is just busy (429/50x), and a circuit breaker that drops a mirror after 2 hard failures
in a row (so a bad Overpass day fails over fast instead of crawling). A failed tile is skipped
(partial list, never a wiped one) and never cached; --refresh-pois forces a fresh pull. OSM
address/POI coverage varies worldwide.

PRIVACY: inputs and the generated map carry real GPS - they stay LOCAL and are
git-ignored. Nothing here is uploaded or published.

stdlib only. Leaflet + CARTO/Esri basemap tiles load in your browser at view time.
"""
import os
import re
import sys
import json
import glob
import time
import math
import random
import hashlib
import argparse
import datetime
import pathlib
import webbrowser
from html import escape as _esc
from collections import namedtuple, Counter

# ---- config defaults --------------------------------------------------------
CELL_SIZE_M   = 50      # grid cell edge in metres (~half a block; near the GPS floor)
MIN_OBS       = 2       # APs in a cell before it counts as "covered" (filters strays)
HOLE_THRESHOLD = 5      # covered 8-neighbours at/above this => "hole", else "edge"
HOTSPOT_PCTL   = 90     # default "hotspots" = the top (100-this)% densest cells (adaptive)
TRACK_GAP_MIN  = 5      # minutes; a larger gap between fixes starts a new track segment
TRACK_MIN_MOVE_M = 5    # drop track fixes closer than this to the last kept one (jitter)
RUN_GAP_MIN    = 30     # minutes of quiet that separates one run/session from the next
EARTH_M_PER_DEG = 111320.0
# Overpass (OpenStreetMap) - names businesses/POIs inside the "hole" cells (on by default).
# overpass-api.de first: it's the reference instance - a touch slower and it rate-limits (we
# pace it), but reliably up when the community mirrors are struggling. kumi.systems is the
# fallback: fast and no rate limit when it's healthy, but it has flaky spells. (Dropped two
# ex-mirrors 2026-09-16: overpass.private.coffee shared kumi's server/IP so it was a redundant
# duplicate that died in lockstep, and overpass.openstreetmap.fr started 403-ing our requests.)
# Both remaining mirrors are equally up to date; a tile query rotates through them on retry.
OVERPASS_URLS = ("https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter")
OVERPASS_URL = OVERPASS_URLS[0]        # default single endpoint (fetch_pois) - primary mirror
# OSM top-level keys we treat as "a named place worth targeting". We always require a name, so
# unnamed clutter (benches, bins) is excluded automatically. Broad on purpose - for wardriving
# any named venue is a target. Changing this set changes the cache key (tiles re-query once).
POI_KEYS = ("shop", "amenity", "office", "tourism", "leisure", "craft",
            "healthcare", "club", "historic")
MAX_POIS_PER_HOLE = 4    # businesses shown per hole (0 = no cap); extras become "+N more"
MAX_POIS_TOTAL    = 100  # total businesses across all holes, richest holes first (0 = no cap)
# One named business from OSM. Address fields (universal OSM addr:* tags) may be blank -
# coverage varies worldwide - so every consumer falls back gracefully when they're empty.
POI = namedtuple("POI", "name cat lat lon street housenumber postcode suburb")
# Local cache of Overpass responses so re-runs over the same area don't re-query OSM.
POI_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".poi_cache")
POI_CACHE_TTL_DAYS = 30
POI_CACHE_GRID = 0.01    # ~1.1 km tiles: we query ONE small tile at a time, only where holes are
# Gentle-to-Overpass knobs: small per-tile queries, immediate mirror-fallback with a circuit
# breaker (drop a mirror that keeps failing), politely paced (more for rate-limiting mirrors).
OVERPASS_TIMEOUT = 12       # server-side [out:json][timeout:N], per tile - short so a slow/dead
                            # tile bails fast (a small ~1km tile normally answers in a few seconds)
OVERPASS_FAIL_STREAK = 2    # consecutive failures on a mirror -> drop it for the rest of the sweep
OVERPASS_PAUSE_S = 0.7      # default polite pause between network queries (cache hits don't pause)
OVERPASS_PAUSE_OVERRIDE = { # mirrors that rate-limit get a longer pause so we don't trip their 429
    "https://overpass-api.de/api/interpreter": 2.5,
}
# Polite backoff-and-retry: when a mirror answers with a THROTTLE/OVERLOAD status (it's up, just
# saying "slow down" / "busy") we wait and try the SAME mirror again instead of giving up. A
# timeout or connection error is NOT retried here - that means the mirror is unresponsive, so we
# fail over to the next mirror at once and let the circuit breaker drop a dead one fast.
OVERPASS_RETRY_CODES = frozenset({429, 500, 502, 503, 504})  # "busy, come back" - worth retrying
OVERPASS_RETRIES = 2          # extra attempts on a throttle/overload response (so up to 3 total)
OVERPASS_RETRY_BASE_S = 3.0   # first backoff wait; doubles each retry (+ jitter), capped below
OVERPASS_RETRY_CAP_S = 30.0   # never back off longer than this per wait
OVERPASS_RETRY_AFTER_CAP_S = 60.0  # honor a server-sent Retry-After header up to this many seconds
# Optional alternative POI source: Overture Maps "places" (open, CDLA-Permissive 2.0). Vastly
# better business coverage than OSM in many regions, worldwide. Needs the `duckdb` package
# (pip install duckdb, ~10MB, no other deps) - stays out of the default OSM path entirely.
POI_SOURCES = ("osm", "overture")
OVERTURE_RELEASE = "2026-08-19.0"     # bump to a newer release date as they publish
OVERTURE_S3 = ("s3://overturemaps-us-west-2/release/{rel}/theme=places/type=place/*")
OVERTURE_MIN_CONFIDENCE = 0.5         # Overture scores each place 0-1; drop the low-confidence noise
POI_ATTRIB = {                        # required attribution per source (shown on the outputs)
    "osm": "OpenStreetMap contributors (ODbL)",
    "overture": "Overture Maps Foundation (CDLA-Permissive 2.0); includes OpenStreetMap (ODbL)",
}
# Default folder read when no path/--track is given: a "data" folder beside this script
# (git-ignored). Drop your KML/CSV + .sqlite backup here and just run the tool.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Optional map API key: a git-ignored map_key.txt beside this script. When present it
# unlocks the keyed OSM (Stadia) basemap in the map's layer switcher; when absent that
# option simply isn't offered. Your own key, never committed - see map_key.txt.example.
MAP_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map_key.txt")

# Leaflet is vendored (BSD-2-Clause) and INLINED into the generated map, so the page is fully
# self-contained: no CDN request, which a hardened browser (NoScript/tracking-protection) blocks
# on file:// pages - and it works offline. Falls back to the CDN only if the vendored copy is
# missing. Refresh with: scripts/vendor_leaflet.py (verifies the download against the SRI hash).
LEAFLET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "leaflet")
LEAFLET_CDN = "https://unpkg.com/leaflet@1.9.4/dist/"
LEAFLET_SRI = {"leaflet.css": "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
               "leaflet.js": "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="}


def leaflet_head():
    """(css, js) markup for the map <head>: the vendored Leaflet inlined (self-contained,
    works in a hardened browser and offline), or CDN <link>/<script> with SRI as a fallback
    if the vendored files aren't present."""
    try:
        with open(os.path.join(LEAFLET_DIR, "leaflet.css"), encoding="utf-8") as fh:
            css = fh.read()
        with open(os.path.join(LEAFLET_DIR, "leaflet.js"), encoding="utf-8") as fh:
            js = fh.read()
        return "<style>\n" + css + "\n</style>", "<script>\n" + js + "\n</script>"
    except OSError:                    # no vendored copy -> CDN (may be blocked by a hardened browser)
        return (f'<link rel="stylesheet" href="{LEAFLET_CDN}leaflet.css"'
                f' integrity="{LEAFLET_SRI["leaflet.css"]}" crossorigin="anonymous"/>',
                f'<script src="{LEAFLET_CDN}leaflet.js"'
                f' integrity="{LEAFLET_SRI["leaflet.js"]}" crossorigin="anonymous"></script>')


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
        self.on = bool(on)         # a real ANSI terminal -> safe to clear-screen
        e = (lambda s: s if on else "")
        self.reset = e("\033[0m"); self.b = e("\033[1m"); self.dim = e("\033[2m")
        self.cyan = e("\033[36m"); self.yellow = e("\033[33m")
        self.green = e("\033[32m"); self.red = e("\033[31m")
        self.magenta = e("\033[35m")   # reserved for the POI-source selector (its own accent)


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


def _fmt_secs(s):
    """Human elapsed time: '0.8s', '42s', or '3m 05s'."""
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{s:.0f}s"
    return f"{int(s // 60)}m {int(s % 60):02d}s"
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


def parse_sqlite_networks(path):
    """Network locations from a WiGLE SQLite backup's `network` table (best-fix lat/lon).
    Returns [(lat, lon), ...] - the actual APs, for counting "networks captured" per cell
    (the WiGLE hotspot metric), distinct from the GPS track in `location`. Read-only."""
    import sqlite3
    import urllib.request
    uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT bestlat, bestlon FROM network "
            "WHERE bestlat BETWEEN -90 AND 90 AND bestlon BETWEEN -180 AND 180 "
            "AND NOT (bestlat = 0 AND bestlon = 0)").fetchall()
    finally:
        con.close()
    return [(float(la), float(lo)) for (la, lo) in rows]


def _percentile(sorted_vals, pct):
    """The pct-th percentile of an already-sorted list (nearest-rank)."""
    if not sorted_vals:
        return 0
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * pct / 100))
    return sorted_vals[i]


def hotspot_cells(counts, threshold):
    """Cells with at least `threshold` networks, as [[row, col, count], ...] biggest first
    (so the densest circles draw on top). `counts` is a {(row,col): n} network-count map."""
    hs = [[r, c, n] for (r, c), n in counts.items() if n >= threshold]
    hs.sort(key=lambda x: x[2])          # ascending -> big ones added last -> on top
    return hs


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


# ---- POIs (name the businesses in each hole, via OpenStreetMap / Overpass) ----
def fetch_pois(south, west, north, east, timeout=OVERPASS_TIMEOUT, url=None):
    """One Overpass query for named POIs (shops/amenities/...) in a bbox, against `url` (an
    Overpass endpoint; defaults to the primary mirror). Returns a list of POI records; address
    fields come from OSM addr:* tags (often blank). Raises on failure (caller degrades).
    `out ... tags` already returns addr:* - no extra request/cost."""
    import urllib.request
    import urllib.parse
    parts = "".join(f'nwr["name"]["{k}"]({south},{west},{north},{east});' for k in POI_KEYS)
    query = f"[out:json][timeout:{int(timeout)}];({parts});out center tags;"
    req = urllib.request.Request(
        url or OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "wigle-coverage (personal wardrive planner; stdlib urllib)"})
    with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        cat = next((tags[k] for k in POI_KEYS if k in tags), "")
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None:                       # way/relation -> its computed center
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None:
            continue
        # universal OSM address tags; suburb ~ neighborhood/district (falls back a couple ways)
        suburb = (tags.get("addr:suburb") or tags.get("addr:neighbourhood")
                  or tags.get("addr:district") or tags.get("addr:city") or "")
        out.append(POI(name, cat, float(lat), float(lon),
                       tags.get("addr:street", ""), tags.get("addr:housenumber", ""),
                       tags.get("addr:postcode", ""), suburb))
    return out


def _snap_bbox(south, west, north, east, grid=POI_CACHE_GRID):
    """Expand a bbox outward to a fixed degree grid so nearby runs share one cache tile
    (and the query covers at least the requested area)."""
    return (math.floor(south / grid) * grid, math.floor(west / grid) * grid,
            math.ceil(north / grid) * grid, math.ceil(east / grid) * grid)


def _tile_of(lat, lon, grid=POI_CACHE_GRID):
    """The single grid tile (a small bbox) that contains a point."""
    s = round(math.floor(lat / grid) * grid, 6)
    w = round(math.floor(lon / grid) * grid, 6)
    return (s, w, round(s + grid, 6), round(w + grid, 6))


def hole_tiles(hole_cells, dlat, dlon, grid=POI_CACHE_GRID):
    """The sorted set of grid tiles that actually contain a hole cell - so we query only
    ground worth querying, never one giant bbox over empty space between clusters. Both
    corners of each ~50 m hole cell are used, so a cell straddling a tile line hits both."""
    tiles = set()
    for (r, c) in hole_cells:
        for la, lo in ((r * dlat, c * dlon), ((r + 1) * dlat, (c + 1) * dlon)):
            tiles.add(_tile_of(la, lo, grid))
    return sorted(tiles)


def _poi_cache_path(bbox, cache_dir):
    key = "|".join(f"{v:.2f}" for v in bbox) + "|" + ",".join(POI_KEYS) + "|v2"
    return os.path.join(cache_dir, "poi_" + hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")


def _mirror_name(url):
    """Just the host of an Overpass endpoint, for readable log lines."""
    m = re.search(r"//([^/]+)", url)
    return m.group(1) if m else url


def _mirror_pause(url):
    """Polite pause after querying a mirror - longer for ones that rate-limit."""
    return OVERPASS_PAUSE_OVERRIDE.get(url, OVERPASS_PAUSE_S)


def _retry_after_secs(err):
    """Seconds to wait from an HTTPError's `Retry-After` header, or None. Honors the common
    integer-seconds form (capped for safety); an HTTP-date form returns None so the caller uses
    its own backoff instead of trusting an arbitrary absolute time."""
    try:
        ra = (err.headers.get("Retry-After") or "").strip()
    except Exception:
        ra = ""
    if ra.isdigit():
        return min(float(ra), OVERPASS_RETRY_AFTER_CAP_S)
    return None


def fetch_pois_polite(south, west, north, east, timeout=OVERPASS_TIMEOUT, url=None,
                      retries=OVERPASS_RETRIES, on_wait=None, _sleep=time.sleep):
    """fetch_pois with polite backoff-and-retry on a throttle/overload RESPONSE (HTTP 429/50x):
    the mirror is up but busy, so we wait - honoring its Retry-After when it sends one, else an
    exponential backoff (3s, 6s, ...) with jitter, capped - and try the SAME mirror again, up to
    `retries` times. A timeout / connection error is NOT retried here: an unresponsive mirror is
    re-raised at once so the caller fails over to the next mirror (and the circuit breaker drops a
    dead one fast). Any other error (bad query, 403, ...) also re-raises immediately. `on_wait
    (attempt, secs, code)` is an optional logging hook; `_sleep` is injectable for tests."""
    import urllib.error
    attempt = 0
    while True:
        try:
            return fetch_pois(south, west, north, east, timeout=timeout, url=url)
        except urllib.error.HTTPError as err:
            if err.code not in OVERPASS_RETRY_CODES or attempt >= retries:
                raise                          # not a "busy" code, or out of retries -> give up
            wait = _retry_after_secs(err)
            if wait is None:                   # no usable Retry-After -> exponential backoff + jitter
                wait = min(OVERPASS_RETRY_BASE_S * (2 ** attempt), OVERPASS_RETRY_CAP_S)
                wait += random.uniform(0, 0.5 * OVERPASS_RETRY_BASE_S)
            if on_wait:
                on_wait(attempt + 1, wait, err.code)
            _sleep(wait)
            attempt += 1


def _poi_cache_get(bbox, cache_dir, ttl_days=POI_CACHE_TTL_DAYS):
    """Cached POIs for a snapped tile bbox, or None on a miss/stale/unreadable entry."""
    path = _poi_cache_path(bbox, cache_dir)
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl_days * 86400:
            with open(path, encoding="utf-8") as fh:
                return [POI(*r) for r in json.load(fh)]
    except Exception:
        pass                                   # unreadable/stale -> treat as a miss
    return None


def _poi_cache_put(bbox, cache_dir, pois):
    """Store a successful tile result (atomic, best-effort)."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = _poi_cache_path(bbox, cache_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([list(p) for p in pois], fh)
        os.replace(tmp, path)
    except Exception:
        pass


def fetch_pois_cached(south, west, north, east, timeout=OVERPASS_TIMEOUT, refresh=False,
                      cache_dir=None, ttl_days=POI_CACHE_TTL_DAYS, url=None):
    """fetch_pois for ONE small tile with an on-disk cache keyed by the snapped bbox, so
    re-running over the same area doesn't re-query OSM. Queries `url` (default primary mirror)
    only on a cache miss. Returns (pois, from_cache). Only a successful query is cached."""
    if cache_dir is None:
        cache_dir = POI_CACHE_DIR          # resolved at call time (tests/callers can override)
    bbox = _snap_bbox(south, west, north, east)
    if not refresh:
        got = _poi_cache_get(bbox, cache_dir, ttl_days)
        if got is not None:
            return got, True
    pois = fetch_pois(*bbox, timeout=timeout, url=url)   # raises on failure (not cached)
    _poi_cache_put(bbox, cache_dir, pois)
    return pois, False


def fetch_pois_tiled(hole_cells, dlat, dlon, refresh=False, cache_dir=None,
                     timeout=OVERPASS_TIMEOUT, pause=None, on_progress=None, urls=None,
                     max_fail_streak=OVERPASS_FAIL_STREAK, ttl_days=POI_CACHE_TTL_DAYS):
    """Look up POIs the gentle way: one small grid tile at a time, only over tiles that contain
    a hole, each cached, politely paced (longer for rate-limiting mirrors). Mirror handling:
      - a tile is tried against the live mirrors in order (primary first); the first that
        answers wins, so a bad tile fails over to the next mirror immediately;
      - a mirror that answers "busy" (HTTP 429/50x) gets a POLITE BACKOFF-AND-RETRY on the same
        mirror (fetch_pois_polite) before it counts as a failure - so a merely-throttled Overpass
        day completes (slowly) instead of collapsing;
      - a CIRCUIT BREAKER drops any mirror that still fails `max_fail_streak` tiles in a row - it
        won't be tried again for the rest of the sweep (no more waiting on a dead server);
      - a success resets that mirror's streak.
    A tile that no live mirror can serve is counted failed and skipped (partial list, never a
    wipe; failures aren't cached). Returns (pois, stats) with stats = {tiles, hits, queried,
    failed, retries, dropped:[urls]} (retries = polite backoff waits across the sweep). `pause`
    overrides per-mirror pacing when given (0 disables it)."""
    if cache_dir is None:
        cache_dir = POI_CACHE_DIR
    tiles = hole_tiles(hole_cells, dlat, dlon)
    active = list(urls or OVERPASS_URLS)       # live mirrors, in preference order
    streak = {u: 0 for u in active}
    pois = []
    stats = {"tiles": len(tiles), "hits": 0, "queried": 0, "failed": 0, "retries": 0, "dropped": []}

    def _note_wait(attempt, secs, code):       # a mirror said "busy" - we're backing off politely
        stats["retries"] += 1

    for i, tile in enumerate(tiles):
        bbox = _snap_bbox(*tile)
        cached = None if refresh else _poi_cache_get(bbox, cache_dir, ttl_days)
        if cached is not None:                 # cache hit needs no mirror at all
            pois.extend(cached)
            stats["hits"] += 1
            if on_progress:
                on_progress(i + 1, stats)
            continue
        tile_pois, used = None, None
        for url in list(active):               # snapshot: we may drop from `active` mid-loop
            try:
                tile_pois = fetch_pois_polite(*bbox, timeout=timeout, url=url, on_wait=_note_wait)
                _poi_cache_put(bbox, cache_dir, tile_pois)
                streak[url] = 0                # success clears this mirror's streak
                used = url
                break
            except Exception:
                streak[url] = streak.get(url, 0) + 1
                if streak[url] >= max_fail_streak and url in active:
                    active.remove(url)         # circuit breaker: stop using this mirror
                    stats["dropped"].append(url)
        if tile_pois is not None:
            pois.extend(tile_pois)
            stats["queried"] += 1
            p = pause if pause is not None else _mirror_pause(used)
            if p and i < len(tiles) - 1:
                time.sleep(p)                  # pace only after a real network query
        else:
            stats["failed"] += 1               # no live mirror served it; re-run later to fill
        if on_progress:
            on_progress(i + 1, stats)
    return pois, stats


# ---- optional POI source: Overture Maps places (open, via DuckDB) -----------
def _load_duckdb():
    """Return the duckdb module, or None if it isn't installed (Overture is opt-in)."""
    try:
        import duckdb
        return duckdb
    except ImportError:
        return None


def _overture_cache_path(bbox, cache_dir, release, conf):
    key = "|".join(f"{v:.2f}" for v in bbox) + f"|overture|{release}|c{conf}"
    return os.path.join(cache_dir, "poi_" + hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")


def fetch_pois_overture(hole_cells, dlat, dlon, refresh=False, cache_dir=None,
                        min_confidence=OVERTURE_MIN_CONFIDENCE, release=OVERTURE_RELEASE,
                        duckdb=None, on_progress=None):
    """POIs from Overture Maps 'places' (open, CDLA-Permissive) via DuckDB. Unlike Overpass
    this is one bbox query over cloud Parquet (DuckDB prunes by the bbox column), so there's no
    per-tile tiling - just the holes' bounding box, cached by bbox. Returns (pois, stats) shaped
    like fetch_pois_tiled. Raises RuntimeError('duckdb-missing') if duckdb isn't installed."""
    if cache_dir is None:
        cache_dir = POI_CACHE_DIR
    rs = [c[0] for c in hole_cells]
    cs = [c[1] for c in hole_cells]
    bbox = _snap_bbox(min(rs) * dlat, min(cs) * dlon, (max(rs) + 1) * dlat, (max(cs) + 1) * dlon)
    stats = {"tiles": 1, "hits": 0, "queried": 0, "failed": 0, "dropped": []}
    path = _overture_cache_path(bbox, cache_dir, release, min_confidence)
    if not refresh:                        # a cache hit is just JSON - no duckdb needed
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < POI_CACHE_TTL_DAYS * 86400:
                with open(path, encoding="utf-8") as fh:
                    pois = [POI(*r) for r in json.load(fh)]
                stats["hits"] = 1
                if on_progress:
                    on_progress(1, stats)
                return pois, stats
        except Exception:
            pass
    duckdb = duckdb or _load_duckdb()      # a fresh query needs duckdb
    if duckdb is None:
        raise RuntimeError("duckdb-missing")
    south, west, north, east = bbox
    src = f"read_parquet('{OVERTURE_S3.format(rel=release)}', hive_partitioning=1)"
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs; "
                    "SET s3_region='us-west-2';")
        rows = con.execute(
            "SELECT names.primary, categories.primary, ST_Y(geometry), ST_X(geometry), "
            "addresses[1].freeform, addresses[1].postcode, addresses[1].locality "
            f"FROM {src} "
            f"WHERE bbox.xmin BETWEEN {west} AND {east} AND bbox.ymin BETWEEN {south} AND {north} "
            f"AND names.primary IS NOT NULL AND confidence >= {float(min_confidence)}").fetchall()
    finally:
        con.close()
    pois = []
    for name, cat, lat, lon, freeform, postcode, locality in rows:
        if lat is None or lon is None:
            continue
        # freeform is the whole street line ("Rua X, 116 - Bairro"); our poi_address() shows it
        pois.append(POI(name, cat or "", float(lat), float(lon),
                        freeform or "", "", postcode or "", locality or ""))
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([list(p) for p in pois], fh)
        os.replace(tmp, path)
    except Exception:
        pass
    stats["queried"] = 1
    if on_progress:
        on_progress(1, stats)
    return pois, stats


def assign_pois_to_holes(pois, hole_cells, dlat, dlon):
    """Bucket POIs into the hole cells they fall in. Returns {(row,col): [poi, ...]}."""
    holes = set(hole_cells)
    out = {}
    for poi in pois:
        cell = cell_of(poi[2], poi[3], dlat, dlon)
        if cell in holes:
            out.setdefault(cell, []).append(poi)
    return out


def cap_pois(poi_by_hole, recs, per_hole=0, total=0):
    """Trim the POI set for display, keeping the highest-value targets:
      1. each hole is trimmed to at most `per_hole` businesses (0 = no per-hole cap),
      2. then whole holes are kept richest-first until `total` businesses are included
         (0 = no total cap) - remaining, sparser holes are dropped entirely.
    Returns (capped_poi_by_hole, more_by_hole) where more_by_hole[cell] is how many extra
    businesses that hole has beyond the ones shown (for a "+N more" note). Holes keep their
    names sorted so the trim is deterministic."""
    holes = [tuple(r["cell"]) for r in recs
             if r["label"] == "hole" and poi_by_hole.get(tuple(r["cell"]))]
    holes.sort(key=lambda c: -len(poi_by_hole[c]))          # richest holes first
    capped, more, running = {}, {}, 0
    for cell in holes:
        if total and running >= total:                     # budget spent -> drop the rest
            break
        ps = sorted(poi_by_hole[cell], key=lambda p: p[0].lower())
        shown = ps[:per_hole] if per_hole else ps
        capped[cell] = shown
        if len(ps) > len(shown):
            more[cell] = len(ps) - len(shown)
        running += len(shown)
    return capped, more


def _targets_for_holes(recs, poi_by_hole, dlat, dlon, more_by_hole=None):
    """Holes that actually have named POIs, most-loaded first (the shared source of
    truth for the .txt list, the HTML doc, and the in-map panel). Yields tuples of
    (center_lat, center_lon, [pois sorted by name], more) where `more` is the count of
    additional businesses trimmed off this hole by the caps (0 if none)."""
    more_by_hole = more_by_hole or {}
    out = []
    for r in recs:
        if r["label"] != "hole":
            continue
        cell = tuple(r["cell"])
        ps = poi_by_hole.get(cell) or []
        if not ps:
            continue
        latc, lonc = (cell[0] + 0.5) * dlat, (cell[1] + 0.5) * dlon
        out.append((latc, lonc, sorted(ps, key=lambda p: p[0].lower()), more_by_hole.get(cell, 0)))
    out.sort(key=lambda t: (-len(t[2]), -t[3]))
    return out


def _osm_link(latc, lonc):
    return (f"https://www.openstreetmap.org/?mlat={latc:.5f}&mlon={lonc:.5f}"
            f"#map=18/{latc:.5f}/{lonc:.5f}")


def poi_address(p):
    """A one-line street address for a POI from its OSM tags, or '' if none is mapped."""
    if not getattr(p, "street", ""):
        return ""
    return f"{p.housenumber} {p.street}".strip() if getattr(p, "housenumber", "") else p.street


def _modal(values):
    """The most common non-empty value (the hole's representative street/postcode/area)."""
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


def group_targets(holes):
    """Group holes into sections for the printable list: primarily by postcode; holes with
    no postcode fall back to their neighborhood (OSM addr:suburb); holes with neither go to a
    single 'unlocated' section. Worldwide-neutral - the header IS the raw postcode/area value.
    `holes` is the _targets_for_holes output. Returns an ordered list of
    (section_label, [hole_dict, ...]) where each hole_dict has lat/lon/pois/more plus its
    representative street/postcode/area."""
    enriched = []
    for latc, lonc, ps, more in holes:
        enriched.append({
            "lat": latc, "lon": lonc, "pois": ps, "more": more,
            "street": _modal(p.street for p in ps),
            "postcode": _modal(p.postcode for p in ps),
            "area": _modal(p.suburb for p in ps)})
    sections = {}
    for h in enriched:
        if h["postcode"]:
            key, label = ("0", h["postcode"]), h["postcode"]
        elif h["area"]:
            key, label = ("1", h["area"]), f"{h['area']} (no postcode)"
        else:
            key, label = ("2", ""), "unlocated"
        sections.setdefault(key, {"label": label, "holes": []})["holes"].append(h)
    ordered = []
    for key in sorted(sections):
        holes_sorted = sorted(sections[key]["holes"], key=lambda h: (-len(h["pois"]), -h["more"]))
        ordered.append((sections[key]["label"], holes_sorted))
    return ordered


def write_targets(path, recs, poi_by_hole, dlat, dlon, more_by_hole=None,
                  attribution="OpenStreetMap contributors (ODbL)"):
    """Write a field target list: businesses inside your holes, grouped by postcode (then
    neighborhood, then unlocated), each with its street address when the source has one."""
    holes = _targets_for_holes(recs, poi_by_hole, dlat, dlon, more_by_hole)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# wigle-coverage targets - named businesses inside your coverage 'holes'\n")
        fh.write(f"# {datetime.date.today():%Y-%m-%d} | POIs (c) {attribution}\n\n")
        if not holes:
            fh.write("(no named POIs found in any hole)\n")
            return
        for label, sec_holes in group_targets(holes):
            fh.write(f"== {label} ==\n")
            for h in sec_holes:
                head = h["street"] or f"{h['lat']:.5f}, {h['lon']:.5f}"
                fh.write(f"  HOLE {head}  ({len(h['pois'])} target(s))\n")
                fh.write(f"    {_osm_link(h['lat'], h['lon'])}\n")
                for p in h["pois"]:
                    addr = poi_address(p)
                    tail = f"  @ {addr}" if addr else ""
                    fh.write(f"      - {p.name}  [{p.cat}]{tail}\n")
                if h["more"]:
                    fh.write(f"      ... (+{h['more']} more not shown)\n")
            fh.write("\n")


_TARGETS_DOC = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#f4f5f4; --card:#ffffff; --ink:#1a2224; --muted:#5b6b6e; --line:#dce3e3;
    --teal:#0b525b; --hole:#dc2626; --chip:#eef4f4; --link:#0b6b78;
  }
  @media (prefers-color-scheme:dark){
    :root{ --bg:#12181a; --card:#1a2325; --ink:#e7edee; --muted:#9fb0b3; --line:#2b3a3d;
           --teal:#5aa7a7; --hole:#f06262; --chip:#20302f; --link:#7fd0dc; }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1240px;margin:0 auto;padding:22px 16px 60px}
  header.top{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 14px;
             border-bottom:3px solid var(--teal);padding-bottom:12px;margin-bottom:6px}
  h1{font-size:1.5rem;margin:0;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:.9rem}
  .count{margin-left:auto;font-weight:600;color:var(--teal)}
  .tip{color:var(--muted);font-size:.86rem;margin:10px 0 18px}
  .btn{border:1px solid var(--line);background:var(--card);color:var(--ink);
       border-radius:7px;padding:5px 11px;font:inherit;font-size:.85rem;cursor:pointer}
  .zone{margin:22px 0 8px;display:flex;align-items:baseline;gap:10px}
  .zone h2{font-size:1.05rem;margin:0;color:var(--teal);letter-spacing:.2px}
  .zone .zc{color:var(--muted);font-size:.8rem}
  .zone::after{content:"";flex:1;border-bottom:1px solid var(--line);align-self:center}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}
  article.hole{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--hole);
               border-radius:9px;padding:12px 14px;break-inside:avoid}
  article.hole > h2{display:flex;align-items:center;gap:8px;margin:0 0 6px;font-size:.98rem}
  .badge{background:var(--hole);color:#fff;border-radius:999px;padding:1px 9px;font-size:.8rem;font-weight:700}
  .street{font-weight:600}
  .coord{font-variant-numeric:tabular-nums;color:var(--muted);font-size:.86rem}
  .osm{margin-left:auto;color:var(--link);text-decoration:none;font-size:.82rem;white-space:nowrap}
  .osm:hover{text-decoration:underline}
  ul.pois{list-style:none;margin:0;padding:0}
  ul.pois li{padding:5px 2px;border-top:1px solid var(--line)}
  ul.pois li:first-child{border-top:none}
  label.row{display:flex;align-items:baseline;gap:8px;cursor:pointer}
  label.row input{margin:0;transform:translateY(1px);flex:none}
  .name{font-weight:500;flex:1;min-width:0}                 /* long names wrap; chip stays right */
  .cat{color:var(--muted);font-size:.76rem;background:var(--chip);flex:none;
       border-radius:5px;padding:1px 7px;white-space:nowrap}
  .addr{color:var(--muted);font-size:.82rem;padding-left:22px;margin-top:1px}  /* own line, indented */
  .addr.none{font-style:italic;opacity:.8}
  li.more{color:var(--muted);font-size:.8rem;font-style:italic;padding:5px 2px;border-top:1px solid var(--line)}
  .empty{color:var(--muted);background:var(--card);border:1px dashed var(--line);
         border-radius:9px;padding:24px;text-align:center}
  footer{color:var(--muted);font-size:.8rem;margin-top:26px;border-top:1px solid var(--line);padding-top:10px}
  footer a{color:var(--link)}
  @media print{
    :root{--bg:#fff;--card:#fff;--ink:#000;--muted:#444;--line:#bbb;--chip:#eee}
    .btn,.tip{display:none}
    article.hole{border:1px solid #999}
    .grid{grid-template-columns:1fr 1fr}
  }
</style></head><body><div class="wrap">
<header class="top">
  <h1>__H1__</h1>
  <span class="sub">__SUB__</span>
  <span class="count">__COUNT__</span>
</header>
<p class="tip">Tick each stop as you pass it. <button class="btn" onclick="window.print()">Print</button></p>
__CARDS__
<footer>Business names &amp; positions &copy; __ATTRIB__ &middot; generated by wigle-coverage. Coverage varies by region &mdash; treat this as a known-targets list, not an exhaustive one.</footer>
</div></body></html>"""


def _poi_li(p):
    addr = poi_address(p)
    addr_html = (f'<div class="addr">{_esc(addr)}</div>' if addr
                 else '<div class="addr none">no address</div>')
    return (f'<li><label class="row"><input type="checkbox">'
            f'<span class="name">{_esc(p.name)}</span>'
            f'<span class="cat">{_esc(p.cat) or "&nbsp;"}</span></label>{addr_html}</li>')


def render_targets_html(path, recs, poi_by_hole, dlat, dlon, more_by_hole=None,
                        attribution="OpenStreetMap contributors (ODbL)"):
    """Write a standalone, offline, print-friendly hit-list of the businesses inside your
    coverage holes, grouped by postcode (then neighborhood, then unlocated) with each
    business's street address - a field sheet you can open on a phone (no network needed)."""
    holes = _targets_for_holes(recs, poi_by_hole, dlat, dlon, more_by_hole)
    total = sum(len(ps) for _, _, ps, _ in holes)
    sections = group_targets(holes)
    if holes:
        blocks = []
        for label, sec_holes in sections:
            secn = sum(len(h["pois"]) for h in sec_holes)
            cards = []
            for h in sec_holes:
                items = "".join(_poi_li(p) for p in h["pois"])
                if h["more"]:
                    items += f'<li class="more">+{h["more"]} more nearby (cap reached)</li>'
                head = (f'<span class="street">{_esc(h["street"])}</span>' if h["street"]
                        else f'<span class="coord">{h["lat"]:.5f}, {h["lon"]:.5f}</span>')
                cards.append(
                    '<article class="hole"><h2>'
                    f'<span class="badge">&#127919; {len(h["pois"])}</span>{head}'
                    f'<a class="osm" href="{_esc(_osm_link(h["lat"], h["lon"]))}" target="_blank" '
                    'rel="noopener">map &#8599;</a></h2>'
                    f'<ul class="pois">{items}</ul></article>')
            blocks.append(
                f'<div class="zone"><h2>{_esc(label)}</h2>'
                f'<span class="zc">{len(sec_holes)} hole(s) &middot; {secn} target(s)</span></div>'
                f'<div class="grid">{"".join(cards)}</div>')
        cards_html = "\n".join(blocks)
    else:
        cards_html = ('<div class="empty">No named businesses turned up inside any hole. '
                      'OSM POI coverage is thin in some regions &mdash; the holes are still '
                      'worth a walk.</div>')
    doc = (_TARGETS_DOC
           .replace("__TITLE__", "WiGLE targets")
           .replace("__H1__", "&#127919; Target list")
           .replace("__SUB__", f"by postcode &middot; {datetime.date.today():%Y-%m-%d}")
           .replace("__COUNT__", f"{len(holes)} holes &middot; {total} targets &middot; {len(sections)} areas")
           .replace("__ATTRIB__", _esc(attribution))
           .replace("__CARDS__", cards_html))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


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
__LEAFLET_CSS__
__LEAFLET_JS__
<style>
  html,body,#map{height:100%;margin:0}
  .legend{background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,.3);
          font:13px system-ui,sans-serif;line-height:1.5}
  .legend b{display:block;margin-bottom:4px}
  .sw{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:-1px;border:1px solid #0003}
  .targets{max-width:264px}
  .targets b{display:inline}
  .targets .tcol{float:right;border:none;background:none;font:inherit;cursor:pointer;color:#555;padding:0 4px;line-height:1}
  .targets .tdoc{display:block;margin:4px 0 2px;font-size:12px;color:#0b6b78;text-decoration:none}
  .targets .tdoc:hover{text-decoration:underline}
  .targets .tlist{list-style:none;margin:6px 0 0;padding:0;max-height:42vh;overflow:auto}
  .targets .tlist>li{padding:5px 4px;border-top:1px solid #eee;cursor:pointer;font-size:12px;line-height:1.35}
  .targets .tlist>li:hover{background:#f3f7f7}
  .targets .tn{display:inline-block;min-width:18px;text-align:center;background:#dc2626;color:#fff;
               border-radius:999px;font-size:11px;font-weight:700;padding:0 5px;margin-right:4px}
  .targets .tnames{margin-top:3px;padding-left:3px}
  .targets .tnames div{padding:1px 0}
  .targets .tmore{color:#777;font-style:italic}
  .leaflet-popup-content .ptargets{margin:3px 0 0;padding-left:20px}
  .leaflet-popup-content .ptargets li{margin:2px 0;line-height:1.3}
  .leaflet-popup-content b{color:#0b525b}
  .hslabel{background:none;border:none;box-shadow:none;padding:0;margin:0;color:#fff;
           font-weight:700;font-size:11px;text-shadow:0 0 2px #000,0 0 2px #000,0 0 1px #000}
  .hslabel::before{display:none}
  .hsramp{display:inline-block;width:54px;height:10px;vertical-align:-1px;border:1px solid #0004;
          border-radius:2px;background:linear-gradient(90deg,#fed976,#feb24c,#fd8d3c,#f03b20,#bd0026)}
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
function escapeHtml(s){ return String(s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
const RC = {hole:'#dc2626', edge:'#f59e0b'};
const targetHoles = [];   // holes that have named businesses -> feeds the target panel
D.recs.forEach(([r,c,label,nb,pois,more])=>{
  const b=bounds(r,c), ct=center(b);
  more = more||0;
  let html='<b>'+(label==='hole'?'Hole (skipped)':'Edge (frontier)')+'</b><br>'
     +nb+' covered neighbours<br>'+maplink(ct[0],ct[1]);
  if (pois && pois.length){
    html += '<br><b>targets:</b><ol class="ptargets">'
          + pois.map(n => '<li>'+escapeHtml(n)+'</li>').join('') + '</ol>';
    if (more) html += '<i>+'+more+' more</i>';
  }
  const rect = L.rectangle(b, {color:RC[label], weight:2, fillColor:RC[label], fillOpacity:.35})
   .bindPopup(html, {maxWidth:340}).addTo(map);
  if (label==='hole' && pois && pois.length)
    targetHoles.push({center:ct, layer:rect, names:pois, more:more});
});

// target panel (top-left) - only when --pois turned up businesses in holes
if (targetHoles.length){
  targetHoles.sort((a,b)=> b.names.length - a.names.length);   // most-loaded first
  const panel = L.control({position:'topleft'});
  panel.onAdd = function(){
    const d = L.DomUtil.create('div','legend targets');
    const doc = D.targetsDoc
      ? '<a class="tdoc" href="'+encodeURI(D.targetsDoc)+'" target="_blank" rel="noopener">open printable list &#8599;</a>'
      : '';
    const rows = targetHoles.map((h,i)=>
      '<li data-i="'+i+'"><span class="tn">'+h.names.length+'</span> targets'
      + '<div class="tnames">'
      + h.names.map(n => '<div>'+escapeHtml(n)+'</div>').join('')
      + (h.more ? '<div class="tmore">+'+h.more+' more</div>' : '')
      + '</div></li>').join('');
    d.innerHTML = '<button class="tcol" title="collapse">&#8211;</button>'
      + '<b>&#127919; Targets ('+targetHoles.length+')</b>'+doc
      + '<ul class="tlist">'+rows+'</ul>';
    L.DomEvent.disableClickPropagation(d);
    L.DomEvent.disableScrollPropagation(d);
    const ul = d.querySelector('.tlist'), btn = d.querySelector('.tcol');
    ul.addEventListener('click', function(ev){       // row -> fly to the hole + open its popup
      const li = ev.target.closest('li[data-i]'); if(!li) return;
      const h = targetHoles[+li.dataset.i];
      map.flyTo(h.center, Math.max(map.getZoom(), 17));
      h.layer.openPopup();
    });
    btn.addEventListener('click', function(){         // collapse / expand the list
      const hidden = ul.style.display==='none';
      ul.style.display = hidden ? '' : 'none';
      btn.innerHTML = hidden ? '&#8211;' : '+';
    });
    return d;
  };
  panel.addTo(map);
}

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

// hotspots: WiGLE-style circles at the densest cells, sized by network count, number labeled,
// and COLOURED by rank within your hotspots (ColorBrewer YlOrRd, low->high) so the top spots pop
if (D.hotspots && D.hotspots.length){
  const hs = L.layerGroup();
  const HSC = ['#fed976','#feb24c','#fd8d3c','#f03b20','#bd0026'];   // YlOrRd 5-class
  const HN = D.hotspots.length;                                      // sorted ascending by count
  D.hotspots.forEach(([r,c,n],i)=>{
    const b=bounds(r,c), ct=center(b);
    const rad = Math.max(6, Math.min(28, 4 + Math.sqrt(n)*0.45));
    const tier = Math.min(HSC.length-1, Math.floor(i / HN * HSC.length));  // quantile / rank tier
    L.circleMarker(ct, {radius:rad, color:'#7f1d1d', weight:1,
                        fillColor:HSC[tier], fillOpacity:.72})
     .bindTooltip(''+n, {permanent:true, direction:'center', className:'hslabel'})
     .bindPopup('<b>'+n+' networks</b> here (SSIDs captured)<br>'+maplink(ct[0],ct[1]))
     .addTo(hs);
  });
  hs.addTo(map);
  layerCtl.addOverlay(hs, 'Hotspots (networks)');
}

map.fitBounds(D.fit);

const lg = L.control({position:'bottomright'});
lg.onAdd = function(){ const d=L.DomUtil.create('div','legend');
  d.innerHTML = '<b>__TITLE__</b>'
   + '<div><span class="sw" style="background:#0b525b"></span>covered (dense &rarr; light)</div>'
   + '<div><span class="sw" style="background:#dc2626"></span>hole &ndash; skipped street</div>'
   + '<div><span class="sw" style="background:#f59e0b"></span>edge &ndash; walk outward</div>'
   + (D.hotspots && D.hotspots.length ? '<div style="margin-top:2px">hotspots (networks) <span class="hsramp"></span> fewer&rarr;more</div>' : '')
   + (D.track && D.track.length ? '<div><span class="sw" id="trkSw" style="background:#111827"></span>your track</div>' : '')
   + '<div style="margin-top:4px;color:#555">'+D.covered.length+' covered cells &middot; '
   + D.recs.length+' suggestions</div>';
  return d; };
lg.addTo(map);
</script></body></html>"""


def render_html(coverage, recs, dlat, dlon, min_obs, out_path, track_segments=None,
                map_key=None, poi_by_hole=None, targets_doc=None, more_by_hole=None,
                hotspots=None):
    poi_by_hole = poi_by_hole or {}
    more_by_hole = more_by_hole or {}
    covered = covered_cells(coverage, min_obs)
    cov_list = [[r, c, coverage[(r, c)]] for (r, c) in covered]
    rec_list = [[x["cell"][0], x["cell"][1], x["label"], x["covered_neighbors"],
                 [p[0] for p in poi_by_hole.get(tuple(x["cell"]), [])],
                 more_by_hole.get(tuple(x["cell"]), 0)] for x in recs]
    all_cells = list(covered) + [x["cell"] for x in recs]
    rows = [r for r, _ in all_cells] or [0]
    cols = [c for _, c in all_cells] or [0]
    fit = [[min(rows) * dlat, min(cols) * dlon],
           [(max(rows) + 1) * dlat, (max(cols) + 1) * dlon]]
    data = {"dlat": dlat, "dlon": dlon, "covered": cov_list, "recs": rec_list,
            "fit": fit, "track": track_segments or [], "mapKey": map_key or "",
            "targetsDoc": targets_doc or "", "hotspots": hotspots or []}
    title = "WiGLE coverage &amp; frontier"
    # Escape <, >, & in the embedded JSON so a POI name from OSM can't break out of the
    # <script> block (e.g. a business literally named "</script>"). JSON \uXXXX escapes
    # parse straight back to the original characters in JS.
    blob = (json.dumps(data)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    css_tag, js_tag = leaflet_head()   # inlined vendored Leaflet (or CDN fallback)
    html = (_HTML.replace("__LEAFLET_CSS__", css_tag).replace("__LEAFLET_JS__", js_tag)
            .replace("__DATA__", blob).replace("__TITLE__", title))
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
    g.add_argument("--pois", action=argparse.BooleanOptionalAction, default=True,
                   help="name the businesses inside each hole (on by default; --no-pois to skip)")
    g.add_argument("--poi-source", choices=POI_SOURCES, default="overture", metavar="SRC",
                   help="where business names come from: 'overture' (default, far better coverage; "
                        "needs: pip install duckdb) or 'osm' (no setup). Without duckdb the default "
                        "auto-falls back to osm.")
    g.add_argument("--overture-confidence", type=float, default=OVERTURE_MIN_CONFIDENCE, metavar="C",
                   help=f"Overture only: drop places below this confidence 0-1 (default {OVERTURE_MIN_CONFIDENCE})")
    g.add_argument("--max-pois-per-hole", type=int, default=MAX_POIS_PER_HOLE, metavar="N",
                   help=f"cap businesses shown per hole; extras become '+N more' (default {MAX_POIS_PER_HOLE}; 0 = no cap)")
    g.add_argument("--max-pois", type=int, default=MAX_POIS_TOTAL, metavar="N",
                   help=f"cap total businesses across all holes, richest first (default {MAX_POIS_TOTAL}; 0 = no cap)")
    g.add_argument("--refresh-pois", action="store_true",
                   help="ignore the local POI cache and re-query the source for this area")

    g = p.add_argument_group("grid + track tuning")
    g.add_argument("--cell-size", type=float, default=CELL_SIZE_M, metavar="M",
                   help=f"cell edge in metres (default {CELL_SIZE_M}; below ~25 = GPS scatter)")
    g.add_argument("--min-obs", type=int, default=MIN_OBS, metavar="N",
                   help=f"networks in a cell for it to count as covered (default {MIN_OBS})")
    g.add_argument("--hole-threshold", type=int, default=HOLE_THRESHOLD, metavar="N",
                   help=f"covered neighbours for a hole vs an edge (default {HOLE_THRESHOLD})")
    g.add_argument("--hotspot", type=int, default=None, metavar="N",
                   help="show WiGLE-style circles on cells with >= N networks captured "
                        f"(default: adaptive, your top {100 - HOTSPOT_PCTL}%% densest; 0 = off)")
    g.add_argument("--track-gap", type=float, default=TRACK_GAP_MIN, metavar="MIN",
                   help=f"minutes that break the path into segments (default {TRACK_GAP_MIN})")

    g = p.add_argument_group("interface + output")
    g.add_argument("-i", "--menu", action="store_true", help="interactive menu instead of flags")
    g.add_argument("--out", metavar="FILE", help="output HTML path (default: beside the input)")
    g.add_argument("--no-open", action="store_true", help="don't auto-open the map in a browser")
    return p.parse_args()


def run(args):
    t_start = time.perf_counter()      # wall-clock: start -> .html generated
    poi_secs = 0.0                     # time spent in the OSM/Overpass lookup (usually the bulk)
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

    # Hotspots: count actual NETWORKS per cell ("SSIDs captured", the WiGLE metric) - not GPS
    # fixes. KML/CSV points already ARE networks; from a SQLite backup, read the `network` table.
    if cover_files and not (args.run or args.date):
        net_counts = coverage
    elif sqlite_path:
        try:
            nets = [(la, lo) for (la, lo) in parse_sqlite_networks(sqlite_path)
                    if -90 <= la <= 90 and -180 <= lo <= 180 and not (la == 0 and lo == 0)]
            net_counts = build_coverage(nets, dlat, dlon)
        except Exception:
            net_counts = coverage
    else:
        net_counts = coverage
    hs_arg = getattr(args, "hotspot", None)
    if hs_arg is None:                              # adaptive default: the top ~10% densest cells
        hs_thr = max(3, _percentile(sorted(net_counts.values()), HOTSPOT_PCTL))
    else:
        hs_thr = hs_arg
    hotspots = hotspot_cells(net_counts, hs_thr) if hs_thr and hs_thr > 0 else []
    if hotspots:
        print(f"  {len(hotspots):,} hotspot cell(s) with {C.b}>={hs_thr}{C.reset} networks "
              f"{C.dim}(peak {max(h[2] for h in hotspots):,}){C.reset}")

    track_segs = None
    if track_fixes:
        track_segs = build_track_segments(
            track_fixes, args.track_gap * 60_000, TRACK_MIN_MOVE_M / EARTH_M_PER_DEG)

    poi_by_hole, more_by_hole = {}, {}
    poi_attrib = POI_ATTRIB["osm"]
    if getattr(args, "pois", True):
        hole_cells = [tuple(r["cell"]) for r in recs if r["label"] == "hole"]
        if not hole_cells:
            print("no holes to look up businesses for.")
        else:
            source = getattr(args, "poi_source", "overture")
            if source == "overture" and _load_duckdb() is None:
                print(f"  {C.dim}Overture needs DuckDB (not installed) - using OpenStreetMap "
                      f"instead.{C.reset} For richer coverage: {C.b}pip install duckdb{C.reset} "
                      f"{C.dim}(~10MB, one package){C.reset}")
                source = "osm"
            poi_attrib = POI_ATTRIB.get(source, POI_ATTRIB["osm"])
            refresh = getattr(args, "refresh_pois", False)
            pois, stats = [], {"tiles": 0, "queried": 0, "hits": 0, "failed": 0, "dropped": []}
            t_poi = time.perf_counter()

            if source == "overture":
                print(f"looking up businesses (Overture Maps, release {OVERTURE_RELEASE}) {C.dim}"
                      f"(one query over your area; first run downloads a DuckDB extension){C.reset}...")
                try:
                    pois, stats = fetch_pois_overture(
                        hole_cells, dlat, dlon, refresh=refresh,
                        min_confidence=getattr(args, "overture_confidence", OVERTURE_MIN_CONFIDENCE))
                except Exception as exc:
                    print(f"  {C.red}!! Overture query failed{C.reset} ({exc}); rendering without targets.")
                poi_secs = time.perf_counter() - t_poi
                how = "from cache" if stats.get("hits") else ("queried S3" if stats.get("queried") else "failed")
                print(f"  Overture lookup: {_fmt_secs(poi_secs)}  {C.dim}({how}, {len(pois):,} places){C.reset}")
            else:
                ntiles = len(hole_tiles(hole_cells, dlat, dlon))
                print(f"looking up businesses (OpenStreetMap) across {len(hole_cells)} holes "
                      f"in {ntiles} small tile(s) {C.dim}(gentle: cached, mirror-fallback, paced){C.reset}...")

                def _prog(done, st):
                    sys.stdout.write(f"\r  tiles {done}/{st['tiles']}  "
                                     f"{C.green}{st['queried']} queried{C.reset}  "
                                     f"{st['hits']} cached  "
                                     f"{(C.red + str(st['failed']) + ' failed' + C.reset) if st['failed'] else '0 failed'}   ")
                    sys.stdout.flush()

                pois, stats = fetch_pois_tiled(hole_cells, dlat, dlon, refresh=refresh, on_progress=_prog)
                poi_secs = time.perf_counter() - t_poi
                sys.stdout.write("\n")
                retried = stats.get("retries", 0)
                print(f"  OSM lookup: {_fmt_secs(poi_secs)}  "
                      f"{C.dim}({stats['queried']} queried, {stats['hits']} cached, "
                      f"{stats['failed']} failed"
                      f"{f', {retried} polite backoff' + ('s' if retried != 1 else '') if retried else ''})"
                      f"{C.reset}")
                if stats.get("dropped"):
                    names = ", ".join(_mirror_name(u) for u in stats["dropped"])
                    print(f"  {C.yellow}note:{C.reset} dropped {names} after "
                          f"{OVERPASS_FAIL_STREAK} failures in a row - used the fallback for the rest.")
                if stats["failed"] and not pois:
                    print(f"  !! all {stats['failed']} tile(s) failed (Overpass busy?); rendering "
                          f"without targets. Re-run later to fill it in.")
                elif stats["failed"]:
                    print(f"  {C.yellow}note:{C.reset} {stats['failed']} tile(s) failed - the list is "
                          f"partial; re-run later (cached tiles won't re-query).")

            found = assign_pois_to_holes(pois, hole_cells, dlat, dlon)
            nfound = sum(len(v) for v in found.values())
            # Trim for display: <= N per hole, then whole holes richest-first up to a total.
            poi_by_hole, more_by_hole = cap_pois(
                found, recs, per_hole=getattr(args, "max_pois_per_hole", MAX_POIS_PER_HOLE),
                total=getattr(args, "max_pois", MAX_POIS_TOTAL))
            nshown = sum(len(v) for v in poi_by_hole.values())
            extra = "" if nshown == nfound else f" (capped from {nfound} across {len(found)})"
            if found:
                print(f"  {nshown} named POIs across {len(poi_by_hole)} holes{extra}")

    out = args.out or os.path.join(
        base_dir or os.getcwd(), f"wigle_coverage{tag}_{datetime.date.today():%Y%m%d}.html")

    # Targets: write both the plain-text list and the printable HTML doc, and hand the
    # doc's name to the map so it can link out to it (and drive its target panel).
    tpath_txt = tpath_html = None
    if poi_by_hole:
        stem = os.path.splitext(out)[0]
        tpath_txt, tpath_html = stem + "_targets.txt", stem + "_targets.html"
        write_targets(tpath_txt, recs, poi_by_hole, dlat, dlon, more_by_hole, attribution=poi_attrib)
        render_targets_html(tpath_html, recs, poi_by_hole, dlat, dlon, more_by_hole, attribution=poi_attrib)

    render_html(coverage, recs, dlat, dlon, args.min_obs, out,
                track_segments=track_segs, map_key=read_map_key(), poi_by_hole=poi_by_hole,
                targets_doc=os.path.basename(tpath_html) if tpath_html else None,
                more_by_hole=more_by_hole, hotspots=hotspots)

    print(_rule("="))
    print(f"  {len(points):,} points  ->  {C.b}{len(covered):,}{C.reset} covered cells (~{args.cell_size:.0f} m)")
    print(f"  recommend: {C.red}{holes} holes{C.reset} (skipped) + {C.yellow}{edges} edges{C.reset} (frontier)")
    if track_segs:
        print(f"  track: {sum(len(s) for s in track_segs):,} points in {len(track_segs)} segment(s)")
    if poi_by_hole:
        print(f"  targets: {tpath_txt}")
        print(f"           {tpath_html}  {C.dim}(printable hit-list){C.reset}")
    print(f"  {C.green}map:{C.reset} {out}")
    total = time.perf_counter() - t_start
    other = max(0.0, total - poi_secs)
    breakdown = f"  {C.dim}({_fmt_secs(poi_secs)} POI lookup + {_fmt_secs(other)} rest){C.reset}" if poi_secs else ""
    print(f"  {C.b}time:{C.reset} {_fmt_secs(total)} start -> map{breakdown}")
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
    b, r, g, m = C.b, C.reset, C.green, C.magenta
    src = st.get("poi_source", "overture")
    print()
    print(f" {C.b}{C.cyan}wigle-coverage{r}")
    print(_rule("="))
    print(f"  {b}data {r} | {st['data']}")
    print(f"  {b}found{r} | {found}")
    print(f"  {b}grid {r} | cell {g}{st['cell_size']:.0f} m{r} | min-obs {st['min_obs']} | hole {st['hole_threshold']}")
    print(f"  {b}mode {r} | {g}{mode}{r}  |  pois {'on' if st.get('pois') else 'off'}")
    print(f"  {b}{m}source{r} | {m}{b}{src.upper()}{r} | where business names come from "
          f"({'zero-setup' if src == 'osm' else 'needs duckdb'})")


def _menu_help():
    y, r, m = C.yellow, C.reset, C.magenta
    print(_rule(label="view - which slice to map"))
    print(f"  {y}all{r}            whole history / union   {C.dim}(default){r}")
    print(f"  {y}runs{r}           list the sessions in the backup")
    print(f"  {y}run{r} N          just run N              {y}date{r} YYYY-MM-DD   just that date")
    print(_rule(label="tune"))
    print(f"  {y}cell{r} N         grid size (m)   {y}min{r} N   min obs   {y}hole{r} N   hole threshold")
    print(f"  {y}data{r} <path>    read a different folder")
    print(f"  {y}pois{r}           toggle naming businesses in holes")
    print(_rule(label="POI source - where the business names come from"))
    print(f"  {m}{C.b}source{r}         switch  {m}{C.b}overture{r} (default; far more businesses; "
          f"pip install duckdb)  <->  {m}{C.b}osm{r} (zero-setup)")
    print(_rule(label="go"))
    print(f"  {y}go{r} (or Enter)  build + open the map    {y}help{r}   commands    {y}q{r}   quit")
    print(_rule("="))


def _clear():
    """Wipe the terminal (screen + scrollback) for a fresh redraw. Only on a real
    ANSI terminal; when piped/redirected it just prints a divider so output stays sane."""
    if C.on:
        sys.stdout.write("\033[2J\033[3J\033[H")   # clear screen + scrollback, cursor home
        sys.stdout.flush()
    else:
        print(_rule("="))


def _redraw(st, changed=None, note=None):
    """Clear and redraw the WHOLE menu (status + help) so a changed setting shows
    against a fresh panel instead of stacking up. `changed` prints an obvious cue."""
    _clear()
    _menu_status(st)
    if changed:
        print(f"  {C.b}{C.cyan}>> redrawn{C.reset}  {C.dim}({changed}){C.reset}")
    if note:
        print(note)
    _menu_help()


def _menu_namespace(st, list_runs=False):
    return argparse.Namespace(
        paths=[st["data"]], track=None, data=None,
        cell_size=st["cell_size"], min_obs=st["min_obs"], hole_threshold=st["hole_threshold"],
        run_gap=st["run_gap"], track_gap=st["track_gap"], list_runs=list_runs,
        run=st["run"] if st["mode"] == "run" else None,
        date=st["date"] if st["mode"] == "date" else None,
        pois=st.get("pois", True), poi_source=st.get("poi_source", "overture"),
        overture_confidence=OVERTURE_MIN_CONFIDENCE, hotspot=None,
        max_pois_per_hole=MAX_POIS_PER_HOLE, max_pois=MAX_POIS_TOTAL,
        refresh_pois=False, out=None, no_open=True, menu=False)


def interactive_menu(args):
    st = {"data": args.data or DATA_DIR, "cell_size": args.cell_size, "min_obs": args.min_obs,
          "hole_threshold": args.hole_threshold, "run_gap": args.run_gap,
          "track_gap": args.track_gap, "mode": "all", "run": None, "date": None,
          "pois": getattr(args, "pois", True), "poi_source": getattr(args, "poi_source", "overture")}
    _redraw(st)
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
                        webbrowser.open(pathlib.Path(out).resolve().as_uri())
                    except Exception:
                        pass
            elif cmd == "runs":
                run(_menu_namespace(st, list_runs=True))
            elif cmd == "all":
                st["mode"], st["run"], st["date"] = "all", None, None
                _redraw(st, changed="view -> entire-DB / union")
            elif cmd == "run" and arg:
                st["mode"], st["run"], st["date"] = "run", int(arg), None
                _redraw(st, changed=f"view -> run {st['run']}")
            elif cmd == "date" and arg:
                st["mode"], st["date"], st["run"] = "date", arg, None
                _redraw(st, changed=f"view -> date {st['date']}")
            elif cmd == "cell" and arg:
                st["cell_size"] = float(arg)
                _redraw(st, changed=f"cell -> {st['cell_size']:.0f} m")
            elif cmd == "min" and arg:
                st["min_obs"] = int(arg)
                _redraw(st, changed=f"min-obs -> {st['min_obs']}")
            elif cmd == "hole" and arg:
                st["hole_threshold"] = int(arg)
                _redraw(st, changed=f"hole-threshold -> {st['hole_threshold']}")
            elif cmd == "data" and arg:
                st["data"] = arg
                _redraw(st, changed=f"data -> {st['data']}")
            elif cmd == "pois":
                st["pois"] = not st.get("pois", False)
                _redraw(st, changed=f"pois -> {'on' if st['pois'] else 'off'}")
            elif cmd in ("source", "poi", "poi-source"):
                st["poi_source"] = "osm" if st.get("poi_source", "overture") == "overture" else "overture"
                note = None
                if st["poi_source"] == "overture" and _load_duckdb() is None:
                    note = (f"  {C.yellow}heads-up:{C.reset} Overture needs DuckDB - "
                            f"{C.b}pip install duckdb{C.reset} (falls back to OSM until then)")
                _redraw(st, changed=f"source -> {st['poi_source']}", note=note)
            elif cmd in ("help", "h", "?"):
                _redraw(st)
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
            webbrowser.open(pathlib.Path(out).resolve().as_uri())
        except Exception:
            pass
    if _launched_standalone():
        try:
            input("\nDone. Press Enter to close.")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
