#!/usr/bin/env python3
"""cluster_snapshot.py — collapse the per-nbhd cluster signal in core.json
into a small JSON summary suitable for committing to the repo.

The summary is a deliberately redacted view: only `nbhd`, `cluster`,
`outreach_need_bin`, `dpi_bin`, and `low_confidence` per neighborhood.
Parcel-level / value-level fields are excluded so this file can be
checked in without leaking what the encrypted build protects.

Used by .github/workflows/nightly-acs-rebuild.yml to detect cluster
category changes between rebuilds and open a PR when they cross the
threshold for human review.

Usage:
  # Full snapshot
  python scripts/cluster_snapshot.py --in data/core.json --out core_summary.json

  # Diff against a previous snapshot (same path is allowed; the previous
  # version is read first and then overwritten):
  python scripts/cluster_snapshot.py --in data/core.json \\
      --out core_summary.json --prev core_summary.json --diff cluster_diff.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bin(v, edges):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    for i, e in enumerate(edges):
        if x < e:
            return i
    return len(edges)


def _classify(props):
    """Coarse-grained classification used for diff stability — three bins
    each for outreach_need and dpi keep small ACS jitter from generating
    a PR every night."""
    on = props.get('outreach_need')
    dpi = props.get('dpi') or props.get('dpi_23') or props.get('dpi_24')
    gi_on = props.get('gi_outreach_need') or props.get('gi_outreach_need_23')
    cluster = None
    if isinstance(gi_on, (int, float)):
        if gi_on >= 1.96:
            cluster = 'hot'
        elif gi_on <= -1.96:
            cluster = 'cold'
        else:
            cluster = 'neutral'
    return {
        'cluster': cluster,
        'outreach_need_bin': _bin(on, [0.33, 0.66]),
        'dpi_bin': _bin(dpi, [0.10, 0.30]),
        'low_confidence': bool(props.get('low_confidence')),
    }


def snapshot(core_path: Path) -> dict:
    core = json.loads(core_path.read_text())
    feats = (core.get('DATA') or {}).get('features') or []
    out = {}
    for f in feats:
        p = f.get('properties') or {}
        nbhd = p.get('nbhd')
        if nbhd is None:
            continue
        out[str(int(nbhd))] = _classify(p)
    return out


def diff(prev: dict, cur: dict) -> list[str]:
    lines = []
    keys = sorted(set(prev) | set(cur), key=lambda k: int(k) if k.isdigit() else k)
    for k in keys:
        a = prev.get(k)
        b = cur.get(k)
        if a == b:
            continue
        if a is None:
            lines.append(f'+ {k}: new {b}')
        elif b is None:
            lines.append(f'- {k}: removed (was {a})')
        else:
            changes = []
            for f in ('cluster', 'outreach_need_bin', 'dpi_bin', 'low_confidence'):
                if a.get(f) != b.get(f):
                    changes.append(f'{f}: {a.get(f)}→{b.get(f)}')
            if changes:
                lines.append(f'~ {k}: ' + ', '.join(changes))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--in', dest='inp', required=True, help='Path to core.json')
    ap.add_argument('--out', required=True, help='Path to write the snapshot JSON')
    ap.add_argument('--prev', help='Optional prior snapshot to diff against')
    ap.add_argument('--diff', help='Optional path to write the human-readable diff')
    args = ap.parse_args()

    inp = Path(args.inp)
    if not inp.exists():
        # No-op when the unencrypted core.json isn't present (the typical
        # case in CI). The workflow still wants a 0 exit so subsequent
        # steps can decide whether to skip.
        print(f'cluster_snapshot: {inp} not present; skipping')
        return 0

    cur = snapshot(inp)

    prev = {}
    if args.prev and Path(args.prev).exists():
        try:
            prev = json.loads(Path(args.prev).read_text())
        except Exception as e:
            print(f'cluster_snapshot: prior {args.prev} unreadable ({e}); treating as empty')

    Path(args.out).write_text(json.dumps(cur, indent=2, sort_keys=True))
    print(f'cluster_snapshot: wrote {args.out} ({len(cur)} nbhds)')

    if args.diff:
        lines = diff(prev, cur)
        Path(args.diff).write_text('\n'.join(lines) + ('\n' if lines else ''))
        print(f'cluster_snapshot: {len(lines)} diff lines → {args.diff}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
