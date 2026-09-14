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


if __name__ == "__main__":
    unittest.main(verbosity=2)
