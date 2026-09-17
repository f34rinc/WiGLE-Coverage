#!/usr/bin/env python3
"""Unit tests for the wigle-coverage grid + recommendation logic. Stdlib only:

    python -m unittest discover -s tests -v

All coordinates here are fabricated. Covers metre->degree conversion, cell
binning, coverage counting, and the hole/edge recommendation classification.
"""
import os
import sys
import math
import shutil
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wigle_coverage as wc  # noqa: E402

LAT = 40.4375  # a mid-latitude sample, for realistic dlon/dlat (nothing region-specific)


def poi(name, cat="shop", lat=0.0, lon=0.0, street="", hn="", postcode="", suburb=""):
    """Build a wc.POI with sensible blanks - most tests only care about a couple of fields."""
    return wc.POI(name, cat, lat, lon, street, hn, postcode, suburb)


class TestGrid(unittest.TestCase):
    def test_meters_to_deg(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        self.assertAlmostEqual(dlat, 200 / wc.EARTH_M_PER_DEG, places=6)
        self.assertAlmostEqual(
            dlon, 200 / (wc.EARTH_M_PER_DEG * math.cos(math.radians(abs(LAT)))), places=6)
        self.assertGreater(dlon, dlat)   # lon degrees are shorter away from the equator

    def test_cell_of_bins_consistently(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        a = wc.cell_of(40.4375, -111.9255, dlat, dlon)
        b = wc.cell_of(40.4374, -111.9256, dlat, dlon)   # ~15 m away -> same cell
        self.assertEqual(a, b)
        c = wc.cell_of(40.4275, -111.9355, dlat, dlon)    # ~1 km away -> different cell
        self.assertNotEqual(a, c)


class TestCoverage(unittest.TestCase):
    def test_build_and_covered(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        pts = [(40.4375, -111.9255)] * 3 + [(40.4275, -111.9355)] * 1
        cov = wc.build_coverage(pts, dlat, dlon)
        self.assertEqual(sum(cov.values()), 4)
        self.assertEqual(len(cov), 2)
        # min_obs=2 keeps only the 3-point cell; the lone point drops out
        self.assertEqual(len(wc.covered_cells(cov, min_obs=2)), 1)
        self.assertEqual(len(wc.covered_cells(cov, min_obs=1)), 2)


class TestRecommend(unittest.TestCase):
    def test_hole_fully_surrounded(self):
        cov = {(dr, dc): 5 for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)}
        recs = wc.recommend(cov, min_obs=2, hole_threshold=5)
        by_cell = {r["cell"]: r for r in recs}
        self.assertIn((0, 0), by_cell)
        self.assertEqual(by_cell[(0, 0)]["label"], "hole")
        self.assertEqual(by_cell[(0, 0)]["covered_neighbors"], 8)

    def test_edge_not_hole_and_covered_excluded(self):
        cov = {(0, 0): 5}
        recs = wc.recommend(cov, min_obs=2, hole_threshold=5)
        labels = {r["label"] for r in recs}
        self.assertIn("edge", labels)
        self.assertNotIn("hole", labels)
        self.assertNotIn((0, 0), {r["cell"] for r in recs})   # covered cell isn't recommended
        self.assertEqual(len(recs), 8)                        # exactly its 8 blank neighbours

    def test_holes_rank_before_edges(self):
        cov = {(dr, dc): 5 for dr in (-1, 0, 1) for dc in (-1, 0, 1) if (dr, dc) != (0, 0)}
        recs = wc.recommend(cov, min_obs=2, hole_threshold=5)
        self.assertTrue(recs and recs[0]["label"] == "hole")   # the surrounded hole sorts first

    def test_no_coverage_no_recs(self):
        self.assertEqual(wc.recommend({(0, 0): 1}, min_obs=2, hole_threshold=5), [])


class TestTrack(unittest.TestCase):
    def test_gap_splits_segments(self):
        # two clusters ~11 min apart -> two segments (gap threshold 5 min)
        rows = [(0, 40.4375, -111.9255), (1000, 40.4373, -111.9257), (2000, 40.4371, -111.9259),
                (700000, 40.4275, -111.9355), (701000, 40.4273, -111.9357)]
        segs = wc.build_track_segments(rows, gap_ms=300000, min_move_deg=0.0)
        self.assertEqual(len(segs), 2)
        self.assertEqual([len(s) for s in segs], [3, 2])

    def test_jitter_decimated(self):
        # a barely-moving fix between two real ones is dropped
        rows = [(0, 40.4375, -111.9255), (1000, 40.437499, -111.925501), (2000, 40.4370, -111.9260)]
        segs = wc.build_track_segments(rows, gap_ms=300000, min_move_deg=0.0001)
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]), 2)   # the sub-threshold jitter point is gone

    def test_lone_segment_dropped(self):
        # a single isolated fix can't form a line
        self.assertEqual(wc.build_track_segments([(0, 40.4375, -111.9255)], 300000, 0.0), [])


