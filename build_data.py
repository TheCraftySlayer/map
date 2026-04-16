#!/usr/bin/env python3
"""
build_data.py — Rebuild map JSON from .dbf tax roll files.

Usage (three modes):

  Mode A — roll only (updates neighborhood stats, preserves all point layers):
    python build_data.py --roll taxroll25.dbf taxroll24.dbf taxroll23.dbf ...

  Mode B — roll + coords file (PARID, XCOORD, YCOORD):
    python build_data.py --roll taxroll.dbf --coords geocoding.dbf

  Mode C — enriched file (has coordinates + protest/freeze details):
    python build_data.py --roll taxroll.dbf --enriched enriched.dbf

Reads existing data/core.json and data/layers.json to preserve:
  - Geometry (neighborhood polygons, county boundary, census tracts)
  - Contact center data (calls, visits, phone channel)
  - Visitor point layers (VET_V, VF_V, HOH_V, PRO_V, SP_GEO)
  - Phone channel layers (RPT, CO, VO, MC)
  - Point layers that require enriched data (when using --coords mode)

Rebuilds from .dbf files:
  - Neighborhood property stats (values, exemptions, year-over-year changes)
  - With --enriched: VF_DENIED, VF_INPROC, PRO_20, PRO_21, VF20_*, VF21_*, VETW*, EG, EL
  - With --coords: HOH points, VET points, sale points, exemption gained/lost (EG, EL)

Requirements:
    pip install dbfread pyproj
"""

import json
import argparse
import csv
import re
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

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

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


def read_xlsx(path):
    """Read an .xlsx file and return list of dicts."""
    if not HAS_OPENPYXL:
        sys.exit("Install openpyxl: pip install openpyxl")
    print(f"  Reading {path}...")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    headers = [str(h).strip() if h else '' for h in next(rows)]
    records = []
    for row in rows:
        records.append(dict(zip(headers, row)))
    wb.close()
    print(f"  → {len(records):,} records")
    return records


def read_csv(path):
    """Read a .csv file and return list of dicts."""
    print(f"  Reading {path}...")
    records = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)
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


def process_coords(records):
    """
    Process geocoding .dbf records into a lookup dict by PARID.
    Returns dict: { parid: (XCOORD, YCOORD, EARLIEST_YR, LATEST_YR) }
    """
    lookup = {}
    skipped = 0
    for r in records:
        parid = str(r.get('PARID', '') or '').strip()
        x = safe_float(r.get('XCOORD'))
        y = safe_float(r.get('YCOORD'))
        if parid and x and y:
            earliest = safe_int(r.get('EARLIEST_YR'))
            latest = safe_int(r.get('LATEST_YR'))
            lookup[parid] = (x, y, earliest, latest)
        else:
            skipped += 1
    if skipped:
        print(f"  Skipped {skipped:,} records without PARID/coords")
    return lookup


def join_roll_with_coords(roll_records, coord_lookup):
    """
    Join tax roll records with coordinate lookup on UPC=PARID.
    Returns dict: { year: [records_with_coords] }
    """
    by_yr = defaultdict(list)
    matched = 0
    unmatched = 0
    for r in roll_records:
        upc = str(r.get('UPC', '') or '').strip()
        yr = safe_int(r.get('TAXYR'))
        if not upc or not yr:
            continue
        if upc in coord_lookup:
            x, y, earliest, latest = coord_lookup[upc]
            r['XCOORD'] = x
            r['YCOORD'] = y
            r['EARLIEST_YR'] = earliest
            r['LATEST_YR'] = latest
            r['PARID'] = upc
            by_yr[yr].append(r)
            matched += 1
        else:
            unmatched += 1
    print(f"  Matched: {matched:,}, unmatched: {unmatched:,}")
    return dict(by_yr)


