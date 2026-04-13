#!/usr/bin/env python3
"""
build_data.py — Rebuild map JSON from .dbf tax roll files.

Usage:
    python build_data.py --roll taxroll.dbf --enriched enriched.dbf

Reads existing data/core.json and data/layers.json to preserve:
  - Geometry (neighborhood polygons, county boundary, census tracts)
  - Contact center data (calls, visits, phone channel)
  - Visitor point layers (VET_V, VF_V, HOH_V, PRO_V, SP_GEO)
  - Phone channel layers (RPT, CO, VO, MC)

Rebuilds from .dbf files:
  - Neighborhood property stats (values, exemptions, year-over-year changes)
  - Point layers: VF_DENIED, VF_INPROC, PRO_20, PRO_21, VF20_*, VF21_*, VETW*
  - Exemption gained/lost layers (EG, EL)

Requirements:
    pip install dbfread pyproj
"""

import json
import argparse
import sys
import statistics
from collections import defaultdict
from pathlib import Path

try:
    from dbfread import DBF
except ImportError:
    sys.exit("Install dbfread: pip install dbfread")

try:
    from pyproj import Transformer
except ImportError:
    sys.exit("Install pyproj: pip install pyproj")

# NM State Plane Central (feet) → WGS84
TRANSFORMER = Transformer.from_crs("EPSG:2903", "EPSG:4326", always_xy=True)

def to_latlon(x, y):
    """Convert State Plane NM Central coords to lat/lon."""
    lon, lat = TRANSFORMER.transform(x, y)
    return round(lat, 6), round(lon, 6)


def read_dbf(path):
    """Read a .dbf file and return list of dicts."""
    print(f"  Reading {path}...")
    records = list(DBF(path, encoding='latin-1'))
    print(f"  → {len(records):,} records")
    return records


def safe_float(v, default=0):
    """Safely convert to float."""
    try:
        if v is None:
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(v, default=0):
    """Safely convert to int."""
    try:
        if v is None:
            return default
        return int(v)
    except (ValueError, TypeError):
        return default


def median_safe(vals):
    """Median of a list, or 0 if empty."""
    return statistics.median(vals) if vals else 0


def process_roll(records):
    """
    Process tax roll .dbf records grouped by NBHD and TAXYR.
    Returns dict: { nbhd: { year: [records] } }
    """
    by_nbhd_yr = defaultdict(lambda: defaultdict(list))
    for r in records:
        nbhd = safe_int(r.get('NBHD'))
        yr = safe_int(r.get('TAXYR'))
        if nbhd and yr:
            by_nbhd_yr[nbhd][yr].append(r)
    return by_nbhd_yr


def process_enriched(records):
    """
    Process enriched .dbf records grouped by TAXYR.
    Returns dict: { year: [records_with_coords] }
    """
    by_yr = defaultdict(list)
    skipped = 0
    for r in records:
        yr = safe_int(r.get('TAXYR'))
        x = safe_float(r.get('XCOORD'))
        y = safe_float(r.get('YCOORD'))
        if yr and x and y:
            by_yr[yr].append(r)
        else:
            skipped += 1
    if skipped:
        print(f"  Skipped {skipped:,} records without coords/year")
    return dict(by_yr)


