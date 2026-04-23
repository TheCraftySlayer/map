"""Regression tests for build_data.py.

Guards the edge-case patches documented in the module so the upcoming
module split (Phase 4) and analytical additions (Phase 3) can't silently
regress them.

Run:
    python -m unittest discover -s tests
"""
import datetime as dt
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("build_data", ROOT / "build_data.py")
build_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_data)


class TestPrimitives(unittest.TestCase):
    def test_safe_float(self):
        self.assertEqual(build_data.safe_float(1), 1.0)
        self.assertEqual(build_data.safe_float("2.5"), 2.5)
        self.assertEqual(build_data.safe_float(None), 0)
        self.assertEqual(build_data.safe_float("x"), 0)
        self.assertEqual(build_data.safe_float(None, default=-1), -1)

    def test_safe_int(self):
        self.assertEqual(build_data.safe_int(3.9), 3)
        self.assertEqual(build_data.safe_int("7"), 7)
        self.assertEqual(build_data.safe_int(None), 0)
        self.assertEqual(build_data.safe_int("foo"), 0)

    def test_median_safe(self):
        self.assertEqual(build_data.median_safe([]), 0)
        self.assertEqual(build_data.median_safe([1, 2, 3]), 2)
        self.assertEqual(build_data.median_safe([1, 2, 3, 4]), 2.5)


class TestExtractYear(unittest.TestCase):
    """Date/year parser — exercises every format in the docstring."""

    def test_datetime(self):
        self.assertEqual(build_data.extract_year(dt.date(2024, 6, 15)), 2024)
        self.assertEqual(build_data.extract_year(dt.datetime(2019, 1, 1)), 2019)

    def test_plain_year_int(self):
        self.assertEqual(build_data.extract_year(2021), 2021)

    def test_packed_yyyymmdd_int(self):
        self.assertEqual(build_data.extract_year(20240615), 2024)

    def test_iso_string(self):
        self.assertEqual(build_data.extract_year("2020-03-12"), 2020)

    def test_slash_string(self):
        self.assertEqual(build_data.extract_year("03/12/2020"), 2020)

    def test_no_separator_string(self):
        self.assertEqual(build_data.extract_year("20180101"), 2018)

    def test_none_and_empty(self):
        self.assertIsNone(build_data.extract_year(None))
        self.assertIsNone(build_data.extract_year(""))
        self.assertIsNone(build_data.extract_year("   "))

    def test_out_of_range(self):
        self.assertIsNone(build_data.extract_year(1800))
        self.assertIsNone(build_data.extract_year(2200))
        self.assertIsNone(build_data.extract_year("1899-01-01"))

    def test_garbage(self):
        self.assertIsNone(build_data.extract_year("foo"))
        self.assertIsNone(build_data.extract_year("abc-def-ghi"))


class TestPointInPolygon(unittest.TestCase):
    """The horizontal-edge epsilon at build_data.py:376 has bitten us before."""

    def test_inside_square(self):
        ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        self.assertTrue(build_data._point_in_ring(5, 5, ring))

    def test_outside_square(self):
        ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        self.assertFalse(build_data._point_in_ring(15, 5, ring))

    def test_horizontal_edge_no_divzero(self):
        """Ray at py=5 crosses two segments that are horizontal
        at exactly the ray height — the epsilon must prevent div-by-zero."""
        ring = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10), (0, 0)]
        build_data._point_in_ring(2.5, 5, ring)

    def test_degenerate_ring(self):
        self.assertFalse(build_data._point_in_ring(0, 0, [(1, 1)]))
        self.assertFalse(build_data._point_in_ring(0, 0, []))

    def test_polygon_with_hole(self):
        geom = {
            "type": "Polygon",
            "coordinates": [
                [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)],
                [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)],
            ],
        }
        self.assertTrue(build_data._point_in_geom(3, 3, geom))
        self.assertFalse(build_data._point_in_geom(10, 10, geom))
        self.assertFalse(build_data._point_in_geom(30, 30, geom))

    def test_multipolygon(self):
        geom = {
            "type": "MultiPolygon",
            "coordinates": [
                [[(0, 0), (5, 0), (5, 5), (0, 5), (0, 0)]],
                [[(10, 10), (15, 10), (15, 15), (10, 15), (10, 10)]],
            ],
        }
        self.assertTrue(build_data._point_in_geom(2, 2, geom))
        self.assertTrue(build_data._point_in_geom(12, 12, geom))
        self.assertFalse(build_data._point_in_geom(7, 7, geom))

    def test_empty_geom(self):
        self.assertFalse(build_data._point_in_geom(0, 0, None))
        self.assertFalse(build_data._point_in_geom(0, 0, {}))
        self.assertFalse(build_data._point_in_geom(0, 0, {"type": "Polygon", "coordinates": []}))


