"""Schema snapshot tests for the analytical layers.

Locks in the *set* of per-nbhd keys the scoring pipeline emits so a silent
rename (or a quietly-dropped layer) trips the test suite instead of
shipping to production. The fixtures don't need real ACS / roll inputs —
synthetic data with enough years is sufficient because the schema only
depends on which scoring functions ran.

Run:
    python -m unittest tests.test_schema
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("build_data", ROOT / "build_data.py")
build_data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_data)


# Per-year layer fields the frontend's year selector flips. Adding a new
# per-year analytical layer to scoring.py without updating the loader's
# propYrFields map (see patch_body.py P4) silently breaks the year scrubber
# for that layer — so the schema test guards both directions.
EXPECTED_PER_YEAR_BASES = {
    'pct_hoh', 'pct_vet', 'pct_val_freeze',
    'hoh_gap', 'vet_gap', 'vf_gap',
    'hoh_uptake', 'vf_uptake', 'vet_uptake',
    'dpi',
    'gi_outreach_need', 'gi_pct_vf_denied',
}

EXPECTED_SCALAR_FIELDS = {
    'outreach_need_slope',
    'pct_hoh_slope', 'pct_vet_slope', 'pct_val_freeze_slope',
    'hoh_churn_slope',
    # Bootstrap CIs added in batch 5 — at least the outreach_need pair
    # should appear given the synthetic test fixture has 6 years.
    'outreach_need_slope_ci_lo', 'outreach_need_slope_ci_hi',
}


def _synthesize(nbhd_count=30, years=range(2020, 2026)):
    """Build a nbhd_stats dict that exercises every scoring function."""
    stats = {}
    for i in range(1, nbhd_count + 1):
        p = {
            'nbhd': float(i),
            'parcels': 200.0 + i,
            'zip_poverty_rate': 0.10 + 0.01 * (i % 5),
            'val_change_pct': 0.05 + 0.01 * (i % 4),
            'tract_pop': 1500 + 50 * i,
            'outreach_need': 0.4,
            'pct_hoh': 0.18,
            'pct_vet': 0.06,
            'pct_val_freeze': 0.04,
        }
        for yr in years:
            yy = yr % 100
            p[f'pct_hoh_{yy}'] = 0.18 + 0.005 * (yr - 2020) + 0.001 * (i % 3)
            p[f'pct_vet_{yy}'] = 0.06 + 0.002 * (yr - 2020)
            p[f'pct_val_freeze_{yy}'] = 0.04 + 0.001 * (yr - 2020)
            p[f'pct_vf_denied_{yy}'] = 0.02 + 0.0005 * i
            p[f'owner_turnover_{yy}'] = 0.15 + 0.005 * (yr - 2020)
            p[f'hoh_churn_{yy}'] = 0.02 + 0.0005 * i
            p[f'outreach_need_{yy}'] = 0.4 + 0.01 * (yr - 2020) + 0.002 * i
        stats[i] = p
    centroids = {i: (35.0 + 0.01 * i, -106.7 + 0.01 * i) for i in stats}
    return stats, centroids


class TestSchemaSnapshot(unittest.TestCase):
    def setUp(self):
        self.stats, self.centroids = _synthesize()
        build_data._compute_exemption_gaps(self.stats)
        build_data._boost_outreach_with_gaps(self.stats)
        build_data._compute_gi_star_per_year(self.stats, self.centroids)
        build_data._compute_dpi_per_year(self.stats)
        build_data._compute_uptake_ratios(self.stats)
        build_data._compute_trend_slopes(self.stats)
        # Smaller bootstrap to keep the test fast.
        build_data._compute_slope_cis(self.stats, n_bootstrap=40)
        build_data._flag_anomalies(self.stats)
        build_data._flag_low_confidence(self.stats)

    def _emitted_per_year_bases(self):
        bases = set()
        for p in self.stats.values():
            for k in p.keys():
                # split off the trailing _YY only when YY is 2 digits
                if '_' in k and k.rsplit('_', 1)[-1].isdigit():
                    bases.add(k.rsplit('_', 1)[0])
        return bases

    def test_expected_per_year_bases_present(self):
        emitted = self._emitted_per_year_bases()
        missing = EXPECTED_PER_YEAR_BASES - emitted
        self.assertFalse(
            missing,
            f"Per-year layer(s) disappeared from scoring output: {sorted(missing)}. "
            "If this is intentional, also remove the base from "
            "patch_body.py's propYrFields patch.",
        )

    def test_expected_scalar_fields_present(self):
        any_nbhd_with = {f: False for f in EXPECTED_SCALAR_FIELDS}
        for p in self.stats.values():
            for f in EXPECTED_SCALAR_FIELDS:
                if p.get(f) is not None:
                    any_nbhd_with[f] = True
        missing = [f for f, present in any_nbhd_with.items() if not present]
        self.assertFalse(
            missing,
            f"Trend-slope field(s) not produced for any synthetic nbhd: {missing}",
        )

    def test_no_unexpected_per_year_bases(self):
        """Tripped when scoring quietly starts emitting a new per-year base.
        Update EXPECTED_PER_YEAR_BASES once the new layer is wired up to
        propYrFields (patch_body.py P4) AND to the legend / radio in the
        body HTML — otherwise the year scrubber won't flip it."""
        emitted = self._emitted_per_year_bases()
        # input bases the synthesizer pre-populates — present but not added by scoring.
        input_bases = {
            'pct_hoh', 'pct_vet', 'pct_val_freeze', 'pct_vf_denied',
            'owner_turnover', 'hoh_churn', 'outreach_need',
        }
        scoring_added = emitted - input_bases
        unexpected = scoring_added - EXPECTED_PER_YEAR_BASES
        self.assertFalse(
            unexpected,
            f"Scoring emitted unexpected per-year base(s) {sorted(unexpected)}. "
            "Add them to EXPECTED_PER_YEAR_BASES and wire them up in "
            "patch_body.py / body HTML so the year selector flips them.",
        )