def compute_nbhd_stats(by_nbhd_yr, existing_props):
    """
    Compute per-neighborhood aggregated stats from tax roll data.
    Merges with existing properties to preserve contact center data.
    """
    # Find the latest year and all years available
    all_years = set()
    for nbhd, yrs in by_nbhd_yr.items():
        all_years.update(yrs.keys())
    all_years = sorted(all_years)
    latest_yr = max(all_years) if all_years else 2026
    print(f"  Tax years found: {all_years}")
    print(f"  Latest year: {latest_yr}")

    # Build lookup of existing properties by nbhd
    existing = {}
    for feat in existing_props:
        n = feat['properties'].get('nbhd')
        if n is not None:
            existing[int(n)] = feat['properties']

    updated = {}
    for nbhd, yr_data in by_nbhd_yr.items():
        # Use latest year data for current stats
        recs = yr_data.get(latest_yr, [])
        if not recs:
            # Fall back to most recent year available
            avail = sorted(yr_data.keys())
            recs = yr_data[avail[-1]] if avail else []
        if not recs:
            continue

        parcels = len(recs)
        tot_values = [safe_float(r.get('TOTVALUE')) for r in recs]
        yr_builts = [safe_int(r.get('YRBUILT')) for r in recs if safe_int(r.get('YRBUILT')) > 0]

        # Exemption counts
        hoh_count = sum(1 for r in recs if safe_float(r.get('HOHEXEMP')) > 0)
        vet_count = sum(1 for r in recs if safe_float(r.get('VETEXEMP')) > 0)
        vf_count = sum(1 for r in recs if str(r.get('EXEMCODE', '')).strip().upper() in ('VF', 'F', 'FREEZE'))

        # Sales
        sale_recs = [r for r in recs if safe_float(r.get('SALEPRICE')) > 0]
        recent_sales = len(sale_recs)

        # New construction
        new_const = sum(1 for r in recs if safe_float(r.get('NEWCONST')) > 0)

        # Year-over-year value changes
        yr_pairs = [(all_years[i], all_years[i+1]) for i in range(len(all_years)-1)]
        yoy_changes = {}
        for y1, y2 in yr_pairs:
            recs1 = yr_data.get(y1, [])
            recs2 = yr_data.get(y2, [])
            if recs1 and recs2:
                med1 = median_safe([safe_float(r.get('TOTVALUE')) for r in recs1 if safe_float(r.get('TOTVALUE')) > 0])
                med2 = median_safe([safe_float(r.get('TOTVALUE')) for r in recs2 if safe_float(r.get('TOTVALUE')) > 0])
                if med1 > 0:
                    yoy_changes[f"chg_{y1%100}_{y2%100}"] = round((med2 - med1) / med1, 4)

        # Appraisal volatility (std dev of YoY changes)
        chg_vals = list(yoy_changes.values())
        appr_vol = round(statistics.stdev(chg_vals), 4) if len(chg_vals) >= 2 else None

        # Cumulative volatility
        volatility = round(sum(abs(c) for c in chg_vals), 4) if chg_vals else None

        # Owner turnover (parcels with different recent sale)
        owner_chg = sum(1 for r in recs if str(r.get('LSALEDATE', '') or '').strip())
        owner_turnover = round(owner_chg / parcels, 4) if parcels else 0

        # HOH churn - need multi-year data
        hoh_churn = None
        if len(all_years) >= 2:
            prev_yr = all_years[-2]
            prev_recs = yr_data.get(prev_yr, [])
            if prev_recs:
                prev_hoh = sum(1 for r in prev_recs if safe_float(r.get('HOHEXEMP')) > 0)
                hoh_churn = round(abs(hoh_count - prev_hoh) / max(parcels, 1), 4)

        # Exemption rates for earliest available year (for drift calculation)
        hoh_20 = None
        vet_20 = None
        pct_hoh_20 = None
        pct_vet_20 = None
        if len(all_years) >= 2:
            earliest = all_years[0]
            early_recs = yr_data.get(earliest, [])
            if early_recs:
                ep = len(early_recs)
                eh = sum(1 for r in early_recs if safe_float(r.get('HOHEXEMP')) > 0)
                ev = sum(1 for r in early_recs if safe_float(r.get('VETEXEMP')) > 0)
                hoh_20 = round(eh / ep, 4) if ep else None
                vet_20 = round(ev / ep, 4) if ep else None
                pct_hoh_20 = hoh_20
                pct_vet_20 = vet_20

        # Exemption drift
        exemp_drift = None
        if hoh_20 is not None and vet_20 is not None and parcels:
            cur_hoh = round(hoh_count / parcels, 4)
            cur_vet = round(vet_count / parcels, 4)
            exemp_drift = round(abs(cur_hoh - hoh_20) + abs(cur_vet - vet_20), 4)

        # Value freeze denial rate
        pct_vf_denied = None  # Need enriched data for this

        # Start with existing props (preserves contact center data)
        props = dict(existing.get(nbhd, {}))

        # Update with freshly computed stats
        props.update({
            'nbhd': float(nbhd),
            'parcels': float(parcels),
            'avg_appraised': int(statistics.mean(tot_values)) if tot_values else 0,
            'median_val_25': int(median_safe([v for v in tot_values if v > 0])),
            'median_yrbuilt': int(median_safe(yr_builts)) if yr_builts else 0,
            'pct_hoh': round(hoh_count / parcels, 4) if parcels else 0,
            'pct_vet': round(vet_count / parcels, 4) if parcels else 0,
            'pct_val_freeze': round(vf_count / parcels, 4) if parcels else 0,
            'vf_count': float(vf_count),
            'owner_turnover': owner_turnover,
            'pct_recent_sale': round(recent_sales / parcels, 4) if parcels else 0,
            'recent_sales': float(recent_sales),
            'appr_volatility': appr_vol,
            'volatility': volatility,
            'hoh_churn': hoh_churn,
            'hoh_20': hoh_20,
            'vet_20': vet_20,
            'pct_hoh_20': pct_hoh_20,
            'pct_vet_20': pct_vet_20,
            'exemp_drift': exemp_drift,
        })
        # Add year-over-year changes
        for key, val in yoy_changes.items():
            props[key] = val

        # Overall value change
        if chg_vals:
            cumulative = 1.0
            for c in chg_vals:
                cumulative *= (1 + c)
            props['val_change_pct'] = round(cumulative - 1, 4)

        updated[nbhd] = props

    return updated