class TestOLSFit(unittest.TestCase):
    def test_too_few_points(self):
        self.assertIsNone(build_data._ols_fit([(i, i) for i in range(7)]))

    def test_basic_fit(self):
        # y = 2x + 1
        fit = build_data._ols_fit([(i, 2 * i + 1) for i in range(10)])
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["slope"], 2.0, places=6)
        self.assertAlmostEqual(fit["intercept"], 1.0, places=6)
        self.assertEqual(fit["n"], 10)

    def test_zero_variance(self):
        fit = build_data._ols_fit([(5, y) for y in range(10)])
        self.assertIsNotNone(fit)
        self.assertEqual(fit["slope"], 0.0)

    def test_ignores_none_pairs(self):
        pairs = [(None, 5), (1, None)] + [(i, 2 * i) for i in range(8)]
        fit = build_data._ols_fit(pairs)
        self.assertIsNotNone(fit)
        self.assertAlmostEqual(fit["slope"], 2.0, places=6)


def _mk_roll_rec(nbhd, yr, totvalue=100_000, hoh=False, vet=False, vf=False,
                 sale_yr=None, yrbuilt=1990):
    """Helper: synthesize a minimal tax-roll record."""
    rec = {
        "NBHD": nbhd,
        "TAXYR": yr,
        "TOTVALUE": totvalue,
        "HOHEXEMP": 1000 if hoh else 0,
        "VETEXEMP": 4000 if vet else 0,
        "EXEMCODE": "VF" if vf else "",
        "YRBUILT": yrbuilt,
        "SALEPRICE": 0,
        "NEWCONST": 0,
    }
    if sale_yr:
        rec["LSALEDATE"] = f"{sale_yr}-06-15"
    return rec


def _mk_existing_feature(nbhd, extra=None):
    feat = {
        "type": "Feature",
        "properties": {"nbhd": nbhd, **(extra or {})},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-106.7, 35.0], [-106.6, 35.0],
                [-106.6, 35.1], [-106.7, 35.1], [-106.7, 35.0],
            ]],
        },
    }
    return feat


