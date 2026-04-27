#!/usr/bin/env python3
"""schema_diff.py — diff the per-feature key sets of two core.json builds.

Loads two `core.json` files (e.g. yesterday's vs today's), gathers the
union of property keys per feature, and reports:

  - keys present in BOTH that changed type
  - keys ADDED in `--new`
  - keys REMOVED from `--old`
  - keys whose population coverage shifted by >= --shift (default 5%)

Useful as a sanity gate when bumping `buildlib/scoring.py`: the schema
snapshot test catches outright renames, this catches the subtler
"field is now populated for half as many tracts" bug.

Exits 0 always; pipe through `--fail-on-change` to make it CI-blocking.

Usage:
  python scripts/schema_diff.py --old prior/core.json --new data/core.json
  python scripts/schema_diff.py --old prior/core.json --new data/core.json --shift 0.10 --fail-on-change
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _summarize(core_path: Path) -> dict:
    """Return {key: (count_present, sample_type)} over all features."""
    core = json.loads(core_path.read_text())
    feats = (core.get('DATA') or {}).get('features') or []
    counts: dict[str, int] = {}
    types: dict[str, str] = {}
    n_total = len(feats)
    for f in feats:
        for k, v in (f.get('properties') or {}).items():
            counts[k] = counts.get(k, 0) + (1 if v is not None else 0)
            if k not in types and v is not None:
                types[k] = type(v).__name__
    return {'n': n_total, 'counts': counts, 'types': types}


def diff(old: dict, new: dict, shift_threshold: float = 0.05) -> list[str]:
    lines = []
    old_keys = set(old['counts'])
    new_keys = set(new['counts'])
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    if added:
        lines.append('## ADDED keys')
        for k in added:
            cov = new['counts'][k] / max(new['n'], 1)
            lines.append(f'  + {k}  ({cov:.0%} populated, type={new["types"].get(k)})')
    if removed:
        lines.append('## REMOVED keys')
        for k in removed:
            cov = old['counts'][k] / max(old['n'], 1)
            lines.append(f'  - {k}  (was {cov:.0%} populated, type={old["types"].get(k)})')
    type_changes = []
    coverage_shifts = []
    for k in sorted(old_keys & new_keys):
        ot, nt = old['types'].get(k), new['types'].get(k)
        if ot and nt and ot != nt:
            type_changes.append(f'  ~ {k}: {ot} → {nt}')
        oc = old['counts'][k] / max(old['n'], 1)
        nc = new['counts'][k] / max(new['n'], 1)
        if abs(nc - oc) >= shift_threshold:
            coverage_shifts.append(f'  ~ {k}: {oc:.0%} → {nc:.0%}')
    if type_changes:
        lines.append('## TYPE changes')
        lines.extend(type_changes)
    if coverage_shifts:
        lines.append(f'## COVERAGE shifts (>= {shift_threshold:.0%})')
        lines.extend(coverage_shifts)
    if not lines:
        lines.append('# no schema differences')
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--old', required=True)
    ap.add_argument('--new', required=True)
    ap.add_argument('--shift', type=float, default=0.05,
                    help='Coverage shift threshold to surface (default 0.05).')
    ap.add_argument('--fail-on-change', action='store_true',
                    help='Exit nonzero if any change is detected.')
    args = ap.parse_args()

    old = _summarize(Path(args.old))
    new = _summarize(Path(args.new))
    print(f'old: {old["n"]} features, {len(old["counts"])} keys')
    print(f'new: {new["n"]} features, {len(new["counts"])} keys')
    print()

    lines = diff(old, new, shift_threshold=args.shift)
    print('\n'.join(lines))

    if args.fail_on_change and any(not l.startswith('# no') for l in lines):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
