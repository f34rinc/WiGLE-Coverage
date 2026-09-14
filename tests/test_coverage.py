#!/usr/bin/env python3
"""Unit tests for the wigle-coverage grid + recommendation logic. Stdlib only:

    python -m unittest discover -s tests -v

All coordinates here are fabricated. Covers metre->degree conversion, cell
binning, coverage counting, and the hole/edge recommendation classification.
"""
import os
import sys
import math
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wigle_coverage as wc  # noqa: E402

LAT = -22.97  # Rio-ish, for realistic dlon/dlat


class TestGrid(unittest.TestCase):
    def test_meters_to_deg(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        self.assertAlmostEqual(dlat, 200 / wc.EARTH_M_PER_DEG, places=6)
        self.assertAlmostEqual(
            dlon, 200 / (wc.EARTH_M_PER_DEG * math.cos(math.radians(abs(LAT)))), places=6)
        self.assertGreater(dlon, dlat)   # lon degrees are shorter away from the equator

    def test_cell_of_bins_consistently(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        a = wc.cell_of(-22.9700, -43.1800, dlat, dlon)
        b = wc.cell_of(-22.9701, -43.1801, dlat, dlon)   # ~15 m away -> same cell
        self.assertEqual(a, b)
        c = wc.cell_of(-22.9800, -43.1900, dlat, dlon)    # ~1 km away -> different cell
        self.assertNotEqual(a, c)


class TestCoverage(unittest.TestCase):
    def test_build_and_covered(self):
        dlat, dlon = wc.meters_to_deg(200, LAT)
        pts = [(-22.97, -43.18)] * 3 + [(-22.98, -43.19)] * 1
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
        rows = [(0, -22.97, -43.18), (1000, -22.9702, -43.1802), (2000, -22.9704, -43.1804),
                (700000, -22.98, -43.19), (701000, -22.9802, -43.1902)]
        segs = wc.build_track_segments(rows, gap_ms=300000, min_move_deg=0.0)
        self.assertEqual(len(segs), 2)
        self.assertEqual([len(s) for s in segs], [3, 2])

    def test_jitter_decimated(self):
        # a barely-moving fix between two real ones is dropped
        rows = [(0, -22.97, -43.18), (1000, -22.970001, -43.180001), (2000, -22.9705, -43.1805)]
        segs = wc.build_track_segments(rows, gap_ms=300000, min_move_deg=0.0001)
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]), 2)   # the sub-threshold jitter point is gone

    def test_lone_segment_dropped(self):
        # a single isolated fix can't form a line
        self.assertEqual(wc.build_track_segments([(0, -22.97, -43.18)], 300000, 0.0), [])


class TestSessionize(unittest.TestCase):
    def test_splits_into_runs_on_gap(self):
        # two clusters ~1h apart -> two runs (30-min gap threshold)
        gap = 30 * 60_000
        fixes = [(0, -22.97, -43.18), (60_000, -22.971, -43.181),
                 (3_600_000, -22.98, -43.19), (3_660_000, -22.981, -43.191),
                 (3_720_000, -22.982, -43.192)]
        runs = wc.sessionize(fixes, gap)
        self.assertEqual(len(runs), 2)
        self.assertEqual([len(r) for r in runs], [2, 3])

    def test_single_run_when_no_big_gap(self):
        fixes = [(t * 60_000, -22.97, -43.18) for t in range(10)]  # 1-min steps, one session
        self.assertEqual(len(wc.sessionize(fixes, 30 * 60_000)), 1)


class TestPOIs(unittest.TestCase):
    def test_assign_pois_to_holes(self):
        dlat, dlon = wc.meters_to_deg(50, LAT)
        hole = wc.cell_of(-22.9700, -43.1800, dlat, dlon)
        other = wc.cell_of(-22.9900, -43.2000, dlat, dlon)   # not a hole
        pois = [("Padaria", "bakery", -22.97001, -43.18001),   # inside the hole cell
                ("FarAway", "bar", -22.99001, -43.20001)]      # outside any hole
        got = wc.assign_pois_to_holes(pois, [hole], dlat, dlon)
        self.assertIn(hole, got)
        self.assertEqual([p[0] for p in got[hole]], ["Padaria"])
        self.assertNotIn(other, got)                            # non-hole POI dropped


class TestCapPois(unittest.TestCase):
    def _recs(self, cells):
        return [{"cell": list(c), "label": "hole", "covered_neighbors": 8} for c in cells]

    def _mk(self, n, tag):
        return [(f"{tag}{i}", "shop", 0.0, 0.0) for i in range(n)]

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

    def test_writes_doc_with_business_and_coord(self):
        import tempfile
        dlat, dlon = self._dlatlon()
        hole = wc.cell_of(-22.9700, -43.1800, dlat, dlon)
        recs = [{"cell": list(hole), "label": "hole", "covered_neighbors": 8}]
        poi_by_hole = {hole: [("Padaria & Café <Zé>", "bakery", -22.97001, -43.18001)]}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t_targets.html")
            wc.render_targets_html(p, recs, poi_by_hole, dlat, dlon)
            with open(p, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("Padaria &amp; Caf", html)              # name is HTML-escaped, not raw
        self.assertNotIn("<Zé>", html)                         # the < is escaped, never injected
        latc = (hole[0] + 0.5) * dlat
        self.assertIn(f"{latc:.5f}", html)                     # the hole's own coordinate is present

    def test_empty_is_still_a_valid_doc(self):
        import tempfile
        dlat, dlon = self._dlatlon()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t_targets.html")
            wc.render_targets_html(p, [], {}, dlat, dlon)      # no holes / no POIs -> must not crash
            with open(p, encoding="utf-8") as fh:
                html = fh.read()
        self.assertIn("<!doctype html>", html.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
