# wigle-coverage

Turn your WiGLE exports into a **coverage map** that shows where you've walked and
— more usefully — **recommends where you haven't**: the blank cells on the frontier
of your footprint (walk outward) and the holes inside it (streets you skipped). Built
for planning the next wardrive so each outing lands on ground that's new to you.

Output is a **self-contained local Leaflet map** (`.html`) you open in your browser.

## Quick start (no arguments)

Drop your KML/CSV exports and a `.sqlite` backup into the **`data/`** folder beside the
script, then run it with **no arguments** for an interactive menu:

```
python wigle_coverage.py
#   data: ./data   (6 KML/CSV, backup: WiGLE Database Backup.sqlite)
#   cell 50 m  min-obs 2  mode: entire-DB / union
#   > runs        # list your sessions
#   > run 3       # pick one     > cell 75   # tweak
#   > go          # build + open the map
```

`-i` / `--menu` forces the menu even with other flags; `--data DIR` points at a
different folder. Everything below still works as one-shot CLI flags for scripting.

## Best input: the WiGLE "Database Backup" (`.sqlite`)

**Export the entire database.** The `.sqlite` backup is the single best file to feed this
tool, because it carries everything in one place:

- **coverage** — every network's location (where you were),
- **your actual track path** — the timestamped GPS breadcrumb (drawn as the blue line), and
- **timestamps** — which unlock the run views (`--list-runs`, `--run N`, `--date`).

A KML/CSV export has *none* of the track or timing — just network locations — so a backup
alone does more than a whole folder of KMLs. Drop one in `data/` (or pass it) and you get
coverage **and** path **and** run-slicing from that one file.

### How to export it from the WiGLE app (Android)

1. Open the **WiGLE WiFi Wardriving** app.
2. Go to the **Data** tab (where the KML/CSV export buttons are).
3. Tap **"Backup DB"** / **"Database Backup"** (button labels vary a little by version).
4. It writes a SQLite file to the phone — usually the `wiglewifi` folder. It often has
   **no file extension** (just `WiGLE Database Backup`), sometimes timestamped.
5. Copy that file to your PC and drop it in this tool's `data/` folder — **as-is**.

> **No need to add a `.sqlite` extension.** The tool detects a WiGLE backup by its
> contents (the `SQLite format 3` signature), not its name, so the raw export works
> whether you pass it directly, put it in `data/`, or point `--track` at it.

> It's a **point-in-time snapshot** and **per-phone** — take a fresh backup for your latest
> walks, and back up each phone separately if more than one person collects.

## Use it

**Drag-and-drop (Windows):** drop one or more `.kml` / WiGLE `.csv` files — or the
whole `WiGLE data` folder — onto `wigle_coverage.py`. It unions everything, builds
the map, and opens it in your browser.

> Coverage is most meaningful from the **union of all your exports**, so dragging the
> whole folder is the sweet spot — it auto-uses the KMLs for coverage and the **newest
> `.sqlite` backup for the track**. A single run-KML just maps that one run's footprint.
> A `.sqlite` backup on its own (dropped or passed) drives **both** coverage and path.

**Command line:**

```
python wigle_coverage.py "C:\path\to\WiGLE data" --cell-size 50 --min-obs 2 --out map.html
```

| flag | default | meaning |
|---|--:|---|
| `--cell-size` | 50 | grid cell edge, metres (~half a block; below ~25 m just maps GPS scatter) |
| `--min-obs` | 2 | networks in a cell before it counts as "covered" (filters stray fixes) |
| `--hole-threshold` | 5 | covered neighbours (of 8) for a cell to rank as a "hole" vs an "edge" |
| `--track` | – | a WiGLE **SQLite backup** — draws your path *and* powers the run views |
| `--track-gap` | 5 | minutes between GPS fixes that starts a new path segment |
| `--list-runs` | – | list the runs (sessions) in the backup and exit |
| `--run` | – | map only run *N* (from `--list-runs`) — that session's coverage + path |
| `--date` | – | map only the fixes from a local date (`YYYY-MM-DD`) |
| `--run-gap` | 30 | minutes of gap that separates one run from the next |
| `--out` | *beside first input* | output HTML path |
| `--no-open` | off | don't auto-open the map |

## How it reads the map

- **Teal cells** = covered (darker = more networks logged there).
- **Red cells** = *holes* — blank cells surrounded by your coverage: streets you
  walked around but not through.
- **Orange cells** = *edges* — blank cells touching your coverage: the natural
  frontier to expand into.

**Your actual path.** Pass `--track "C:\...\WiGLE Database Backup.sqlite"` and the map
gains a toggle-able **blue line of where you really walked** — raw GPS fixes from the
backup's `location` table, split into segments on time gaps (so separate walks don't
join with a straight line). Cells come from the KML/CSV; the track from the SQLite.
Lay it over the cells to see exactly which streets your coverage came from. A
bottom-left dropdown recolors the track line.

**Historical runs vs a single run.** Because the SQLite backup carries timestamps
(a KML doesn't), you can slice it by session:

```
python wigle_coverage.py --track "...\WiGLE Database Backup.sqlite" --list-runs
#   [  1] 2026-09-06  18:42-20:15   93m    4,120 fixes
#   [  2] 2026-09-13  16:02-17:48  106m    6,800 fixes
python wigle_coverage.py --track "...\WiGLE Database Backup.sqlite" --run 2
python wigle_coverage.py --track "...\WiGLE Database Backup.sqlite" --date 2026-09-13
```

With just `--track` and no run flag you get the **entire-DB view** — all your history
at once (coverage *and* path straight from the backup, no KML needed).

Each cell's popup gives its centre coordinate plus an **Open in OpenStreetMap** link,
so one click drops you at that exact spot with full business/place names (the base
tiles don't carry those). The top-right switcher offers Streets, Light gray,
Satellite, and **Satellite + labels** — the last is handy for orienting among
look-alike highrise blocks.

## Privacy

Your WiGLE exports and the generated map carry **real GPS coordinates**. They stay
**local** and are git-ignored (`*.kml`, `*.csv`, `*.html`, `data/`, `out/`) — nothing
here is uploaded or published, even though this repo is private.

## What it is / isn't

- **Is:** a fast, dependency-free (stdlib) coverage + gap planner. Uses the AP
  locations in your exports as a proxy for where you passed.
- **Isn't (yet):** street-level routing or population weighting — see below.

## Roadmap

- **Streets (phase 2):** overlay the real road network (OpenStreetMap) so
  suggestions are named streets/segments, not just cells.
- **Demographics (phase 2):** weight suggestions by population density (Brazil IBGE
  census) so dense uncovered areas rank first.

## Tests

```
python -m unittest discover -s tests -v
```

Stdlib only. Grid conversion, cell binning, coverage counting, and hole/edge
classification are covered with fabricated coordinates.