def build_point_layers_from_roll(joined_by_yr):
    """Build point layers from tax roll + coords (no enriched data needed).
    Limits to last 5 years to keep file size manageable."""
    layers = {}

    # Use last 10 years for point layers, plus one extra prior year as
    # baseline so change detection can produce points for the earliest year.
    all_years = sorted(joined_by_yr.keys())
    max_point_years = 10
    recent_years = all_years[-(max_point_years + 1):] if len(all_years) > max_point_years + 1 else all_years
    print(f"  Using years {recent_years[0]}-{recent_years[-1]} for point layers ({len(recent_years)} years)")

    recent_recs = []
    for yr in recent_years:
        recs = joined_by_yr[yr]
        for r in recs:
            r['_yr'] = yr
        recent_recs.extend(recs)

    print(f"  Processing {len(recent_recs):,} recent records...")

    def ll(r):
        # XCOORD=longitude, YCOORD=latitude (already WGS84)
        return round(safe_float(r.get('YCOORD')), 6), round(safe_float(r.get('XCOORD')), 6)

    # ── Sale points: detect NEW sales by comparing consecutive years ──
    by_parid = defaultdict(dict)
    for r in recent_recs:
        parid = str(r.get('PARID', '') or r.get('UPC', '') or '').strip()
        yr = r['_yr']
        if parid:
            by_parid[parid][yr] = r

    sale_pts = []
    for i in range(len(recent_years) - 1):
        y1, y2 = recent_years[i], recent_years[i+1]
        for parid, yrs in by_parid.items():
            if y1 not in yrs or y2 not in yrs:
                continue
            r1, r2 = yrs[y1], yrs[y2]
            sp1 = safe_float(r1.get('SALEPRICE'))
            sp2 = safe_float(r2.get('SALEPRICE'))
            sd1 = str(r1.get('SALEDATE', '') or '').strip()
            sd2 = str(r2.get('SALEDATE', '') or '').strip()
            # New sale if price or date changed
            if sp2 > 0 and (sp2 != sp1 or sd2 != sd1):
                x = safe_float(r2.get('XCOORD'))
                y_coord = safe_float(r2.get('YCOORD'))
                if x and y_coord:
                    la, ln = round(y_coord, 6), round(x, 6)
                    sale_pts.append({
                        'la': la, 'ln': ln,
                        'y': y2,
                        'p': int(sp2),
                        'd': 0
                    })
    layers['SL'] = sale_pts

    # ── Exemption gained/lost (recent year-pairs only) ──
    eg_h, eg_v, el_h, el_v = [], [], [], []
    for i in range(len(recent_years) - 1):
        y1, y2 = recent_years[i], recent_years[i+1]
        for parid, yrs in by_parid.items():
            if y1 not in yrs or y2 not in yrs:
                continue
            r1, r2 = yrs[y1], yrs[y2]
            x = safe_float(r2.get('XCOORD'))
            y_coord = safe_float(r2.get('YCOORD'))
            if not x or not y_coord:
                continue

            hoh1 = safe_float(r1.get('HOHEXEMP')) > 0
            hoh2 = safe_float(r2.get('HOHEXEMP')) > 0
            vet1 = safe_float(r1.get('VETEXEMP')) > 0
            vet2 = safe_float(r2.get('VETEXEMP')) > 0

            la, ln = round(y_coord, 6), round(x, 6)
            if not hoh1 and hoh2:
                eg_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if not vet1 and vet2:
                eg_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if hoh1 and not hoh2:
                el_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if vet1 and not vet2:
                el_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})

    layers['EG_H'] = eg_h
    layers['EG_V'] = eg_v
    layers['EL_H'] = el_h
    layers['EL_V'] = el_v

    return layers


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

    # Helper to get lat/lon (XCOORD=longitude, YCOORD=latitude, already WGS84)
    def ll(r):
        return round(safe_float(r.get('YCOORD')), 6), round(safe_float(r.get('XCOORD')), 6)

    # ── VF_DENIED: Value freeze denied (latest year only) ──
    latest_yr = max(enriched_by_yr.keys())
    latest_recs = enriched_by_yr.get(latest_yr, [])
    vf_denied = []
    vf_inproc = []
    for r in latest_recs:
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
    print(f"  VF denied: {len(vf_denied):,}, in-process: {len(vf_inproc):,} (year {latest_yr})")

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

    eg_h, eg_v, el_h, el_v = [], [], [], []
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

            la, ln = round(y, 6), round(x, 6)  # Already WGS84
            if not hoh1 and hoh2:
                eg_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if not vet1 and vet2:
                eg_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if hoh1 and not hoh2:
                el_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})
            if vet1 and not vet2:
                el_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2})

    layers['EG_H'] = eg_h
    layers['EG_V'] = eg_v
    layers['EL_H'] = el_h
    layers['EL_V'] = el_v
    print(f"  Exemptions gained: HOH {len(eg_h):,}, VET {len(eg_v):,}")
    print(f"  Exemptions lost:   HOH {len(el_h):,}, VET {len(el_v):,}")

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