def build_point_layers(enriched_by_yr):
    """Build point layers from enriched .dbf data."""
    layers = {}

    # Collect all records across all years
    all_recs = []
    for yr, recs in enriched_by_yr.items():
        for r in recs:
            r['_yr'] = yr
        all_recs.extend(recs)

    print(f"  Processing {len(all_recs):,} enriched records...")

    # Helper to get lat/lon
    def ll(r):
        return to_latlon(safe_float(r.get('XCOORD')), safe_float(r.get('YCOORD')))

    # ── VF_DENIED: Value freeze denied (current roll) ──
    vf_denied = []
    vf_inproc = []
    for r in all_recs:
        status = str(r.get('VAL_FREEZE_STATUS', '') or '').strip().lower()
        if status == 'denied':
            la, ln = ll(r)
            vf_denied.append({
                'la': la, 'ln': ln,
                'v': safe_int(r.get('APRTOTAL')),
                'yr': str(r.get('VAL_FREEZE_YEAR', '') or '').strip() or None
            })
        elif status in ('in process', 'in-process', 'inprocess', 'pending'):
            la, ln = ll(r)
            vf_inproc.append({'la': la, 'ln': ln, 'v': safe_int(r.get('APRTOTAL'))})
    layers['VF_DENIED'] = vf_denied
    layers['VF_INPROC'] = vf_inproc

    # ── Protest layers by year ──
    for target_yr, key in [(2020, 'PRO_20'), (2021, 'PRO_21')]:
        recs = enriched_by_yr.get(target_yr, [])
        pts = []
        for r in recs:
            if not r.get('PROTESTED'):
                continue
            protested = str(r.get('PROTESTED', '') or '').strip()
            if protested.upper() not in ('Y', 'YES', '1', 'TRUE'):
                continue
            la, ln = ll(r)
            pts.append({
                'la': la, 'ln': ln,
                'ht': str(r.get('HEARING TYPE', '') or '').strip()[:1] or '',
                'st': str(r.get('HEARING STATUS', '') or '').strip()[:1] or '',
                'ra': str(r.get('RESULT ACTION', '') or '').strip() or '',
                'nv': safe_int(r.get('NOTICE VALUE')),
                'tv': safe_int(r.get('TAXPAYER VALUE')),
            })
        layers[key] = pts

    # ── Value freeze layers by year ──
    for target_yr in [2020, 2021]:
        recs = enriched_by_yr.get(target_yr, [])
        active, denied, removed = [], [], []
        for r in recs:
            status = str(r.get('VAL_FREEZE_STATUS', '') or '').strip().lower()
            if not status:
                continue
            la, ln = ll(r)
            v = safe_int(r.get('APRTOTAL'))
            vf_yr = str(r.get('VAL_FREEZE_YEAR', '') or '').strip() or None
            if status == 'active':
                if target_yr == 2020:
                    active.append({'la': la, 'ln': ln, 's': 'Active', 'v': v})
                else:
                    active.append({'la': la, 'ln': ln, 'v': v, 'yr': vf_yr})
            elif status == 'denied':
                if target_yr == 2020:
                    denied.append({'la': la, 'ln': ln, 's': 'Denied', 'v': v})
                else:
                    denied.append({'la': la, 'ln': ln, 'v': v, 'yr': vf_yr})
            elif status == 'removed':
                if target_yr == 2020:
                    removed.append({'la': la, 'ln': ln, 's': 'Removed', 'v': v})
                else:
                    removed.append({'la': la, 'ln': ln, 'v': v, 'yr': vf_yr})

        if target_yr == 2020:
            layers['VF20_A'] = active
            layers['VF20_D'] = denied
            layers['VF20_R'] = removed
            layers['VF_20_G'] = [{'la': p['la'], 'ln': p['ln'], 'v': p['v'], 'yr': vf_yr} for p in active]
            layers['VF_20_D'] = [{'la': p['la'], 'ln': p['ln'], 'v': p['v'], 'yr': vf_yr} for p in denied]
        else:
            layers['VF21_A'] = active
            layers['VF21_D'] = denied
            layers['VF21_R'] = removed

    # ── Disabled veteran waiver ──
    for target_yr, key in [(2020, 'VETW'), (2021, 'VETW_21')]:
        recs = enriched_by_yr.get(target_yr, [])
        pts = []
        for r in recs:
            waiver = str(r.get('DISABLED VETERAN TAX WAIVER', '') or '').strip()
            if waiver.upper() in ('Y', 'YES', '1', 'TRUE'):
                la, ln = ll(r)
                pts.append({'la': la, 'ln': ln, 'v': safe_int(r.get('APRTOTAL'))})
        layers[key] = pts

    # ── Exemption gained/lost (multi-year comparison) ──
    # Group enriched records by parcel ID across years
    by_parid = defaultdict(dict)
    for r in all_recs:
        parid = r.get('PARID', '')
        yr = r['_yr']
        if parid:
            by_parid[parid][yr] = r

    eg_list, el_list = [], []
    comparison_years = sorted(enriched_by_yr.keys())
    for i in range(len(comparison_years) - 1):
        y1, y2 = comparison_years[i], comparison_years[i+1]
        for parid, yrs in by_parid.items():
            if y1 not in yrs or y2 not in yrs:
                continue
            r1, r2 = yrs[y1], yrs[y2]
            x = safe_float(r2.get('XCOORD'))
            y = safe_float(r2.get('YCOORD'))
            if not x or not y:
                continue

            hoh1 = str(r1.get('HEAD OF HOUSEHOLD', '') or '').strip().upper() in ('Y', 'YES', '1', 'TRUE')
            hoh2 = str(r2.get('HEAD OF HOUSEHOLD', '') or '').strip().upper() in ('Y', 'YES', '1', 'TRUE')
            vet1 = str(r1.get('VETERANS EXEMPTION', '') or '').strip().upper() in ('Y', 'YES', '1', 'TRUE')
            vet2 = str(r2.get('VETERANS EXEMPTION', '') or '').strip().upper() in ('Y', 'YES', '1', 'TRUE')

            gained_h = (not hoh1 and hoh2)
            gained_v = (not vet1 and vet2)
            lost_h = (hoh1 and not hoh2)
            lost_v = (vet1 and not vet2)

            la, ln = to_latlon(x, y)
            if gained_h or gained_v:
                eg_list.append({
                    'la': la, 'ln': ln,
                    'h': 1 if gained_h else 0,
                    'v': 1 if gained_v else 0,
                    'c': 1, 'y': y2
                })
            if lost_h or lost_v:
                el_list.append({
                    'la': la, 'ln': ln,
                    'h': 1 if lost_h else 0,
                    'v': 1 if lost_v else 0,
                    'c': 1, 'y': y2
                })

    layers['EG'] = eg_list
    layers['EL'] = el_list

    return layers


