"""Tests for patch_body.py — the engine itself, not the JS payloads.

Specifically verifies:
  * The new marker-based idempotency check prevents double-injection when
    the new content embeds the old anchor as a substring (e.g. P5/P6/P7+).
  * The engine still applies a 3-tuple patch (legacy form) the same way.
  * Missing anchors are reported, not silently skipped.

Run:
    python -m unittest tests.test_patch_body
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("patch_body", ROOT / "patch_body.py")
patch_body = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch_body)


def _run_patches(text, patches):
    """Drive patch_body.main() against an in-memory text using a temp file."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "body.html"
        p.write_text(text, encoding="utf-8")
        with unittest.mock.patch.object(patch_body, "PATCHES", patches), \
             unittest.mock.patch.object(sys, "argv",
                ["patch_body.py", str(p), "--no-backup"]), \
             unittest.mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            try:
                patch_body.main()
            except SystemExit:
                # main() exits when nothing applies; that's not a test failure.
                pass
        return p.read_text(encoding="utf-8")


class TestMarkerIdempotency(unittest.TestCase):
    def test_marker_blocks_reapply_when_new_embeds_old(self):
        """P5/P6/P7+ pattern: new content keeps the old anchor as a prefix
        so the engine would otherwise re-fire on each invocation."""
        text = "</body></html>\n"
        patches = [(
            "embed-anchor",
            "</body></html>\n",
            "<script>/*MARK*/...</script></body></html>\n",
            "/*MARK*/",
        )]
        once = _run_patches(text, patches)
        twice = _run_patches(once, patches)
        self.assertEqual(once, twice, "marker failed to block second application")
        self.assertEqual(once.count("/*MARK*/"), 1)

    def test_legacy_three_tuple_still_works(self):
        text = "FOO\n"
        patches = [("legacy", "FOO\n", "BAR\n")]
        out = _run_patches(text, patches)
        self.assertEqual(out, "BAR\n")
        # Re-running on already-patched text is a no-op (FOO is gone).
        self.assertEqual(_run_patches(out, patches), out)

    def test_missing_anchor_is_reported_not_silently_skipped(self):
        text = "nothing matching here\n"
        patches = [("absent", "ZZZ", "YYY")]
        # main() will sys.exit(1) when patches go missing; capture that.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "body.html"
            p.write_text(text, encoding="utf-8")
            with unittest.mock.patch.object(patch_body, "PATCHES", patches), \
                 unittest.mock.patch.object(sys, "argv",
                    ["patch_body.py", str(p), "--no-backup"]), \
                 unittest.mock.patch.object(sys, "stdout", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit) as cm:
                patch_body.main()
            self.assertEqual(cm.exception.code, 1)
            # File untouched.
            self.assertEqual(p.read_text(encoding="utf-8"), text)


class TestExistingPatchesShape(unittest.TestCase):
    """The shipped PATCHES list: every entry that injects-via-embed has a marker."""

    def test_embed_anchor_patches_have_markers(self):
        # Every patch whose NEW content embeds its OLD anchor as a substring
        # must have an idempotency marker. The naming convention is _V1 etc.
        embed_prefixes = (
            "MAP_EXT_V1", "PDF_EXPORT_V1", "INSIGHTS_V1", "TOOLS_V1",
            "REPORTS_V1", "ANNOTATE_V1", "COMPARE_V1", "DECIDE_V1", "VIZ_V1",
            "FIELD_V1", "PASTEVENTS_V1", "CHORO_CSV_V1", "BODY_CLEAN_V1",
            "WORKLIST_CSV_FIX_V1", "RADAR_V1", "CLUSTER_SANKEY_V1",
        )
        for entry in patch_body.PATCHES:
            name = entry[0]
            if any(name.startswith(p) for p in embed_prefixes):
                self.assertEqual(len(entry), 4,
                    f"Patch '{name}' is an embed-anchor patch and needs a marker")

    def test_each_marker_appears_exactly_once_in_its_new(self):
        for entry in patch_body.PATCHES:
            if len(entry) != 4:
                continue
            name, _, new, marker = entry
            self.assertIn(marker, new,
                f"marker {marker!r} for '{name}' is not in its NEW content")
            self.assertEqual(new.count(marker), 1,
                f"marker {marker!r} for '{name}' appears multiple times in NEW")

    def test_no_two_patches_share_a_marker(self):
        markers = [e[3] for e in patch_body.PATCHES if len(e) == 4]
        self.assertEqual(len(markers), len(set(markers)),
            "two patches share an idempotency marker")


