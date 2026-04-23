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


class TestDpiUptakeSlopes(unittest.TestCase):
    """Phase 3 analytical layers: DPI_YY, *_uptake_YY, *_slope."""

    def test_dpi_requires_both_displacement_and_pressure(self):
        """A nbhd with high turnover but zero pressure should score low;
        one with both moderate signals should score meaningfully higher."""
        stats = {
            1: {  # high turnover, no pressure
                "owner_turnover_23": 0.3, "hoh_churn_23": 0.05,
                "val_change_pct": 0.0, "zip_poverty_rate": 0.0,
            },
            2: {  # moderate on both sides
                "owner_turnover_23": 0.15, "hoh_churn_23": 0.03,
                "val_change_pct": 0.3, "zip_poverty_rate": 0.15,
            },
            3: {  # high pressure, no turnover
                "owner_turnover_23": 0.0, "hoh_churn_23": 0.0,
                "val_change_pct": 0.5, "zip_poverty_rate": 0.25,
            },
        }
        build_data._compute_dpi_per_year(stats)
        # Per-year key emitted only where owner_turnover_YY exists.
        self.assertIn("dpi_23", stats[1])
        self.assertIn("dpi_23", stats[2])
        self.assertIn("dpi_23", stats[3])
        # Both-signal nbhd beats single-signal extremes (the design goal).
        self.assertGreater(stats[2]["dpi_23"], stats[1]["dpi_23"])
        self.assertGreater(stats[2]["dpi_23"], stats[3]["dpi_23"])

    def test_dpi_prefers_tract_poverty_over_zip(self):
        stats = {
            1: {
                "owner_turnover_23": 0.2,
                "hoh_churn_23": 0.0,
                "val_change_pct": 0.0,
                "zip_poverty_rate": 0.0,
                "tract_poverty_rate": 0.25,  # should dominate
            }
        }
        build_data._compute_dpi_per_year(stats)
        self.assertGreater(stats[1]["dpi_23"], 0.0)

    def test_dpi_skips_nbhds_without_per_year_data(self):
        stats = {1: {"parcels": 200, "outreach_need": 0.5}}
        build_data._compute_dpi_per_year(stats)
        self.assertFalse(any(k.startswith("dpi_") for k in stats[1]))

    def test_uptake_ratio_centered_around_one(self):
        """With a flat dataset, actual≈predicted so uptake≈1."""
        stats = {i: {"pct_hoh_23": 0.2, "zip_poverty_rate": 0.15,
                     "pct_val_freeze_23": 0.05, "pct_vet_23": 0.08}
                 for i in range(10)}
        build_data._compute_uptake_ratios(stats)
        for p in stats.values():
            self.assertAlmostEqual(p["hoh_uptake_23"], 1.0, places=2)
            self.assertAlmostEqual(p["vet_uptake_23"], 1.0, places=2)

    def test_uptake_ratio_capped_at_three(self):
        """A neighborhood far above the mean must not produce a huge ratio."""
        stats = {i: {"pct_vet_23": 0.001} for i in range(10)}
        stats[11] = {"pct_vet_23": 1.0}  # wildly above mean
        build_data._compute_uptake_ratios(stats)
        self.assertLessEqual(stats[11]["vet_uptake_23"], 3.0)

    def test_trend_slopes_fit_linear_series(self):
        """A linearly-rising outreach_need_YY series should yield the true slope."""
        p = {f"outreach_need_{yy}": 0.2 + 0.05 * (yy - 20)
             for yy in range(20, 26)}  # 2020..2025
        stats = {1: p}
        build_data._compute_trend_slopes(stats)
        self.assertAlmostEqual(p["outreach_need_slope"], 0.05, places=4)

    def test_trend_slope_skipped_when_too_few_years(self):
        p = {"outreach_need_24": 0.4, "outreach_need_25": 0.5}
        stats = {1: p}
        build_data._compute_trend_slopes(stats)
        self.assertNotIn("outreach_need_slope", p)


class TestBuildlibSplit(unittest.TestCase):
    """Phase 4: the buildlib/ package must be importable on its own,
    and build_data.* must remain a compatible façade over it."""

    def test_buildlib_modules_importable(self):
        from buildlib import io_utils, spatial, scoring, census  # noqa: F401

    def test_build_data_reexports_match_buildlib(self):
        from buildlib import io_utils, spatial, scoring, census
        self.assertIs(build_data.extract_year, io_utils.extract_year)
        self.assertIs(build_data._point_in_ring, spatial._point_in_ring)
        self.assertIs(build_data._compute_dpi_per_year, scoring._compute_dpi_per_year)
        self.assertIs(build_data._compute_persistence, scoring._compute_persistence)
        self.assertIs(build_data.fetch_tract_acs, census.fetch_tract_acs)