# Year → color map (must match YRC in index.html)
YRC = {
    2005:'#a6cee3',2006:'#b2df8a',2007:'#fb9a99',2008:'#fdbf6f',2009:'#cab2d6',
    2010:'#ffff99',2011:'#b15928',2012:'#6a3d9a',2013:'#ff7f00',2014:'#33a02c',
    2015:'#e31a1c',2016:'#1f78b4',2017:'#a65628',2018:'#f781bf',2019:'#999999',
    2020:'#66c2a5',2021:'#fc8d62',2022:'#984ea3',2023:'#2166ac',2024:'#f4a11d',
    2025:'#1a9850',2026:'#e41a1c',
}


def update_html_sidebar(final_layers, html_path):
    """Update index.html sidebar counts and year filter checkboxes
    to match the actual generated layer data."""
    html_file = Path(html_path)
    if not html_file.exists():
        print(f"  Skipping HTML update: {html_path} not found")
        return

    raw = html_file.read_bytes()
    crlf = b'\r\n' in raw
    html = raw.decode('utf-8').replace('\r\n', '\n')
    counts = {k: len(v) for k, v in final_layers.items()}

    # ── 1. Year filter checkboxes for tax roll point layers ──
    yr_set = set()
    for k in ['SL', 'EG_H', 'EG_V', 'EL_H', 'EL_V']:
        for p in final_layers.get(k, []):
            if 'y' in p:
                yr_set.add(p['y'])

    if yr_set:
        min_yr, max_yr = min(yr_set), max(yr_set)
        # Update header range
        html = re.sub(
            r'(Tax roll point layers <span[^>]*>)\d+&ndash;\d+( tax rolls)',
            rf'\g<1>{min_yr}&ndash;{max_yr}\2',
            html,
        )
        # Rebuild year checkbox block
        cb_lines = []
        for yr in sorted(yr_set):
            color = YRC.get(yr, '#666')
            short = f"'{yr % 100:02d}"
            cb_lines.append(
                f'    <label style="margin:0"><input type="checkbox" class="yrf" value="{yr}"> '
                f'<span style="color:{color};font-weight:600">{short}</span></label>'
            )
        new_block = '\n'.join(cb_lines)
        html = re.sub(
            r'(  <div style="display:flex;gap:6px;margin:4px 0 6px 2px;font-size:10px;flex-wrap:wrap">\n)'
            r'(?:    <label style="margin:0"><input type="checkbox" class="yrf"[^\n]*\n)+'
            r'(  </div>)',
            rf'\1{new_block}\n\2',
            html,
        )

    # ── 2. VF pipeline counts ──
    vf_denied_n = counts.get('VF_DENIED', 0)
    vf_inproc_n = counts.get('VF_INPROC', 0)
    vf_total = vf_denied_n + vf_inproc_n
    html = re.sub(
        r'(Value freeze pipeline <span[^>]*>)[^<]*(</span>)',
        rf'\g<1>2025 roll &middot; {vf_total:,} geocoded\2',
        html,
    )
    html = re.sub(
        r'(data-layer="vf_denied">.*?Denied )\([\d,]+\)',
        rf'\1({vf_denied_n:,})',
        html,
    )
    html = re.sub(
        r'(data-layer="vf_inproc">.*?In-process )\([\d,]+\)',
        rf'\1({vf_inproc_n:,})',
        html,
    )

    # ── 3. 2020 snapshot counts ──
    for html_key, data_key, label in [
        ('pro_20', 'PRO_20', 'Protests'),
        ('vf_20_g', 'VF_20_G', 'VF granted'),
        ('vf_20_d', 'VF_20_D', 'VF denied'),
        ('vetw', 'VETW', 'Disabled vet waiver'),
    ]:
        n = counts.get(data_key, 0)
        html = re.sub(
            rf'(data-layer="{html_key}">.*?{re.escape(label)} )\([\d,]+\)',
            rf'\1({n:,})',
            html,
        )

    # ── 4. 2021 snapshot counts ──
    for html_key, data_key, label in [
        ('pro_21', 'PRO_21', 'Protests'),
        ('vf21_a', 'VF21_A', 'VF active'),
        ('vf21_d', 'VF21_D', 'VF denied'),
        ('vf21_r', 'VF21_R', 'VF removed'),
        ('vetw_21', 'VETW_21', 'Disabled vet waiver'),
    ]:
        n = counts.get(data_key, 0)
        html = re.sub(
            rf'(data-layer="{html_key}">.*?{re.escape(label)} )\([\d,]+\)',
            rf'\1({n:,})',
            html,
        )

    out = html.encode('utf-8')
    if crlf:
        out = out.replace(b'\n', b'\r\n')
    html_file.write_bytes(out)
    print(f"  Updated {html_path} sidebar counts")


