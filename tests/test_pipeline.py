"""Tests for buildlib.pipeline (extracted assembly + write stage).

Run:
    python -m unittest tests.test_pipeline
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from buildlib.pipeline import (
    assemble_layers,
    merge_nbhd_stats_into_core,
    write_core_and_layers,
    write_json_compact,
)


class TestMergeNbhdStats(unittest.TestCase):
    def test_replaces_properties_for_matching_nbhds(self):
        core = {
            'DATA': {
                'features': [
                    {'properties': {'nbhd': 1, 'old': True}, 'geometry': None},
                    {'properties': {'nbhd': 2, 'old': True}, 'geometry': None},
                    {'properties': {'nbhd': 99, 'old': True}, 'geometry': None},
                ],
            },
        }
        stats = {1: {'nbhd': 1, 'parcels': 100}, 2: {'nbhd': 2, 'parcels': 200}}
        matched = merge_nbhd_stats_into_core(core, stats)
        self.assertEqual(matched, 2)
        self.assertEqual(core['DATA']['features'][0]['properties'], stats[1])
        self.assertEqual(core['DATA']['features'][1]['properties'], stats[2])
        # No match → properties left alone.
        self.assertEqual(core['DATA']['features'][2]['properties'], {'nbhd': 99, 'old': True})

    def test_handles_null_or_garbage_nbhd(self):
        core = {'DATA': {'features': [
            {'properties': {'nbhd': None}, 'geometry': None},
            {'properties': {'nbhd': 'foo'}, 'geometry': None},
            {'properties': {}, 'geometry': None},
        ]}}
        # Should not crash; nothing matched.
        self.assertEqual(merge_nbhd_stats_into_core(core, {1: {}}), 0)


class TestAssembleLayers(unittest.TestCase):
    def test_preserves_and_rebuilds(self):
        existing = {'TRACT_GEO': 'tract-data', 'OLD_LAYER': 'old', 'GONE': 'x'}
        new = {'HOH_V': [1, 2], 'EG': []}
        out = assemble_layers(
            existing, new,
            preserved_keys=['TRACT_GEO', 'OLD_LAYER'],
            rebuilt_keys=['HOH_V', 'EG'],
        )
        self.assertEqual(out['TRACT_GEO'], 'tract-data')
        self.assertEqual(out['OLD_LAYER'], 'old')
        self.assertEqual(out['HOH_V'], [1, 2])
        self.assertEqual(out['EG'], [])
        # Keys not in either list are dropped.
        self.assertNotIn('GONE', out)

    def test_missing_rebuilt_defaults_to_empty_list(self):
        out = assemble_layers({}, {}, preserved_keys=[], rebuilt_keys=['MISSING'])
        self.assertEqual(out['MISSING'], [])


class TestWriteJsonCompact(unittest.TestCase):
    def test_compact_format_and_size_returned(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'sub' / 'out.json'
            size = write_json_compact(p, {'a': 1, 'b': [2, 3]})
            text = p.read_text()
            self.assertEqual(text, '{"a":1,"b":[2,3]}')
            self.assertEqual(size, len(text))


class TestWriteCoreAndLayersEndToEnd(unittest.TestCase):
    def test_writes_both_files_and_invokes_centers(self):
        existing_core = {'DATA': {'features': [
            {'properties': {'nbhd': 1}, 'geometry': None},
        ]}}
        stats = {1: {'nbhd': 1, 'parcels': 50}}
        captured = {}

        def fake_centers(data):
            captured['called_with'] = data
            return {'1': [35.0, -106.7]}

        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / 'core.json'
            lp = Path(td) / 'layers.json'
            cs, ls = write_core_and_layers(
                existing_core=existing_core,
                nbhd_stats=stats,
                existing_layers={'TRACT_GEO': 'kept'},
                new_layers={'HOH_V': [1, 2, 3]},
                preserved_keys=['TRACT_GEO'],
                rebuilt_keys=['HOH_V'],
                build_nbhd_centers=fake_centers,
                core_path=cp,
                layers_path=lp,
            )
            self.assertGreater(cs, 0)
            self.assertGreater(ls, 0)
            core_back = json.loads(cp.read_text())
            layers_back = json.loads(lp.read_text())
            self.assertEqual(core_back['NBHD_CENTERS'], {'1': [35.0, -106.7]})
            self.assertEqual(core_back['DATA']['features'][0]['properties'], stats[1])
            self.assertEqual(layers_back, {'TRACT_GEO': 'kept', 'HOH_V': [1, 2, 3]})
            self.assertIs(captured['called_with'], existing_core['DATA'])


if __name__ == '__main__':
    unittest.main()