def update_nbhd_stats_from_enriched(nbhd_stats, enriched_by_yr):
    """Update neighborhood stats with data only available in enriched .dbf."""
    latest_yr = max(enriched_by_yr.keys()) if enriched_by_yr else None
    if not latest_yr:
        return

    # Group enriched records by NBHD
    by_nbhd = defaultdict(list)
    for r in enriched_by_yr.get(latest_yr, []):
        nbhd = safe_int(r.get('NBHD'))
        if nbhd:
            by_nbhd[nbhd].append(r)

    for nbhd, recs in by_nbhd.items():
        if nbhd not in nbhd_stats:
            continue
        props = nbhd_stats[nbhd]

        # Is commercial
        nbhd_type = str(recs[0].get('NBHD_TYPE', '') or '').strip().lower()
        props['is_commercial'] = 1 if nbhd_type in ('commercial', 'comm', 'c') else 0

        # VF denial rate from enriched data
        vf_recs = [r for r in recs if str(r.get('VAL_FREEZE_STATUS', '') or '').strip()]
        if vf_recs:
            denied = sum(1 for r in vf_recs if str(r.get('VAL_FREEZE_STATUS', '')).strip().lower() == 'denied')
            props['pct_vf_denied'] = round(denied / len(vf_recs), 4)


def build_nbhd_centers(core_data):
    """Compute neighborhood centers from polygon geometry."""
    centers = {}
    for feat in core_data['features']:
        nbhd = feat['properties'].get('nbhd')
        if nbhd is None:
            continue
        nbhd_key = str(int(nbhd))
        geom = feat['geometry']
        coords = []
        if geom['type'] == 'Polygon':
            coords = geom['coordinates'][0]
        elif geom['type'] == 'MultiPolygon':
            for poly in geom['coordinates']:
                coords.extend(poly[0])
        if coords:
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            centers[nbhd_key] = [
                sum(lats) / len(lats),
                sum(lons) / len(lons)
            ]
    return centers