class TestWorklistCsvFix(unittest.TestCase):
    """P18 must repair the P5 worklist CSV: drop the broken `typeof nbhdLayer`
    guard and substitute the Leaflet _layers walk used by P16."""

    def _by_name(self, prefix):
        for entry in patch_body.PATCHES:
            if entry[0].startswith(prefix):
                return entry
        return None

    def test_p18_replaces_broken_global_lookup(self):
        p5 = self._by_name("MAP_EXT_V1")
        p18 = self._by_name("WORKLIST_CSV_FIX_V1")
        self.assertIsNotNone(p5, "P5 (MAP_EXT_V1) missing from PATCHES")
        self.assertIsNotNone(p18, "P18 (WORKLIST_CSV_FIX_V1) missing from PATCHES")

        p5_new = p5[2]
        p18_old, p18_new = p18[1], p18[2]

        # P18's OLD anchor must be a substring of P5's NEW (otherwise P18
        # would never find anything to swap once P5 has been applied).
        self.assertIn(p18_old, p5_new,
            "P18 OLD anchor not present in P5 NEW — patches don't compose")

        # The broken global-lookup guard goes away in the fixed version.
        self.assertIn("typeof nbhdLayer!=='undefined'", p18_old)
        self.assertNotIn("typeof nbhdLayer!=='undefined'", p18_new)

        # The fix uses the same shape as P16: walk the map's _layers.
        self.assertIn("_layers", p18_new)
        self.assertIn("nbhd", p18_new)

    def test_p18_round_trips_against_synthetic_p5_body(self):
        # Build a minimal seed that lets P5 then P18 both apply, and
        # confirm the resulting body has the marker and lacks the
        # broken guard.
        text = "</body></html>\n"
        # Filter the live PATCHES list to just P5 and P18 so the test
        # doesn't depend on the rest of the chain.
        keep = [e for e in patch_body.PATCHES
                if e[0].startswith(("MAP_EXT_V1", "WORKLIST_CSV_FIX_V1"))]
        self.assertEqual(len(keep), 2, "expected exactly P5 and P18 in PATCHES")
        out = _run_patches(text, keep)
        self.assertIn("/*WORKLIST_CSV_FIX_V1*/", out)
        # The broken guard must not survive the chain.
        self.assertNotIn("typeof nbhdLayer!=='undefined'", out)
        # The fixed function should be the only downloadWorklist defn.
        self.assertEqual(out.count("function downloadWorklist("), 1)


class TestRadarSankeyChain(unittest.TestCase):
    """P20 (RADAR_V1) must chain onto P16 (CHORO_CSV_V1)'s marker, and
    P21 (CLUSTER_SANKEY_V1) onto P20's marker. Both are end-of-chain
    body-tail patches, so the marker convention matters."""

    def _by_name(self, prefix):
        for entry in patch_body.PATCHES:
            if entry[0].startswith(prefix):
                return entry
        return None

    def test_radar_anchors_on_choro_csv_marker(self):
        p20 = self._by_name("RADAR_V1")
        self.assertIsNotNone(p20)
        self.assertEqual(p20[1], "/*CHORO_CSV_V1*/")
        self.assertIn("/*RADAR_V1*/", p20[2])

    def test_sankey_anchors_on_radar_marker(self):
        p21 = self._by_name("CLUSTER_SANKEY_V1")
        self.assertIsNotNone(p21)
        self.assertEqual(p21[1], "/*RADAR_V1*/")
        self.assertIn("/*CLUSTER_SANKEY_V1*/", p21[2])

    def test_chain_round_trips_from_choro_marker(self):
        # Synthetic body that already carries the CHORO_CSV_V1 marker —
        # then P20 and P21 should land in order.
        text = "prefix /*CHORO_CSV_V1*/ suffix\n"
        keep = [e for e in patch_body.PATCHES
                if e[0].startswith(("RADAR_V1", "CLUSTER_SANKEY_V1"))]
        self.assertEqual(len(keep), 2)
        out = _run_patches(text, keep)
        self.assertIn("/*RADAR_V1*/", out)
        self.assertIn("/*CLUSTER_SANKEY_V1*/", out)
        # Idempotent re-application.
        twice = _run_patches(out, keep)
        self.assertEqual(out, twice)
        self.assertEqual(out.count("/*RADAR_V1*/"), 1)
        self.assertEqual(out.count("/*CLUSTER_SANKEY_V1*/"), 1)


if __name__ == "__main__":
    unittest.main()
