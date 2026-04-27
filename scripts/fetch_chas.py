#!/usr/bin/env python3
"""fetch_chas.py — pull HUD CHAS renter cost-burden tables for Bernalillo
County tracts and merge into data/layers.json as a renter_cost_burden layer.

CHAS background:
  HUD's Comprehensive Housing Affordability Strategy data ships ACS-derived
  cost-burden splits (renters paying >30%, >50% of income on housing) at
  the Census tract level, refreshed annually. We use the public HUD data
  portal CSV — no API key required.

What this writes:
  data/chas_cache/<year>.csv   raw download (cached, gitignored)
  data/layers.json             gets a `renter_cost_burden` array of
                               {geoid, total, burden_30, burden_50, share_30, share_50}

Usage:
  python scripts/fetch_chas.py --year 2017-2021 \
      --out-layers data/layers.json --cache data/chas_cache

NOTE: HUD updates the CHAS URL pattern occasionally. The default below is
the 2017-2021 vintage as of writing; pass --url to override when HUD
publishes a newer release.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen, Request

# HUD CHAS download base. The "tract" file inside the ZIP contains
# Bernalillo County tracts (state=35, county=001).
DEFAULT_URL = (
    'https://www.huduser.gov/portal/datasets/cp/2017thru2021-140-csv.zip'
)
BERN_STATE = '35'
BERN_COUNTY = '001'

# CHAS table 8 maps to renter cost-burden splits. The exact column names
# differ by vintage — these are the 2017-2021 names. Override with
# --columns if HUD renames them.
DEFAULT_COLS = {
    'total':     'T8_est1',   # all renter households
    'burden_30': 'T8_est10',  # renters cost-burdened (>30%)
    'burden_50': 'T8_est7',   # renters severely cost-burdened (>50%)
}


def fetch_zip(url: str, cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    cache_path = cache / Path(url).name
    if cache_path.exists():
        return cache_path.read_bytes()
    req = Request(url, headers={'User-Agent': 'map-bernalillo-chas/1.0'})
    print(f'  downloading {url}')
    with urlopen(req, timeout=60) as r:
        blob = r.read()
    cache_path.write_bytes(blob)
    return blob


def parse_chas(zip_bytes: bytes, cols: dict[str, str]) -> list[dict]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        # Find the tract-level CSV (HUD's filename varies but always contains
        # 'tract' or '140' for the geographic level).
        tract_name = None
        for name in z.namelist():
            low = name.lower()
            if low.endswith('.csv') and ('140' in low or 'tract' in low):
                tract_name = name
                break
        if not tract_name:
            sys.exit('No tract-level CSV found in CHAS ZIP — vintage may differ; pass --inner-name.')
        with z.open(tract_name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8'))
            for r in reader:
                if r.get('st') != BERN_STATE or r.get('cnty') != BERN_COUNTY:
                    continue
                geoid = (r.get('st') or '') + (r.get('cnty') or '') + (r.get('tract') or '')
                try:
                    total = int(r.get(cols['total']) or 0)
                    b30 = int(r.get(cols['burden_30']) or 0)
                    b50 = int(r.get(cols['burden_50']) or 0)
                except (TypeError, ValueError):
                    continue
                rows.append({
                    'geoid': geoid,
                    'total': total,
                    'burden_30': b30,
                    'burden_50': b50,
                    'share_30': round(b30 / total, 4) if total else None,
                    'share_50': round(b50 / total, 4) if total else None,
                })
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--cache', default='data/chas_cache')
    ap.add_argument('--out-layers', default='data/layers.json')
    ap.add_argument('--inner-name', help='Override the inner CSV filename if HUD changes it.')
    args = ap.parse_args()

    blob = fetch_zip(args.url, Path(args.cache))
    rows = parse_chas(blob, DEFAULT_COLS)
    print(f'  parsed {len(rows)} Bernalillo tracts')

    out_path = Path(args.out_layers)
    if out_path.exists():
        layers = json.loads(out_path.read_text())
    else:
        layers = {}
    layers['renter_cost_burden'] = rows
    out_path.write_text(json.dumps(layers, separators=(',', ':')))
    print(f'  wrote {out_path}')


if __name__ == '__main__':
    main()
