#!/usr/bin/env python3
"""reconcile_tax_roll.py — flag tracts where parcel-level exemption uptake
diverges from ACS-derived eligibility.

Cross-checks the tax roll against ACS estimates per tract:
  - HOH (head-of-household exemption) uptake vs. estimated owner-occupied
    households eligible for the deduction (proxy: ACS owner-occupied + ≥18
    population). A large negative gap is the data-quality signal — either
    eligible owners aren't claiming, or parcel-tract joins are off.
  - Veteran exemption uptake vs. ACS veteran population per tract
    (B21001_002E). Very small magnitudes; we just emit the ratio.

Inputs:
  * --core <data/core.json>           rebuilt core (post-build_data.py)
  * --tract-acs <data/acs_cache/tracts.json>   tract ACS dict
  * --threshold <float>               min |ratio−1| to flag (default 0.30)

Outputs a tab-separated report on stdout, one row per flagged
tract/neighborhood pair, with the residual + reason. Designed to be
piped into a spreadsheet for staff review.

NOTE: requires the plaintext core.json on disk. The tax roll itself is
not in this repo — `build_data.py --roll` consumes it and writes
core.json with the pct_hoh / pct_vet aggregates this script reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path):
    if not path.exists():
        sys.exit(f"missing: {path}")
    return json.loads(path.read_text())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--core', default='data/core.json')
    ap.add_argument('--tract-acs', default='data/acs_cache/tracts.json')
    ap.add_argument('--threshold', type=float, default=0.30,
                    help='Minimum |ratio - 1| to surface as a flag.')
    args = ap.parse_args()

    core = _load(Path(args.core))
    tract_acs_blob = _load(Path(args.tract_acs))
    by_geoid = tract_acs_blob.get('by_geoid') or {}

    nbhds = (core.get('DATA') or {}).get('features') or []
    print('nbhd\tgeoid\tfield\tactual\tpredicted\tratio\treason')
    flagged = 0
    for f in nbhds:
        p = f.get('properties') or {}
        nbhd = p.get('nbhd')
        geoid = p.get('tract_geoid') or p.get('GEOID')
        if not geoid or geoid not in by_geoid:
            continue
        acs = by_geoid[geoid]
        # HOH cross-check: ACS doesn't ship a clean "HOH-eligible" share,
        # so use 'owner_occupied_share' if the build wrote it; else skip.
        owner_share = p.get('owner_occupied_share') or acs.get('owner_occupied_share')
        pct_hoh = p.get('pct_hoh')
        if owner_share and pct_hoh is not None and owner_share > 0:
            ratio = pct_hoh / owner_share
            if abs(ratio - 1) >= args.threshold:
                reason = 'over' if ratio > 1 else 'under'
                print(f'{nbhd}\t{geoid}\tpct_hoh\t{pct_hoh:.4f}\t{owner_share:.4f}\t{ratio:.2f}\t{reason}')
                flagged += 1
        # Veteran cross-check: B21001_002E count over total pop.
        vet_count = acs.get('vet_count')
        tract_pop = acs.get('tract_pop') or 1
        pct_vet = p.get('pct_vet')
        if vet_count and pct_vet is not None and tract_pop:
            acs_vet_share = vet_count / tract_pop
            if acs_vet_share > 0:
                ratio = pct_vet / acs_vet_share
                if abs(ratio - 1) >= args.threshold:
                    reason = 'over' if ratio > 1 else 'under'
                    print(f'{nbhd}\t{geoid}\tpct_vet\t{pct_vet:.4f}\t{acs_vet_share:.4f}\t{ratio:.2f}\t{reason}')
                    flagged += 1
    print(f'# flagged: {flagged} (threshold |r-1|>={args.threshold})', file=sys.stderr)


if __name__ == '__main__':
    main()