class TestSlopeCIs(unittest.TestCase):
    def test_ci_brackets_point_estimate(self):
        # Simple, low-noise series → CI should bracket the OLS slope tightly.
        stats = {1: {f'outreach_need_{yy}': 0.2 + 0.05 * (yy - 20)
                     for yy in range(20, 26)}}
        build_data._compute_trend_slopes(stats)
        slope = stats[1]['outreach_need_slope']
        build_data._compute_slope_cis(stats, n_bootstrap=200)
        lo = stats[1]['outreach_need_slope_ci_lo']
        hi = stats[1]['outreach_need_slope_ci_hi']
        self.assertLessEqual(lo, slope)
        self.assertGreaterEqual(hi, slope)

    def test_ci_skipped_when_slope_skipped(self):
        # < 4 years → no slope and no CI.
        stats = {1: {'outreach_need_24': 0.4, 'outreach_need_25': 0.5}}
        build_data._compute_trend_slopes(stats)
        build_data._compute_slope_cis(stats, n_bootstrap=50)
        self.assertNotIn('outreach_need_slope_ci_lo', stats[1])

    def test_ci_deterministic_with_fixed_seed(self):
        # Same input + same seed → same output.
        def mk():
            return {1: {f'pct_hoh_{yy}': 0.18 + 0.005 * (yy - 20) + 0.001 * yy
                        for yy in range(20, 26)}}
        a = mk(); b = mk()
        build_data._compute_trend_slopes(a); build_data._compute_trend_slopes(b)
        build_data._compute_slope_cis(a, n_bootstrap=100, seed=42)
        build_data._compute_slope_cis(b, n_bootstrap=100, seed=42)
        self.assertEqual(a[1]['pct_hoh_slope_ci_lo'], b[1]['pct_hoh_slope_ci_lo'])
        self.assertEqual(a[1]['pct_hoh_slope_ci_hi'], b[1]['pct_hoh_slope_ci_hi'])


class TestAnomalyFlag(unittest.TestCase):
    def test_huge_jump_flagged(self):
        # 30+ ordinary deltas, then one massive one — must trip the 3σ flag.
        stats = {}
        for n in range(1, 41):
            stats[n] = {f'pct_vf_denied_{yy}': 0.02 + 0.001 * n
                        for yy in range(20, 26)}
        # Implant a 50-pp jump in nbhd 99's last year.
        stats[99] = {f'pct_vf_denied_{yy}': 0.02 for yy in range(20, 25)}
        stats[99]['pct_vf_denied_25'] = 0.52
        build_data._flag_anomalies(stats)
        self.assertEqual(stats[99].get('pct_vf_denied_anomaly_yy'), 25)
        self.assertGreater(abs(stats[99].get('pct_vf_denied_anomaly_z', 0)), 3)
        # Untouched nbhds shouldn't trip.
        for n in range(1, 41):
            self.assertNotIn('pct_vf_denied_anomaly_yy', stats[n])

    def test_no_op_with_too_few_deltas(self):
        # < 30 deltas overall → bail out, no flags.
        stats = {1: {'pct_vf_denied_24': 0.0, 'pct_vf_denied_25': 0.5}}
        build_data._flag_anomalies(stats)
        self.assertNotIn('pct_vf_denied_anomaly_yy', stats[1])


class TestLowConfidenceFlag(unittest.TestCase):
    def test_flag_set_for_thin_parcels(self):
        stats = {1: {'parcels': 30, 'tract_pop': 5000}}
        build_data._flag_low_confidence(stats)
        self.assertTrue(stats[1].get('low_confidence'))
        self.assertIn('parcels<', stats[1].get('low_confidence_reason', ''))

    def test_flag_set_for_thin_tract_pop(self):
        stats = {1: {'parcels': 1000, 'tract_pop': 200}}
        build_data._flag_low_confidence(stats)
        self.assertTrue(stats[1].get('low_confidence'))
        self.assertIn('tract_pop<', stats[1].get('low_confidence_reason', ''))

    def test_flag_absent_when_signals_thick(self):
        stats = {1: {'parcels': 1000, 'tract_pop': 5000}}
        build_data._flag_low_confidence(stats)
        self.assertNotIn('low_confidence', stats[1])
        self.assertNotIn('low_confidence_reason', stats[1])

    def test_flag_absent_when_parcels_missing_and_tract_ok(self):
        # No parcels field → don't trip on it. Common for very small / new nbhds.
        stats = {1: {'tract_pop': 5000}}
        build_data._flag_low_confidence(stats)
        self.assertNotIn('low_confidence', stats[1])

    def test_both_reasons_concatenated(self):
        stats = {1: {'parcels': 5, 'tract_pop': 100}}
        build_data._flag_low_confidence(stats)
        reason = stats[1].get('low_confidence_reason', '')
        self.assertIn('parcels<', reason)
        self.assertIn('tract_pop<', reason)


if __name__ == "__main__":
    unittest.main()
