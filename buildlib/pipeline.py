"""buildlib.pipeline — extracted assembly + write-out stage of build_data.

The full ETL still lives in build_data.py (year loops, MDF integration,
roll/enriched/coord branches), but the mechanical "merge nbhd_stats into
core, pick preserved/rebuilt keys for layers, write both files" tail has
been pulled out so:

  1. Other scripts (e.g. scripts/merge_outreach_dose.py) can re-use the
     same writer to keep the on-disk layout consistent.
  2. The tail can be unit-tested in isolation against synthetic inputs.

This is the first slice of a longer-term refactor; the next planned
extraction is the per-year scoring orchestration.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from .io_utils import safe_int


def merge_nbhd_stats_into_core(existing_core: dict, nbhd_stats: Mapping[int, dict]) -> int:
    """Replace each feature's `properties` with the matching entry from
    nbhd_stats. Returns the count of features that matched."""
    feats = (existing_core.get('DATA') or {}).get('features') or []
    matched = 0
    for feat in feats:
        nbhd = safe_int((feat.get('properties') or {}).get('nbhd'))
        if nbhd and nbhd in nbhd_stats:
            feat['properties'] = nbhd_stats[nbhd]
            matched += 1
    return matched


def assemble_layers(existing_layers: Mapping[str, object],
                    new_layers: Mapping[str, object],
                    preserved_keys: Iterable[str],
                    rebuilt_keys: Iterable[str]) -> dict:
    """Build the final layers.json dict by copying preserved keys from
    `existing_layers` and overwriting `rebuilt_keys` from `new_layers`.

    Missing rebuilt keys default to an empty list to keep the schema
    stable across partial rebuilds.
    """
    out: dict = {}
    for k in preserved_keys:
        if k in existing_layers:
            out[k] = existing_layers[k]
    for k in rebuilt_keys:
        out[k] = new_layers.get(k, [])
    return out


def write_json_compact(path: Path, payload: object) -> int:
    """Write `payload` to `path` as compact JSON; return the on-disk size."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(payload, f, separators=(',', ':'))
    return path.stat().st_size


def write_core_and_layers(existing_core: dict,
                          nbhd_stats: Mapping[int, dict],
                          existing_layers: Mapping[str, object],
                          new_layers: Mapping[str, object],
                          preserved_keys: Iterable[str],
                          rebuilt_keys: Iterable[str],
                          build_nbhd_centers,
                          core_path: Path,
                          layers_path: Path) -> tuple[int, int]:
    """End-to-end: merge stats → existing_core, recompute NBHD_CENTERS,
    assemble layers, write both files. Returns (core_size, layers_size).

    `build_nbhd_centers` is taken as a callable so this module doesn't
    have to import the geometry helper directly (lives in build_data.py
    today). Once that helper moves into buildlib it can be a default arg.
    """
    merge_nbhd_stats_into_core(existing_core, nbhd_stats)
    existing_core['NBHD_CENTERS'] = build_nbhd_centers(existing_core['DATA'])

    final_layers = assemble_layers(existing_layers, new_layers, preserved_keys, rebuilt_keys)

    core_size = write_json_compact(Path(core_path), existing_core)
    layers_size = write_json_compact(Path(layers_path), final_layers)
    return core_size, layers_size