def main():
    parser = argparse.ArgumentParser(description='Rebuild map JSON from .dbf files')
    parser.add_argument('--roll', required=True, help='Path to tax roll .dbf file')
    parser.add_argument('--enriched', required=True, help='Path to enriched parcel .dbf file')
    parser.add_argument('--outdir', default='data', help='Output directory (default: data)')
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    # Load existing JSON for geometry and preserved data
    core_path = outdir / 'core.json'
    layers_path = outdir / 'layers.json'

    if core_path.exists():
        print("Loading existing core.json...")
        with open(core_path) as f:
            existing_core = json.load(f)
    else:
        sys.exit(f"Error: {core_path} not found. Need existing file for geometry.")

    if layers_path.exists():
        print("Loading existing layers.json...")
        with open(layers_path) as f:
            existing_layers = json.load(f)
    else:
        existing_layers = {}

    # Read .dbf files
    print("\nReading tax roll...")
    roll_records = read_dbf(args.roll)

    print("\nReading enriched data...")
    enriched_records = read_dbf(args.enriched)

    # Process
    print("\nProcessing tax roll by neighborhood...")
    by_nbhd_yr = process_roll(roll_records)
    print(f"  {len(by_nbhd_yr)} neighborhoods found")

    print("\nProcessing enriched data by year...")
    enriched_by_yr = process_enriched(enriched_records)

    print("\nComputing neighborhood stats...")
    nbhd_stats = compute_nbhd_stats(by_nbhd_yr, existing_core['DATA']['features'])

    print("Updating stats from enriched data...")
    update_nbhd_stats_from_enriched(nbhd_stats, enriched_by_yr)

    print("\nBuilding point layers...")
    new_layers = build_point_layers(enriched_by_yr)

    # ── Assemble core.json ──
    print("\nAssembling core.json...")
    # Update DATA features with new stats, preserving geometry
    for feat in existing_core['DATA']['features']:
        nbhd = int(feat['properties'].get('nbhd', 0))
        if nbhd in nbhd_stats:
            feat['properties'] = nbhd_stats[nbhd]

    # Recompute centers
    existing_core['NBHD_CENTERS'] = build_nbhd_centers(existing_core['DATA'])

    # ── Assemble layers.json ──
    print("Assembling layers.json...")
    # Layers rebuilt from .dbf
    dbf_layer_keys = [
        'VF_DENIED', 'VF_INPROC', 'PRO_20', 'PRO_21',
        'VF20_A', 'VF20_D', 'VF20_R', 'VF_20_G', 'VF_20_D',
        'VF21_A', 'VF21_D', 'VF21_R',
        'VETW', 'VETW_21', 'EG', 'EL'
    ]
    # Layers preserved from existing (contact center, visitor, phone)
    preserved_keys = ['SL', 'TL', 'RPT', 'CO', 'VO', 'MC',
                      'VET_V', 'VF_V', 'HOH_V', 'PRO_V', 'SP_GEO']

    final_layers = {}
    for k in preserved_keys:
        if k in existing_layers:
            final_layers[k] = existing_layers[k]
    for k in dbf_layer_keys:
        final_layers[k] = new_layers.get(k, [])

    # ── Write output ──
    print(f"\nWriting {core_path}...")
    with open(core_path, 'w') as f:
        json.dump(existing_core, f, separators=(',', ':'))
    core_size = core_path.stat().st_size
    print(f"  → {core_size:,} bytes")

    print(f"Writing {layers_path}...")
    with open(layers_path, 'w') as f:
        json.dump(final_layers, f, separators=(',', ':'))
    layers_size = layers_path.stat().st_size
    print(f"  → {layers_size:,} bytes")

    # Summary
    print("\n── Summary ──")
    print(f"Neighborhoods:  {len(nbhd_stats)}")
    print(f"core.json:      {core_size/1024/1024:.1f} MB")
    print(f"layers.json:    {layers_size/1024/1024:.1f} MB")
    print()
    for k in sorted(dbf_layer_keys):
        print(f"  {k}: {len(final_layers.get(k, [])):,} points")
    print()
    for k in sorted(preserved_keys):
        print(f"  {k}: {len(final_layers.get(k, [])):,} points (preserved)")
    print("\nDone! Commit and push to update the site.")


if __name__ == '__main__':
    main()
