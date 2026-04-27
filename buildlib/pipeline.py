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
import os
import subprocess
import time
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


def _git_sha(repo_dir: Path) -> str | None:
    """Best-effort `git rev-parse HEAD`; returns None outside a checkout."""
    try:
        out = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=str(repo_dir),
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip()
    except Exception:
        return None


def write_build_info(out_path: Path,
                     core_size: int,
                     layers_size: int,
                     nbhd_count: int,
                     parcel_total: int | None = None,
                     acs_year: int | None = None,
                     extra: Mapping[str, object] | None = None) -> Path:
    """Write data/build_info.json — non-secret provenance for the deploy.

    The loader can fetch this to display "data current as of …" without
    needing a password (the file is public-tier by design). The schema
    is intentionally small so it can be eyeballed in a PR review.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        'built_at': int(time.time()),
        'built_iso': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'git_sha': _git_sha(out_path.parent.parent if out_path.parent.name == 'data' else Path('.')),
        'nbhd_count': nbhd_count,
        'core_bytes': core_size,
        'layers_bytes': layers_size,
    }
    if parcel_total is not None:
        payload['parcel_total'] = parcel_total
    if acs_year is not None:
        payload['acs_year'] = acs_year
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return out_path


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
