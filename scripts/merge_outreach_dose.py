#!/usr/bin/env python3
"""merge_outreach_dose.py — overlay Assessor outreach spend / staff-hours
against per-neighborhood outreach_need_YY.

Reads a CSV the operator drops in (`inputs/outreach_dose.csv`) with one
row per (year, nbhd) and either a dollar amount or a staff-hour count.
Joins it onto data/core.json so the frontend can render a "dose vs.
need" diverging-color layer.

Expected CSV columns (case-insensitive; flexible — any subset works):
  nbhd, year, dose_usd, staff_hours, events, contacts

Output: writes core.json in place, adding per-nbhd:
  outreach_dose_YY        (sum of dose_usd for that year)
  outreach_hours_YY       (sum of staff_hours for that year)
  outreach_dose_ratio_YY  (dose_usd / outreach_need_YY, normalized to
                           the county-wide median so a 1.0 = "average
                           amount of attention per unit need")

If outreach_need_YY is missing for a (nbhd, year) the ratio is left
absent rather than dividing by zero.

Usage:
  python scripts/merge_outreach_dose.py \\
      --csv inputs/outreach_dose.csv --core data/core.json
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _yy(yr):
    try:
        y = int(yr)
    except (TypeError, ValueError):
        return None
    return y % 100 if y >= 100 else y


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--core', default='data/core.json')
    args = ap.parse_args()

    rows_by_nbhd_yy: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: {'dose_usd': 0.0, 'hours': 0.0, 'events': 0, 'contacts': 0}
    )
    with open(args.csv, newline='') as f:
        reader = csv.DictReader(f)
        # case-insensitive col lookup
        cmap = {c.lower(): c for c in (reader.fieldnames or [])}
        nbhd_col = cmap.get('nbhd')
        year_col = cmap.get('year')
        if not nbhd_col or not year_col:
            sys.exit('CSV needs at minimum nbhd and year columns')
        dose_col = cmap.get('dose_usd') or cmap.get('dollars')
        hours_col = cmap.get('staff_hours') or cmap.get('hours')
        events_col = cmap.get('events')
        contacts_col = cmap.get('contacts')
        for r in reader:
            try:
                nbhd = int(float(r[nbhd_col]))
            except (TypeError, ValueError):
                continue
            yy = _yy(r[year_col])
            if yy is None:
                continue
            bucket = rows_by_nbhd_yy[(nbhd, yy)]
            try:
                if dose_col:
                    bucket['dose_usd'] += float(r.get(dose_col) or 0)
                if hours_col:
                    bucket['hours'] += float(r.get(hours_col) or 0)
                if events_col:
                    bucket['events'] += int(float(r.get(events_col) or 0))
                if contacts_col:
                    bucket['contacts'] += int(float(r.get(contacts_col) or 0))
            except (TypeError, ValueError):
                continue

    core = json.loads(Path(args.core).read_text())
    feats = (core.get('DATA') or {}).get('features') or []

    # First pass: write the dose / hours fields on each feature.
    pending_ratios = []
    for f in feats:
        p = f.get('properties') or {}
        try:
            nbhd = int(p.get('nbhd'))
        except (TypeError, ValueError):
            continue
        for (n, yy), b in rows_by_nbhd_yy.items():
            if n != nbhd:
                continue
            if b['dose_usd']:
                p[f'outreach_dose_{yy:02d}'] = round(b['dose_usd'], 2)
            if b['hours']:
                p[f'outreach_hours_{yy:02d}'] = round(b['hours'], 1)
            need = p.get(f'outreach_need_{yy:02d}')
            if need and b['dose_usd'] and need > 0:
                pending_ratios.append((p, yy, b['dose_usd'] / need))

    # Normalize ratios to the county median so 1.0 = "typical attention".
    if pending_ratios:
        med = statistics.median(r for _, _, r in pending_ratios)
        if med > 0:
            for p, yy, r in pending_ratios:
                p[f'outreach_dose_ratio_{yy:02d}'] = round(r / med, 4)

    Path(args.core).write_text(json.dumps(core, separators=(',', ':')))
    print(f'merge_outreach_dose: {len(rows_by_nbhd_yy)} (nbhd,year) rows merged into {args.core}')
    print(f'  ratios computed: {len(pending_ratios)} (normalized to median={med if pending_ratios else "n/a"})')


if __name__ == '__main__':
    main()
