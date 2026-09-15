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

### Optional: a nicer OSM basemap (bring your own key)

The keyless Esri basemaps work out of the box. If you want a clean OSM-based
"Alidade Smooth" style too, drop your **own** free API key into a `map_key.txt`:

1. Grab a free key at <https://client.stadiamaps.com/> (email, no card).
2. Copy `map_key.txt.example` → `map_key.txt` and paste your key on a line.

An **"OSM smooth (Stadia)"** option then appears in the layer switcher (and becomes the
default). **No key → the option simply isn't shown.** `map_key.txt` is git-ignored — it's
*your* key, never committed; you don't use anyone else's, and no one uses yours.

### Target the holes: name the businesses (POIs)

**On by default:** every run asks **OpenStreetMap** (via the Overpass API) what named
businesses sit inside each *hole* — turning "empty cell here" into a concrete hit-list for
your next run. Pass **`--no-pois`** to skip the lookup (or toggle `pois` in the menu).

```
python wigle_coverage.py
#   > HOLE @ -22.970, -43.180  (4 targets): Padaria São José · Bar do Zé · Mercado · Farmácia
```

You get the same list **four ways**, so you can plan at the desk and work off it in the field:

- in each hole's **map popup**;
- in an in-map **"Targets" panel** (top-left) — click any row to fly the map to that hole and
  open its popup, most-loaded holes first;
- in a standalone **`*_targets.html`** — a self-contained, **offline, printable field sheet**.
  It **groups holes by postcode** (then by neighborhood where a postcode isn't mapped, then a
  catch-all "unlocated"), labels each hole by its **street**, and lists every business with its
  **street address** — with a checkbox to tick off as you go. The panel links straight to it, or
  open it on your phone. No network needed once saved;
- in a plain **`*_targets.txt`** (same grouping + addresses) for grepping/scripting.

> **Addresses come from OpenStreetMap and coverage varies worldwide** — dense in much of
> Europe/North America, thinner elsewhere. When a business has no `addr:*` tags the sheet falls
> back gracefully (the coordinate still powers the map link), and postcode-less holes drop to the
> neighborhood or "unlocated" section. We read the address tags from the *same* query — **no extra
> load** on OSM.

**Kept manageable.** The list is trimmed so it stays useful, not overwhelming:

| flag | default | meaning |
|---|--:|---|
| `--max-pois-per-hole` | 10 | most businesses shown per hole; extras collapse to a **"+N more"** note |
| `--max-pois` | 100 | total across all holes — keeps the **richest holes whole**, drops the sparsest past the budget |

Set either to `0` to lift that cap.

**Gentle to OpenStreetMap.** Instead of one big citywide bounding box (which the Overpass server
will time out with a `504` on a large entire-DB view), the lookup walks the area **one small
~1 km tile at a time, only over tiles that actually contain a hole** — each tile **cached
locally** (`./.poi_cache/`, ~30 days, git-ignored) and **politely paced**. So re-running over the
same ground (tweaking `--cell-size`, `--min-obs`, …) doesn't re-query OSM at all, and a hiccup on
one tile yields a **partial** list rather than wiping it (failed tiles aren't cached — re-run to
fill them in). Pass **`--refresh-pois`** to force a fresh pull.

Queries go to the **kumi.systems** mirror by default (well-resourced and minutely-fresh, so it's
much faster than the busy reference instance) and **fall back to `overpass-api.de`** — both are
equally up to date. Three things keep a bad Overpass day from becoming a 15-minute crawl:

- a **short per-tile timeout**, so a slow/dead tile bails in seconds instead of ~40s;
- a **circuit breaker** — if a mirror fails **two tiles in a row**, it's dropped for the rest of
  the run (no more waiting on a server that's down); the other mirror carries on;
- **per-mirror pacing** — the reference instance is paced more slowly so we don't trip its rate
  limit (`429`).

POIs are © OpenStreetMap contributors (ODbL); treat the result as a "known targets" list, not an
exhaustive one.

## Give back: donate to OSM and the servers we lean on

This tool is free because it stands on volunteer- and community-funded infrastructure. If it's
useful to you, please consider chipping in to the projects that make it possible:

- **OpenStreetMap** — the map data behind *everything* here (the basemaps, the businesses, the
  addresses). Donate to the OpenStreetMap Foundation at
  <https://supporting.openstreetmap.org/donate/> (or via
  [OpenStreetMap Germany](https://www.openstreetmap.de/spenden/)).
- **Overpass API** — the query service that names the businesses in your holes. The software is
  free and open (AGPL, by Roland Olbricht). Its reference instance `overpass-api.de` — our
  fallback mirror — is operated by the non-profit **FOSSGIS e.V.**; donate at
  <https://www.fossgis.de/verein/spenden/> (German page; PayPal or bank transfer).
- **kumi.systems** — the fast Overpass mirror this tool queries **by default**, run *free for the
  community* by [Kumi Systems](https://kumi.systems/). They don't solicit public donations — the
  way to support them is a thank-you and, if you ever need paid hosting, keeping them in mind.

Not into giving money? **Mapping the businesses in your own "holes" back into OpenStreetMap is a
donation too** — you're already standing in front of the unmapped ones, and it makes the next
person's target list better, anywhere in the world.

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