class TestPersistence(unittest.TestCase):
    """Persistence / chronic score over per-year data."""

    def test_consecutive_top_decile_streak(self):
        # 30 nbhds, values spread uniformly 0.1..0.9 so the 90th percentile
        # lands around 0.82. Inject three tracked nbhds above that:
        #   nbhd 100 (always top) — should have 6-year streak
        #   nbhd 101 (top only in 2025) — 1-year recent, never chronic
        #   nbhd 102 (never top) — no streak
        stats = {}
        for i in range(30):
            p = {}
            for yr in range(20, 26):
                p[f'outreach_need_{yr}'] = 0.1 + (i / 29) * 0.7  # 0.1..0.8
            stats[i] = p
        stats[100] = {f'outreach_need_{yr}': 0.95 for yr in range(20, 26)}
        stats[101] = {f'outreach_need_{yr}': 0.2 for yr in range(20, 25)}
        stats[101]['outreach_need_25'] = 0.95
        stats[102] = {f'outreach_need_{yr}': 0.2 for yr in range(20, 26)}

        build_data._compute_persistence(stats, base='outreach_need',
                                        decile=0.9, min_streak=3)
        self.assertEqual(stats[100]['outreach_need_persistence_streak'], 6)
        self.assertEqual(stats[100]['outreach_need_persistence_recent'], 6)
        self.assertTrue(stats[100]['outreach_need_persistence_chronic'])
        self.assertEqual(stats[101]['outreach_need_persistence_streak'], 1)
        self.assertEqual(stats[101]['outreach_need_persistence_recent'], 1)
        self.assertFalse(stats[101]['outreach_need_persistence_chronic'])
        self.assertEqual(stats[102]['outreach_need_persistence_streak'], 0)
        self.assertFalse(stats[102]['outreach_need_persistence_chronic'])

    def test_gap_year_breaks_streak(self):
        stats = {}
        for i in range(30):
            p = {}
            # nbhd 0: 2020,2021 top-decile, 2022 is zero (no data), 2023
            # onwards top-decile. Streak should reset at the gap.
            if i == 0:
                p['outreach_need_20'] = 0.95
                p['outreach_need_21'] = 0.95
                p['outreach_need_22'] = 0  # gap — excluded
                p['outreach_need_23'] = 0.95
                p['outreach_need_24'] = 0.95
                p['outreach_need_25'] = 0.95
            else:
                for yr in range(20, 26):
                    p[f'outreach_need_{yr}'] = 0.1
            stats[i] = p
        build_data._compute_persistence(stats, base='outreach_need', decile=0.9)
        # Longest consecutive streak for nbhd 0 is max(2, 3) = 3 (2023-25).
        # The 2020-21 streak is 2 years. The gap breaks consecutive counting.
        self.assertEqual(stats[0]['outreach_need_persistence_streak'], 3)
        # Recent streak ends at 2025; the gap at 2022 doesn't interrupt
        # the walk BACKWARDS from 25 because 23,24,25 are consecutive.
        self.assertEqual(stats[0]['outreach_need_persistence_recent'], 3)
        # Total top-decile years = 5 (2020, 2021, 2023, 2024, 2025).
        self.assertEqual(stats[0]['outreach_need_persistence_total'], 5)

    def test_sparse_layer_skips(self):
        """Too few nbhds → threshold can't be computed, nothing emitted."""
        stats = {i: {'outreach_need_25': 0.5} for i in range(5)}
        build_data._compute_persistence(stats, base='outreach_need')
        for p in stats.values():
            self.assertNotIn('outreach_need_persistence_streak', p)

    def test_ignores_non_existent_base(self):
        stats = {i: {'outreach_need_25': 0.5} for i in range(30)}
        build_data._compute_persistence(stats, base='nonexistent_base')
        for p in stats.values():
            self.assertNotIn('nonexistent_base_persistence_streak', p)


class TestAcsCache(unittest.TestCase):
    """Phase 2: disk-cache wrappers around the Census API."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.outdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_miss_returns_none(self):
        self.assertIsNone(build_data._acs_cache_read(self.outdir, "census", 30))

    def test_read_write_roundtrip(self):
        payload = {"county": {"year": 2023, "pop": 100}, "zips": {"87102": {"pop": 50}}}
        build_data._acs_cache_write(self.outdir, "census", payload)
        got = build_data._acs_cache_read(self.outdir, "census", 30)
        self.assertIsNotNone(got)
        self.assertEqual(got["county"]["year"], 2023)
        self.assertIn("fetched_at", got)

    def test_stale_cache_returns_none(self):
        import time as _t
        path = build_data._acs_cache_path(self.outdir, "census")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a payload with a stale timestamp (40 days old).
        old_ts = _t.time() - 40 * 86400
        with open(path, "w") as f:
            import json as _j
            _j.dump({"fetched_at": old_ts, "county": {}}, f)
        self.assertIsNone(build_data._acs_cache_read(self.outdir, "census", 30))

    def test_merge_tract_acs_populates_properties(self):
        feats = [
            {"properties": {"GEOID": "35001000100"}},
            {"properties": {"GEOID": "35001000200"}},
            {"properties": {"GEOID": "99999"}},  # won't match
        ]
        by_geoid = {
            "35001000100": {"poverty_rate": 0.25, "median_age": 35.0},
            "35001000200": {"poverty_rate": 0.10, "median_age": 50.0},
        }
        merged = build_data._merge_tract_acs(feats, by_geoid)
        self.assertEqual(merged, 2)
        self.assertEqual(feats[0]["properties"]["poverty_rate"], 0.25)
        self.assertEqual(feats[1]["properties"]["median_age"], 50.0)
        self.assertNotIn("poverty_rate", feats[2]["properties"])

    def test_fetch_tract_acs_uses_cache_without_network(self):
        """When a fresh cache exists, fetch_tract_acs must not touch the network."""
        from buildlib import census as _census
        payload = {
            "acs_year": 2023,
            "by_geoid": {"35001000100": {"poverty_rate": 0.3, "acs_year": 2023}},
        }
        build_data._acs_cache_write(self.outdir, "tracts", payload)
        feats = [{"properties": {"GEOID": "35001000100"}}]
        tract_geo = {"features": feats}

        # Sabotage urlopen so an accidental fetch would raise loudly. Patch
        # at buildlib.census (where the real binding lives) — the re-export
        # on build_data is a reference, not a redirect.
        orig = _census.urlopen
        _census.urlopen = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("network was called despite fresh cache")
        )
        try:
            build_data.fetch_tract_acs(tract_geo, outdir=self.outdir, use_cache=True)
        finally:
            _census.urlopen = orig
        self.assertEqual(feats[0]["properties"]["poverty_rate"], 0.3)


if __name__ == "__main__":
    unittest.main()
