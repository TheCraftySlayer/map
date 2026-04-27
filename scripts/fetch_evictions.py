#!/usr/bin/env python3
"""fetch_evictions.py — load UNM/Eviction Lab tract-level eviction filing
counts for Bernalillo County into data/layers.json.

The Eviction Lab (Princeton/UNM partnership) publishes county- and tract-
level historical filing data. There is no public API as of writing — the
operator downloads the per-state CSV from https://evictionlab.org/ and
points this script at it.

Inputs:
  --csv <path>      Path to the New Mexico tract CSV (manually downloaded).
  --year <YYYY>     Single year to surface (latest in the file by default).
  --out-layers      data/layers.json (in-place merge; preserves other layers).

Output layer schema (one entry per Bernalillo tract):
  {
    "geoid": "35001000100",
    "year": 2019,
    "filings": 124,
    "filing_rate": 0.034,        # filings / renter_households
    "judgments": 87
  }

If --csv is omitted and EVICTION_LAB_URL is configured in the environment,
the script will GET it directly. The default fallback exits with an error
so the operator always confirms the data source.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

BERN_FIPS_PREFIX = '35001'  # state=35 NM, county=001 Bernalillo


def parse(csv_path: Path, year: int | None) -> list[dict]:
    rows = []
    with csv_path.open(newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            geoid = r.get('GEOID') or r.get('geoid') or ''
            if not geoid.startswith(BERN_FIPS_PREFIX):
                continue
            try:
                yr = int(r.get('year') or r.get('Year') or 0)
            except ValueError:
                continue
            if year is not None and yr != year:
                continue
            try:
                filings = int(float(r.get('eviction-filings') or r.get('filings') or 0))
                judgments = int(float(r.get('evictions') or r.get('judgments') or 0))
                rate = r.get('eviction-filing-rate') or r.get('filing_rate')
                rate = float(rate) / 100.0 if rate else None
            except (TypeError, ValueError):
                continue
            rows.append({
                'geoid': geoid,
                'year': yr,
                'filings': filings,
                'judgments': judgments,
                'filing_rate': rate,
            })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', help='Path to the Eviction Lab NM tract CSV.')
    ap.add_argument('--url', help='HTTPS URL to fetch the CSV from (overrides --csv).')
    ap.add_argument('--year', type=int, help='Single year to surface (default: latest).')
    ap.add_argument('--out-layers', default='data/layers.json')
    args = ap.parse_args()

    if args.url:
        with urlopen(args.url, timeout=30) as r:
            tmp = Path('data/eviction_cache.csv')
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(r.read())
        csv_path = tmp
    elif args.csv:
        csv_path = Path(args.csv)
    else:
        env = os.environ.get('EVICTION_LAB_URL')
        if env:
            with urlopen(env, timeout=30) as r:
                tmp = Path('data/eviction_cache.csv')
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(r.read())
            csv_path = tmp
        else:
            sys.exit('Provide --csv or --url (or set EVICTION_LAB_URL). The Eviction Lab '
                     'requires a manual download or licensed mirror.')

    rows = parse(csv_path, args.year)
    if args.year is None and rows:
        latest = max(r['year'] for r in rows)
        rows = [r for r in rows if r['year'] == latest]
        print(f'  using latest year: {latest}')
    print(f'  parsed {len(rows)} Bernalillo tract-years')

    out_path = Path(args.out_layers)
    if out_path.exists():
        layers = json.loads(out_path.read_text())
    else:
        layers = {}
    layers['eviction_filings'] = rows
    out_path.write_text(json.dumps(layers, separators=(',', ':')))
    print(f'  wrote {out_path}')


if __name__ == '__main__':
    main()
