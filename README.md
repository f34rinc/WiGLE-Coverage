# wigle-coverage

Turn your WiGLE exports into a **coverage map** that shows where you've walked and
— more usefully — **recommends where you haven't**: the blank cells on the frontier
of your footprint (walk outward) and the holes inside it (streets you skipped). Built
for planning the next wardrive so each outing lands on ground that's new to you.

Output is a **self-contained local Leaflet map** (`.html`) you open in your browser.

## Use it

**Drag-and-drop (Windows):** drop one or more `.kml` / WiGLE `.csv` files — or the
whole `WiGLE data` folder — onto `wigle_coverage.py`. It unions everything, builds
the map, and opens it in your browser.

> Coverage is most meaningful from the **union of all your exports**, so dragging the
> whole folder (or selecting all the KMLs together) is the sweet spot. A single
> run-KML just maps that one run's footprint.

**Command line:**

```
python wigle_coverage.py "C:\path\to\WiGLE data" --cell-size 100 --min-obs 2 --out map.html
```

| flag | default | meaning |
|---|--:|---|
| `--cell-size` | 100 | grid cell edge, metres (~one city block; try 50 for finer) |
| `--min-obs` | 2 | networks in a cell before it counts as "covered" (filters stray fixes) |
| `--hole-threshold` | 5 | covered neighbours (of 8) for a cell to rank as a "hole" vs an "edge" |
| `--out` | *beside first input* | output HTML path |
| `--no-open` | off | don't auto-open the map |

## How it reads the map

- **Teal cells** = covered (darker = more networks logged there).
- **Red cells** = *holes* — blank cells surrounded by your coverage: streets you
  walked around but not through.
- **Orange cells** = *edges* — blank cells touching your coverage: the natural
  frontier to expand into.

Each suggestion's popup gives its centre coordinate so you can point a map at it.

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