def main():
    parser = argparse.ArgumentParser(
        description='Rebuild map JSON from .dbf files',
        epilog='Use --enriched OR --coords (not both). '
               '--coords mode joins the tax roll with a geocoding file on UPC=PARID.'
    )
    parser.add_argument('--roll', required=True, nargs='+', help='Path(s) to tax roll .dbf file(s)')
    parser.add_argument('--enriched', help='Path to enriched parcel .dbf (has coords + protest/freeze)')
    parser.add_argument('--coords', help='Path to geocoding .dbf (PARID, XCOORD, YCOORD)')
    parser.add_argument('--outdir', default='data', help='Output directory (default: data)')
    args = parser.parse_args()

    # Determine mode
    if args.enriched:
        mode = 'enriched'
    elif args.coords:
        mode = 'coords'
    else:
        mode = 'roll-only'
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
    roll_records = []
    for roll_path in args.roll:
        print(f"\nReading tax roll: {roll_path}")
        roll_records.extend(read_dbf(roll_path))
    print(f"\nTotal tax roll records: {len(roll_records):,}")

    # Process tax roll by neighborhood
    print("\nProcessing tax roll by neighborhood...")
    by_nbhd_yr = process_roll(roll_records)
    print(f"  {len(by_nbhd_yr)} neighborhoods found")

    # Compute neighborhood stats from roll
    print("\nComputing neighborhood stats...")
    nbhd_stats = compute_nbhd_stats(by_nbhd_yr, existing_core['DATA']['features'])

    # All possible layer keys
    all_layer_keys = [
        'SL', 'RPT', 'CO', 'VO', 'MC',
        'VET_V', 'VF_V', 'HOH_V', 'PRO_V', 'SP_GEO',
        'VF_DENIED', 'VF_INPROC', 'PRO_20', 'PRO_21',
        'VF20_A', 'VF20_D', 'VF20_R', 'VF_20_G', 'VF_20_D',
        'VF21_A', 'VF21_D', 'VF21_R', 'VETW', 'VETW_21',
        'EG_H', 'EG_V', 'EL_H', 'EL_V',
    ]

    if mode == 'enriched':
        # ── Enriched mode: full rebuild ──
        print("\nReading enriched data...")
        ext = args.enriched.lower()
        if ext.endswith('.csv'):
            enriched_records = read_csv(args.enriched)
        elif ext.endswith('.xlsx'):
            enriched_records = read_xlsx(args.enriched)
        else:
            enriched_records = read_dbf(args.enriched)

        print("\nProcessing enriched data by year...")
        enriched_by_yr = process_enriched(enriched_records)

        print("Updating stats from enriched data...")
        update_nbhd_stats_from_enriched(nbhd_stats, enriched_by_yr)

        print("\nBuilding point layers from enriched data...")
        new_layers = build_point_layers(enriched_by_yr)

        # Also build SL (sale) layer from roll + enriched coords
        print("\nBuilding coordinate lookup from enriched data...")
        coord_lookup = {}
        for r in enriched_records:
            parid = str(r.get('PARID', '') or '').strip()
            x = safe_float(r.get('XCOORD'))
            y = safe_float(r.get('YCOORD'))
            if parid and x and y:
                coord_lookup[parid] = (x, y, 0, 0)
        print(f"  {len(coord_lookup):,} parcels with coordinates")

        print("\nJoining tax roll with coordinates for sale detection...")
        joined_by_yr = join_roll_with_coords(roll_records, coord_lookup)
        for yr in sorted(joined_by_yr.keys()):
            print(f"  {yr}: {len(joined_by_yr[yr]):,} records")

        print("\nBuilding sale & exemption layers from joined roll data...")
        roll_layers = build_point_layers_from_roll(joined_by_yr)
        new_layers['SL'] = roll_layers.get('SL', [])
        # Use roll-based exemption gain/loss (wider year range than enriched)
        for k in ['EG_H', 'EG_V', 'EL_H', 'EL_V']:
            new_layers[k] = roll_layers.get(k, [])

        rebuilt_keys = [
            'SL', 'VF_DENIED', 'VF_INPROC', 'PRO_20', 'PRO_21',
            'VF20_A', 'VF20_D', 'VF20_R', 'VF_20_G', 'VF_20_D',
            'VF21_A', 'VF21_D', 'VF21_R',
            'VETW', 'VETW_21', 'EG_H', 'EG_V', 'EL_H', 'EL_V'
        ]
        preserved_keys = [k for k in all_layer_keys if k not in rebuilt_keys]

    elif mode == 'coords':
        # ── Coords mode: join roll + geocoding, rebuild what we can ──
        print(f"\nReading coords file...")
        ext = args.coords.lower()
        if ext.endswith('.xlsx'):
            coord_records = read_xlsx(args.coords)
        elif ext.endswith('.csv'):
            coord_records = read_csv(args.coords)
        else:
            coord_records = read_dbf(args.coords)

        print("\nBuilding coordinate lookup...")
        coord_lookup = process_coords(coord_records)
        print(f"  {len(coord_lookup):,} parcels with coordinates")

        print("\nJoining tax roll with coordinates...")
        joined_by_yr = join_roll_with_coords(roll_records, coord_lookup)
        for yr in sorted(joined_by_yr.keys()):
            print(f"  {yr}: {len(joined_by_yr[yr]):,} records")

        print("\nBuilding point layers from joined data...")
        new_layers = build_point_layers_from_roll(joined_by_yr)

        rebuilt_keys = ['SL', 'EG_H', 'EG_V', 'EL_H', 'EL_V']
        preserved_keys = [k for k in all_layer_keys if k not in rebuilt_keys]

    else:
        # ── Roll-only mode: just update neighborhood stats, preserve all layers ──
        print("\nRoll-only mode: updating neighborhood stats, preserving all point layers.")
        new_layers = {}
        rebuilt_keys = []
        preserved_keys = all_layer_keys

    # ── Assemble core.json ──
    print("\nAssembling core.json...")
    for feat in existing_core['DATA']['features']:
        nbhd = int(feat['properties'].get('nbhd', 0))
        if nbhd in nbhd_stats:
            feat['properties'] = nbhd_stats[nbhd]

    existing_core['NBHD_CENTERS'] = build_nbhd_centers(existing_core['DATA'])

    # ── Assemble layers.json ──
    print("Assembling layers.json...")
    final_layers = {}
    for k in preserved_keys:
        if k in existing_layers:
            final_layers[k] = existing_layers[k]
    for k in rebuilt_keys:
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

    # ── Update HTML sidebar counts ──
    html_path = outdir.parent / 'index.html'
    print(f"\nUpdating {html_path}...")
    update_html_sidebar(final_layers, html_path)

    # Summary
    print("\n── Summary ──")
    print(f"Mode:           {mode}")
    print(f"Neighborhoods:  {len(nbhd_stats)}")
    print(f"core.json:      {core_size/1024/1024:.1f} MB")
    print(f"layers.json:    {layers_size/1024/1024:.1f} MB")
    print()
    for k in sorted(rebuilt_keys):
        print(f"  {k}: {len(final_layers.get(k, [])):,} points (rebuilt)")
    print()
    for k in sorted(preserved_keys):
        count = len(final_layers.get(k, []))
        print(f"  {k}: {count:,} points (preserved)")

    # Show year coverage for tax roll point layers
    yr_set = set()
    for k in ['SL', 'EG_H', 'EG_V', 'EL_H', 'EL_V']:
        for p in final_layers.get(k, []):
            if 'y' in p:
                yr_set.add(p['y'])
    if yr_set:
        print(f"\n  Tax roll point years: {sorted(yr_set)}")
        print(f"  Note: include the prior year's roll file to get points for the earliest year.")

    print("\nDone! Commit and push to update the site.")


if __name__ == '__main__':
    main()