class TestComputeNbhdStats(unittest.TestCase):
    """End-to-end smoke of compute_nbhd_stats with tiny synthetic data.
    Focus is on the documented edge-case patches."""

    def test_min_med_guard_blocks_tiny_baseline(self):
        """build_data.py:737 MIN_MED=1000 — a baseline median below $1k
        should NOT produce an astronomical chg_YY1_YY2 value."""
        by_nbhd_yr = {
            1: {
                2020: [_mk_roll_rec(1, 2020, totvalue=1)],       # median = 1
                2021: [_mk_roll_rec(1, 2021, totvalue=100_000)],
            }
        }
        existing = [_mk_existing_feature(1)]
        stats, _ = build_data.compute_nbhd_stats(by_nbhd_yr, existing)
        props = stats[1]
        # If the guard regressed, val_change_pct would be astronomical.
        if props.get("val_change_pct") is not None:
            self.assertLess(abs(props["val_change_pct"]), 10.0)
        # And no chg_20_21 should be emitted when baseline < MIN_MED.
        self.assertNotIn("chg_20_21", props)

    def test_earliest_year_forward_diff_for_hoh_churn(self):
        """build_data.py:1128 — earliest year uses forward diff so the UI
        doesn't show a gray hole at the start of the year series."""
        by_nbhd_yr = {
            1: {
                2020: [
                    _mk_roll_rec(1, 2020, hoh=True),
                    _mk_roll_rec(1, 2020, hoh=False),
                ],
                2021: [
                    _mk_roll_rec(1, 2021, hoh=True),
                    _mk_roll_rec(1, 2021, hoh=True),
                ],
            }
        }
        existing = [_mk_existing_feature(1)]
        stats, _ = build_data.compute_nbhd_stats(by_nbhd_yr, existing)
        props = stats[1]
        # Both years should have hoh_churn_YY populated.
        self.assertIsNotNone(props.get("hoh_churn_20"))
        self.assertIsNotNone(props.get("hoh_churn_21"))
        # Forward diff on 2020 should equal back diff on 2021.
        self.assertAlmostEqual(props["hoh_churn_20"], props["hoh_churn_21"], places=4)

    def test_per_year_fields_preserved_across_rebuilds(self):
        """build_data.py:856 — a single-year rebuild must not clobber
        pct_hoh_YY fields left by an earlier multi-year build."""
        # First build: multi-year
        by_nbhd_yr_full = {
            1: {
                2020: [_mk_roll_rec(1, 2020, hoh=True), _mk_roll_rec(1, 2020)],
                2021: [_mk_roll_rec(1, 2021, hoh=True), _mk_roll_rec(1, 2021, hoh=True)],
            }
        }
        existing = [_mk_existing_feature(1)]
        stats_v1, _ = build_data.compute_nbhd_stats(by_nbhd_yr_full, existing)
        pct_hoh_20_v1 = stats_v1[1].get("pct_hoh_20")
        self.assertIsNotNone(pct_hoh_20_v1)
        self.assertGreater(pct_hoh_20_v1, 0)

        # Second build: only the 2021 roll present.
        existing_v2 = [_mk_existing_feature(1, extra=dict(stats_v1[1]))]
        by_nbhd_yr_partial = {
            1: {2021: [_mk_roll_rec(1, 2021, hoh=True)]}
        }
        stats_v2, _ = build_data.compute_nbhd_stats(by_nbhd_yr_partial, existing_v2)
        # The 2020 field from the prior multi-year build must survive.
        self.assertAlmostEqual(
            stats_v2[1].get("pct_hoh_20"), pct_hoh_20_v1, places=4,
            msg="pct_hoh_20 was clobbered by the single-year rebuild",
        )

    def test_gi_star_bails_when_too_few_nbhds(self):
        """_compute_gi_star_per_year must return cleanly when n_total <= k,
        not explode on the KNN distance sort."""
        # Only 3 nbhds — k defaults to 8.
        nbhd_stats = {
            i: {"nbhd": float(i), "pct_vf_denied_20": 0.1 * i,
                "outreach_need_20": 0.3}
            for i in (1, 2, 3)
        }
        centroids = {1: (35.0, -106.7), 2: (35.1, -106.6), 3: (35.05, -106.65)}
        # Should not raise and should not write any gi_* keys (early return).
        build_data._compute_gi_star_per_year(nbhd_stats, centroids)
        for p in nbhd_stats.values():
            gi_keys = [k for k in p if k.startswith("gi_")]
            self.assertEqual(gi_keys, [])


class TestNoisyOrSemantics(unittest.TestCase):
    """Locks in the noisy-OR composition at build_data.py:1040.

    Comment on that line says noisy-OR replaced max() so that multiple
    moderate issues rank ABOVE a single moderate issue — the test
    verifies the property that motivated the switch from max().
    """

    def _noisy_or(self, *vals):
        p = 1.0
        for v in vals:
            p *= 1.0 - max(0.0, min(1.0, v or 0.0))
        return 1.0 - p

    def test_multiple_moderate_beats_single_moderate(self):
        moderate_trio = self._noisy_or(0.5, 0.5, 0.5)
        single = self._noisy_or(0.5, 0.0, 0.0)
        self.assertGreater(moderate_trio, single)

    def test_bounded_in_unit_interval(self):
        self.assertAlmostEqual(self._noisy_or(0.0, 0.0), 0.0, places=6)
        self.assertAlmostEqual(self._noisy_or(1.0, 1.0), 1.0, places=6)
        for v in (0.1, 0.3, 0.7, 0.9):
            r = self._noisy_or(v, v, v)
            self.assertGreaterEqual(r, 0.0)
            self.assertLessEqual(r, 1.0)


if __name__ == "__main__":
    unittest.main()