class TestSessionize(unittest.TestCase):
    def test_splits_into_runs_on_gap(self):
        # two clusters ~1h apart -> two runs (30-min gap threshold)
        gap = 30 * 60_000
        fixes = [(0, 40.4375, -111.9255), (60_000, 40.4365, -111.9265),
                 (3_600_000, 40.4275, -111.9355), (3_660_000, 40.4265, -111.9365),
                 (3_720_000, 40.4255, -111.9375)]
        runs = wc.sessionize(fixes, gap)
        self.assertEqual(len(runs), 2)
        self.assertEqual([len(r) for r in runs], [2, 3])

    def test_single_run_when_no_big_gap(self):
        fixes = [(t * 60_000, 40.4375, -111.9255) for t in range(10)]  # 1-min steps, one session
        self.assertEqual(len(wc.sessionize(fixes, 30 * 60_000)), 1)


class TestPOIs(unittest.TestCase):
    def test_assign_pois_to_holes(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        hole = wc.cell_of(40.4375, -111.9255, dlat, dlon)
        other = wc.cell_of(40.4175, -111.9455, dlat, dlon)   # not a hole
        pois = [poi("Corner Bakery", "bakery", 40.43749, -111.92551),  # inside the hole cell
                poi("FarAway", "bar", 40.41749, -111.94551)]           # outside any hole
        got = wc.assign_pois_to_holes(pois, [hole], dlat, dlon)
        self.assertIn(hole, got)
        self.assertEqual([p.name for p in got[hole]], ["Corner Bakery"])
        self.assertNotIn(other, got)                            # non-hole POI dropped


class TestCapPois(unittest.TestCase):
    def _recs(self, cells):
        return [{"cell": list(c), "label": "hole", "covered_neighbors": 8} for c in cells]

    def _mk(self, n, tag):
        return [poi(f"{tag}{i}") for i in range(n)]

    def test_no_caps_keeps_everything(self):
        cells = [(0, 0), (0, 1)]
        pbh = {(0, 0): self._mk(5, "a"), (0, 1): self._mk(2, "b")}
        capped, more = wc.cap_pois(pbh, self._recs(cells), per_hole=0, total=0)
        self.assertEqual(len(capped[(0, 0)]), 5)
        self.assertEqual(len(capped[(0, 1)]), 2)
        self.assertEqual(more, {})

    def test_per_hole_cap_trims_and_reports_more(self):
        pbh = {(0, 0): self._mk(5, "a"), (0, 1): self._mk(2, "b")}
        capped, more = wc.cap_pois(pbh, self._recs([(0, 0), (0, 1)]), per_hole=2, total=0)
        self.assertEqual(len(capped[(0, 0)]), 2)
        self.assertEqual(more[(0, 0)], 3)          # 5 -> 2, so 3 hidden
        self.assertEqual(len(capped[(0, 1)]), 2)
        self.assertNotIn((0, 1), more)             # nothing trimmed off the 2-item hole

    def test_total_cap_drops_whole_sparse_holes_richest_first(self):
        # sizes 5, 3, 2; per-hole cap off; total budget 3 -> keep richest holes whole until >=3
        pbh = {(0, 0): self._mk(5, "a"), (0, 1): self._mk(3, "b"), (0, 2): self._mk(2, "c")}
        capped, more = wc.cap_pois(pbh, self._recs([(0, 0), (0, 1), (0, 2)]), per_hole=0, total=3)
        self.assertIn((0, 0), capped)              # richest kept whole (running 5 -> >=3, stop after)
        self.assertNotIn((0, 1), capped)           # budget already reached, dropped whole
        self.assertNotIn((0, 2), capped)
        self.assertEqual(len(capped[(0, 0)]), 5)


class TestTargetsHtml(unittest.TestCase):
    def _dlatlon(self):
        return wc.meters_to_deg(50, LAT)

    def test_doc_shows_address_and_groups_by_postcode(self):
        import tempfile
        dlat, dlon = self._dlatlon()
        hole = wc.cell_of(40.4375, -111.9255, dlat, dlon)
        recs = [{"cell": list(hole), "label": "hole", "covered_neighbors": 8}]
        poi_by_hole = {hole: [poi("Bob & Sons <Café>", "bakery", 40.43749, -111.92551,
                                  street="Oak St", hn="502", postcode="84043")]}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t_targets.html")
            wc.render_targets_html(p, recs, poi_by_hole, dlat, dlon)
            with open(p, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("Bob &amp; Sons", html)                  # name is HTML-escaped, not raw
        self.assertNotIn("<Café>", html)                       # the < is escaped, never injected
        self.assertIn("84043", html)                           # postcode section header
        self.assertIn("502 Oak St", html)                      # the street address is shown
        self.assertIn('class="street"', html)                  # hole labeled by its street, not coord

    def test_doc_unlocated_falls_back_to_coordinate(self):
        import tempfile
        dlat, dlon = self._dlatlon()
        hole = wc.cell_of(40.4375, -111.9255, dlat, dlon)
        recs = [{"cell": list(hole), "label": "hole", "covered_neighbors": 8}]
        poi_by_hole = {hole: [poi("Mercadinho", "shop", 40.43749, -111.92551)]}   # no address
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t_targets.html")
            wc.render_targets_html(p, recs, poi_by_hole, dlat, dlon)
            with open(p, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("unlocated", html)                       # goes to the catch-all section
        self.assertIn("no address", html)                      # per-POI graceful fallback
        latc = (hole[0] + 0.5) * dlat
        self.assertIn(f"{latc:.5f}", html)                     # hole header falls back to the coordinate

    def test_empty_is_still_a_valid_doc(self):
        import tempfile
        dlat, dlon = self._dlatlon()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t_targets.html")
            wc.render_targets_html(p, [], {}, dlat, dlon)      # no holes / no POIs -> must not crash
            with open(p, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("<!doctype html>", html.lower())


class TestGroupTargets(unittest.TestCase):
    def test_postcode_primary_then_neighborhood_then_unlocated(self):
        # holes are (lat, lon, [POI], more) - the _targets_for_holes shape
        h_zip = (0.0, 0.0, [poi("A", street="S1", postcode="111")], 0)
        h_sub = (1.0, 1.0, [poi("B", street="S2", suburb="Riverside")], 0)   # no postcode
        h_non = (2.0, 2.0, [poi("C")], 0)                                     # neither
        groups = wc.group_targets([h_sub, h_non, h_zip])
        labels = [g[0] for g in groups]
        self.assertEqual(labels[0], "111")                    # postcode section sorts first
        self.assertIn("Riverside", labels[1])                  # neighborhood fallback next
        self.assertIn("no postcode", labels[1])
        self.assertEqual(labels[-1], "unlocated")              # the catch-all comes last

    def test_hole_street_is_the_modal_street(self):
        ps = [poi("A", street="Main St"), poi("B", street="Main St"), poi("C", street="Side St")]
        (label, holes) = wc.group_targets([(0.0, 0.0, ps, 0)])[0]
        self.assertEqual(holes[0]["street"], "Main St")        # representative = most common street


class TestSnapBbox(unittest.TestCase):
    def test_grid_aligned_bbox_snaps_to_itself(self):
        # A tile already on the 0.01 grid must snap to itself. Regression for a float bug:
        # math.floor(0.59 / 0.01) == 58 (0.59/0.01 is 58.9999...), which widened the tile.
        g = wc.POI_CACHE_GRID
        for i in range(0, 200):
            v = round(i * g, 6)
            tile = (v, v, round(v + g, 6), round(v + g, 6))
            self.assertEqual(wc._snap_bbox(*tile), tile)

    def test_mid_cell_bbox_expands_outward(self):
        g = wc.POI_CACHE_GRID
        s, w, n, e = 0.445, 0.585, 0.455, 0.595
        self.assertEqual(wc._snap_bbox(s, w, n, e),
                         (round(0.44, 6), round(0.58, 6), round(0.46, 6), round(0.60, 6)))


class TestPoiCache(unittest.TestCase):
    def test_second_call_hits_cache_without_querying(self):
        import tempfile
        calls = {"n": 0}
        real = wc.fetch_pois

        def fake(s, w, n, e, timeout=60, url=None):
            calls["n"] += 1
            return [wc.POI("Shop", "shop", (s + n) / 2, (w + e) / 2, "", "", "", "")]

        wc.fetch_pois = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                a = wc.fetch_pois_cached(40.4375, -111.9255, 40.4475, -111.9155, cache_dir=d)
                b = wc.fetch_pois_cached(40.4375, -111.9255, 40.4475, -111.9155, cache_dir=d)
        finally:
            wc.fetch_pois = real
        self.assertEqual(calls["n"], 1)                        # only the first call queried OSM
        self.assertFalse(a[1])                                 # (pois, from_cache): first is a miss
        self.assertTrue(b[1])                                  # second is served from cache
        self.assertEqual([p.name for p in b[0]], ["Shop"])     # round-trips JSON back into POI

    def test_refresh_bypasses_cache(self):
        import tempfile
        calls = {"n": 0}
        real = wc.fetch_pois

        def fake(s, w, n, e, timeout=60, url=None):
            calls["n"] += 1
            return []

        wc.fetch_pois = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                wc.fetch_pois_cached(-1.0, -1.0, 0.0, 0.0, cache_dir=d)
                wc.fetch_pois_cached(-1.0, -1.0, 0.0, 0.0, cache_dir=d, refresh=True)
        finally:
            wc.fetch_pois = real
        self.assertEqual(calls["n"], 2)                        # --refresh forced a fresh query


class TestTiledFetch(unittest.TestCase):
    def setUp(self):
        self._sleep = wc.time.sleep
        wc.time.sleep = lambda *a, **k: None       # no real backoff/pacing waits in tests
        self._fetch = wc.fetch_pois
        self.dlat, self.dlon = wc.meters_to_deg(50, LAT)

    def tearDown(self):
        wc.time.sleep = self._sleep
        wc.fetch_pois = self._fetch

    def test_only_tiles_with_holes_and_two_far_holes_split(self):
        far = [(0, 0), (1000, 1000)]               # ~0.45 deg apart -> two different tiles
        tiles = wc.hole_tiles(far, self.dlat, self.dlon)
        self.assertEqual(len(tiles), 2)

    def test_unions_tiles_and_counts(self):
        import tempfile
        calls = []

        def fake(s, w, n, e, timeout=60, url=None):
            calls.append((round(s, 3), round(w, 3)))
            return [wc.POI(f"P{len(calls)}", "shop", (s + n) / 2, (w + e) / 2, "", "", "", "")]

        wc.fetch_pois = fake
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_tiled([(0, 0), (1000, 1000)], self.dlat, self.dlon,
                                              cache_dir=d, pause=0)
        self.assertEqual(stats["tiles"], 2)
        self.assertEqual(stats["queried"], 2)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(pois), 2)             # one POI unioned from each tile

    def test_partial_failure_keeps_the_good_tile(self):
        import tempfile
        bad_tile = wc._tile_of(1000 * self.dlat, 1000 * self.dlon)

        def fake(s, w, n, e, timeout=60, url=None):
            if (round(s, 6), round(w, 6)) == (bad_tile[0], bad_tile[1]):
                raise RuntimeError("HTTP Error 504")
            return [wc.POI("Good", "shop", (s + n) / 2, (w + e) / 2, "", "", "", "")]

        wc.fetch_pois = fake
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_tiled([(0, 0), (1000, 1000)], self.dlat, self.dlon,
                                              cache_dir=d, pause=0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual([p.name for p in pois], ["Good"])   # the healthy tile still came through

    def test_tile_fails_over_to_next_mirror_within_a_tile(self):
        import tempfile
        calls = {"n": 0}

        def fake(s, w, n, e, timeout=60, url=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("HTTP Error 504")   # primary fails, falls over to the next mirror
            return [wc.POI("Late", "shop", 0.0, 0.0, "", "", "", "")]

        wc.fetch_pois = fake
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_tiled([(0, 0)], self.dlat, self.dlon, cache_dir=d, pause=0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["queried"], 1)
        self.assertEqual([p.name for p in pois], ["Late"])

    def test_failure_is_not_cached(self):
        import tempfile
        state = {"fail": True}

        def fake(s, w, n, e, timeout=60, url=None):
            if state["fail"]:
                raise RuntimeError("HTTP Error 504")
            return [wc.POI("Now", "shop", 0.0, 0.0, "", "", "", "")]

        wc.fetch_pois = fake
        with tempfile.TemporaryDirectory() as d:
            _, s1 = wc.fetch_pois_tiled([(0, 0)], self.dlat, self.dlon, cache_dir=d, pause=0)
            self.assertEqual(s1["failed"], 1)
            self.assertFalse(os.listdir(d))         # nothing cached from the failed tile
            state["fail"] = False                    # now it recovers
            pois, s2 = wc.fetch_pois_tiled([(0, 0)], self.dlat, self.dlon, cache_dir=d, pause=0)
        self.assertEqual(s2["queried"], 1)           # re-queried (not served an empty cache)
        self.assertEqual([p.name for p in pois], ["Now"])


class _FakeCon:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, q):
        self._q = q
        if "COPY" in q and "TO '" in q:          # a snapshot export -> create the file os.replace expects
            path = q.split("TO '", 1)[1].split("'", 1)[0]
            open(path, "w").close()
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeDuck:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeCon(self._rows)


class TestOverture(unittest.TestCase):
    def setUp(self):
        self._snap = tempfile.mkdtemp()          # keep the local snapshot out of the real data/ folder
        self._orig = wc.OVERTURE_LOCAL_DIR
        wc.OVERTURE_LOCAL_DIR = self._snap
        self.addCleanup(shutil.rmtree, self._snap, ignore_errors=True)
        self.addCleanup(setattr, wc, "OVERTURE_LOCAL_DIR", self._orig)

    def test_maps_rows_caches_and_drops_coordless(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        rows = [("H&M", "clothing_store", 40.43749, -111.92551,
                 "116 Oak St", "84043", "Lehi"),
                ("NoGeo", "bar", None, None, "", "", "")]     # no coords -> dropped
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d,
                                                 duckdb=_FakeDuck(rows))
            self.assertEqual(stats["queried"], 1)
            self.assertEqual(stats["exported"], 1)                  # first run builds the local snapshot
            self.assertEqual([p.name for p in pois], ["H&M"])       # coord-less row dropped
            p = pois[0]
            self.assertEqual(p.street, "116 Oak St")                # freeform -> street/address
            self.assertEqual(p.postcode, "84043")
            self.assertEqual(p.suburb, "Lehi")
            # a result-cache hit must NOT need duckdb
            pois2, stats2 = wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d, duckdb=None)
        self.assertEqual(stats2["hits"], 1)
        self.assertEqual([p.name for p in pois2], ["H&M"])

    def test_reuses_snapshot_for_a_new_area_inside_it(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        rows = [("Shop", "shop", 40.4, -111.9, "", "", "")]
        with tempfile.TemporaryDirectory() as d:
            wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d, duckdb=_FakeDuck(rows))  # builds
            pois, stats = wc.fetch_pois_overture([(30, 30)], dlat, dlon, cache_dir=d, duckdb=_FakeDuck(rows))
        self.assertEqual(stats["exported"], 0)                      # inside the stored box -> no re-download
        self.assertEqual(stats["queried"], 1)
        self.assertEqual([p.name for p in pois], ["Shop"])

    def test_reexports_when_coverage_expands_outside(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        rows = [("Shop", "shop", 40.4, -111.9, "", "", "")]
        with tempfile.TemporaryDirectory() as d:
            wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d, duckdb=_FakeDuck(rows))       # builds
            _, stats = wc.fetch_pois_overture([(300, 300)], dlat, dlon, cache_dir=d, duckdb=_FakeDuck(rows))
        self.assertEqual(stats["exported"], 1)                      # holes now outside -> re-download

    def test_refresh_snapshot_rebuilds_in_place(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        rows = [("Shop", "shop", 40.4, -111.9, "", "", "")]
        with tempfile.TemporaryDirectory() as d:
            wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d, duckdb=_FakeDuck(rows))       # builds
            _, stats = wc.fetch_pois_overture([(0, 0)], dlat, dlon, cache_dir=d,
                                              refresh_snapshot=True, duckdb=_FakeDuck(rows))
        self.assertEqual(stats["exported"], 1)                      # --refresh-overture re-downloads

    def test_missing_duckdb_on_a_fresh_query_raises(self):
        import tempfile
        orig = wc._load_duckdb
        wc._load_duckdb = lambda: None
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(RuntimeError):
                    wc.fetch_pois_overture([(0, 0)], 0.001, 0.001, cache_dir=d, duckdb=None)
        finally:
            wc._load_duckdb = orig


class TestLeafletInline(unittest.TestCase):
    def test_inlines_vendored_leaflet(self):
        css, js = wc.leaflet_head()
        self.assertTrue(css.lstrip().startswith("<style>"))     # inlined, not a CDN <link>
        self.assertTrue(js.lstrip().startswith("<script>"))
        self.assertNotIn("unpkg.com", css)
        self.assertNotIn("unpkg.com", js)
        self.assertNotIn("url(images/", css)                   # icons inlined as data URIs
        self.assertIn("data:image/png;base64", css)

    def test_falls_back_to_cdn_when_vendor_missing(self):
        import tempfile
        orig = wc.LEAFLET_DIR
        wc.LEAFLET_DIR = os.path.join(tempfile.gettempdir(), "wc_no_leaflet_dir_zzz")
        try:
            css, js = wc.leaflet_head()
        finally:
            wc.LEAFLET_DIR = orig
        self.assertIn("unpkg.com/leaflet@1.9.4/dist/leaflet.css", css)
        self.assertIn("integrity=", css)
        self.assertIn("unpkg.com/leaflet@1.9.4/dist/leaflet.js", js)

    def test_rendered_map_has_no_cdn_dependency(self):
        import tempfile
        dlat, dlon = wc.meters_to_deg(50, LAT)
        coverage = {(0, 0): 3, (0, 1): 3}                      # two covered cells
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "m.html")
            wc.render_html(coverage, [], dlat, dlon, 2, out)
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
        self.assertNotIn("unpkg.com", html)                    # fully self-contained
        self.assertIn("<style>", html)
        self.assertIn("1.9.4", html)                           # the inlined Leaflet source


class TestHotspots(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        vals = list(range(1, 11))                      # 1..10, already sorted
        self.assertEqual(wc._percentile(vals, 90), 10)
        self.assertEqual(wc._percentile(vals, 50), 6)
        self.assertEqual(wc._percentile([], 90), 0)

    def test_hotspot_cells_filters_and_orders(self):
        counts = {(0, 0): 5, (0, 1): 100, (0, 2): 268, (1, 0): 2}
        hs = wc.hotspot_cells(counts, 50)
        self.assertEqual([h[2] for h in hs], [100, 268])   # only >=50, ascending (biggest on top)
        self.assertNotIn((0, 0), [(h[0], h[1]) for h in hs])

    def test_render_embeds_hotspots(self):
        import tempfile
        dlat, dlon = wc.meters_to_deg(50, LAT)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "m.html")
            wc.render_html({(0, 0): 3, (0, 1): 3}, [], dlat, dlon, 2, out, hotspots=[[0, 0, 268]])
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn('"hotspots": [[0, 0, 268]]', html)


class TestFmtSecs(unittest.TestCase):
    def test_formats_elapsed(self):
        self.assertEqual(wc._fmt_secs(0.83), "0.8s")     # sub-10s keeps a decimal
        self.assertEqual(wc._fmt_secs(42.0), "42s")      # tens of seconds, whole
        self.assertEqual(wc._fmt_secs(185.0), "3m 05s")  # minutes + zero-padded seconds


class TestMirrorFallback(unittest.TestCase):
    def setUp(self):
        self._sleep = wc.time.sleep
        wc.time.sleep = lambda *a, **k: None
        self._fetch = wc.fetch_pois
        self.dlat, self.dlon = wc.meters_to_deg(50, LAT)

    def tearDown(self):
        wc.time.sleep = self._sleep
        wc.fetch_pois = self._fetch

    def test_falls_over_from_primary_to_secondary_mirror(self):
        import tempfile
        seen = []

        def fake(s, w, n, e, timeout=60, url=None):
            seen.append(url)
            if url == wc.OVERPASS_URLS[0]:            # primary mirror is down for this tile
                raise RuntimeError("HTTP Error 504")
            return [wc.POI("ok", "shop", 0.0, 0.0, "", "", "", "")]

        wc.fetch_pois = fake
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_tiled([(0, 0)], self.dlat, self.dlon, cache_dir=d, pause=0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual([p.name for p in pois], ["ok"])     # served by the fallback
        self.assertEqual(seen[0], wc.OVERPASS_URLS[0])       # tried the primary first
        self.assertEqual(seen[1], wc.OVERPASS_URLS[1])       # then fell over to the fallback

    def test_circuit_breaker_drops_a_mirror_after_two_failures(self):
        import tempfile
        calls = {wc.OVERPASS_URLS[0]: 0, wc.OVERPASS_URLS[1]: 0}

        def fake(s, w, n, e, timeout=60, url=None):
            calls[url] += 1
            if url == wc.OVERPASS_URLS[0]:            # primary always fails
                raise RuntimeError("HTTP Error 504")
            return [wc.POI("ok", "shop", (s + n) / 2, (w + e) / 2, "", "", "", "")]

        wc.fetch_pois = fake
        holes = [(k * 1000, k * 1000) for k in range(5)]     # 5 separate tiles
        with tempfile.TemporaryDirectory() as d:
            pois, stats = wc.fetch_pois_tiled(holes, self.dlat, self.dlon, cache_dir=d, pause=0)
        self.assertIn(wc.OVERPASS_URLS[0], stats["dropped"])       # primary was dropped
        self.assertEqual(calls[wc.OVERPASS_URLS[0]], 2)           # tried exactly twice, then ignored
        self.assertEqual(stats["failed"], 0)                     # the fallback served every tile
        self.assertEqual(len(pois), 5)


class TestPoliteRetry(unittest.TestCase):
    """fetch_pois_polite: back off + retry a mirror that answers 'busy' (429/50x); fail fast on a
    hard error (403), a timeout, or a dead connection so the caller can fail over."""
    def setUp(self):
        self._fetch = wc.fetch_pois
        self.waits = []                       # seconds passed to our injected sleep, in order

    def tearDown(self):
        wc.fetch_pois = self._fetch

    def _http_error(self, code, retry_after=None):
        import io
        from email.message import Message
        hdrs = Message()
        if retry_after is not None:
            hdrs["Retry-After"] = retry_after
        return urllib.error.HTTPError("http://mirror/api", code, "busy", hdrs, io.BytesIO(b""))

    def _record(self, secs):
        self.waits.append(secs)

    def _assert_one_ratelimit_pause(self):
        lo, hi = wc.OVERPASS_RATELIMIT_PAUSE_RANGE       # a kind, jittered 35-60s pause
        self.assertEqual(len(self.waits), 1)
        self.assertTrue(lo <= self.waits[0] <= hi, self.waits)

    def test_backs_off_then_succeeds_on_429(self):
        seq = [self._http_error(429)]         # busy once, then serves
        def fake(*a, **k):
            if seq:
                raise seq.pop(0)
            return [poi("ok")]
        wc.fetch_pois = fake
        out = wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self.assertEqual([p.name for p in out], ["ok"])
        self._assert_one_ratelimit_pause()                             # 429 -> a jittered 35-60s pause

    def test_gives_up_after_max_retries(self):
        def fake(*a, **k):
            raise self._http_error(503)
        wc.fetch_pois = fake
        with self.assertRaises(urllib.error.HTTPError):
            wc.fetch_pois_polite(0, 0, 1, 1, retries=2, _sleep=self._record)
        self.assertEqual(len(self.waits), 2)              # slept `retries` times, then re-raised

    def test_does_not_retry_a_hard_4xx(self):
        def fake(*a, **k):
            raise self._http_error(403)       # forbidden (e.g. blocked UA) - retrying is pointless
        wc.fetch_pois = fake
        with self.assertRaises(urllib.error.HTTPError):
            wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self.assertEqual(self.waits, [])

    def test_does_not_retry_a_timeout(self):
        def fake(*a, **k):
            raise TimeoutError("read timed out")   # unresponsive mirror -> fail over, don't wait
        wc.fetch_pois = fake
        with self.assertRaises(TimeoutError):
            wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self.assertEqual(self.waits, [])

    def test_short_retry_after_is_raised_to_the_pause(self):
        seq = [self._http_error(429, retry_after="5")]    # server says 5s; our floor is a 35-60s pause
        def fake(*a, **k):
            if seq:
                raise seq.pop(0)
            return [poi("ok")]
        wc.fetch_pois = fake
        wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self._assert_one_ratelimit_pause()                # raised into the 35-60s range

    def test_406_is_treated_as_a_rate_limit(self):
        seq = [self._http_error(406)]                     # 406 -> the same jittered pause
        def fake(*a, **k):
            if seq:
                raise seq.pop(0)
            return [poi("ok")]
        wc.fetch_pois = fake
        wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self._assert_one_ratelimit_pause()

    def test_retry_after_is_capped(self):
        seq = [self._http_error(429, retry_after="99999")]   # absurd -> clamped to the cap
        def fake(*a, **k):
            if seq:
                raise seq.pop(0)
            return [poi("ok")]
        wc.fetch_pois = fake
        wc.fetch_pois_polite(0, 0, 1, 1, _sleep=self._record)
        self.assertEqual(self.waits, [wc.OVERPASS_RETRY_AFTER_CAP_S])


if __name__ == "__main__":
    unittest.main(verbosity=2)
