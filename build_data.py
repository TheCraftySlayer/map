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
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

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


def read_xlsx(path, sheet_name=None):
    """Read an .xlsx file and return list of dicts. Optionally specify sheet."""
    if not HAS_OPENPYXL:
        sys.exit("Install openpyxl: pip install openpyxl")
    print(f"  Reading {path}" + (f" [{sheet_name}]" if sheet_name else "") + "...")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb.active
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


def extract_year(v):
    """Pull a 4-digit year out of a date-ish value, or None if nothing plausible.

    Handles datetime.date/datetime, YYYY-MM-DD / MM-DD-YYYY / MM/DD/YYYY /
    YYYYMMDD strings, bare YYYY ints, and similar tax-roll shapes. Only
    returns years in [1900, 2100] so garbage doesn't slip through.
    """
    if v is None:
        return None
    y = getattr(v, 'year', None)
    if y is not None:
        return y if 1900 <= y <= 2100 else None
    if isinstance(v, (int, float)):
        iv = int(v)
        if 1900 <= iv <= 2100:
            return iv
        # Packed YYYYMMDD as an integer
        if 19000101 <= iv <= 21001231:
            return iv // 10000
        return None
    s = str(v).strip()
    if not s:
        return None
    for tok in re.split(r'[^0-9]', s):
        if len(tok) == 4 and tok.isdigit():
            iv = int(tok)
            if 1900 <= iv <= 2100:
                return iv
    if len(s) >= 8 and s[:8].isdigit():
        iv = int(s[:4])
        if 1900 <= iv <= 2100:
            return iv
    return None


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
    """Build point layers from tax roll + coords (no enriched data needed)."""
    layers = {}

    # Use all available years for point layers.
    all_years = sorted(joined_by_yr.keys())
    recent_years = all_years
    if not recent_years:
        # No tax roll records matched the coord lookup — bail out cleanly
        # instead of crashing on recent_years[0] / [-1].
        print("  No matched years — skipping point-layer build (empty joined_by_yr)")
        return layers
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
                        'd': 0,
                        'n': safe_int(r2.get('NBHD'))
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
            nb = safe_int(r2.get('NBHD'))
            if not hoh1 and hoh2:
                eg_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': nb})
            if not vet1 and vet2:
                eg_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': nb})
            if hoh1 and not hoh2:
                el_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': nb})
            if vet1 and not vet2:
                el_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': nb})

    layers['EG_H'] = eg_h
    layers['EG_V'] = eg_v
    layers['EL_H'] = el_h
    layers['EL_V'] = el_v

    return layers


# Community center coordinates used for nearest-CC tagging and drive-time
# queries. Kept at module level so compute_nbhd_stats and the OSRM query
# in main() reference the same source of truth.
CC_LOCATIONS = [
    ('Vista Grande', 35.1769943, -106.3409576),
    ('Los Vecinos', 35.0788198, -106.3923734),
    ('Paradise Hills', 35.1950907, -106.7129307),
    ('Raymond G. Sanchez', 35.193073, -106.6157715),
    ('Westside', 35.0537822, -106.672675),
    ('Los Padillas', 34.9569792, -106.696385),
    ('Kiki Saavedra', 35.0158333, -106.6577778),
    ('South Valley Senior', 35.069824, -106.6881653),
    ('Alamosa', 35.0714679, -106.7101371),
]


def _point_in_ring(px, py, ring):
    """Ray-casting point-in-polygon test for a single ring (pure Python)."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > py) != (yj > py):
            # Avoid div-by-zero with a tiny epsilon on horizontal edges
            xint = (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi
            if px < xint:
                inside = not inside
        j = i
    return inside


def _point_in_geom(px, py, geom):
    """Test whether (px, py) falls inside a GeoJSON Polygon/MultiPolygon."""
    if not geom:
        return False
    t = geom.get('type')
    if t == 'Polygon':
        rings = geom.get('coordinates') or []
        if not rings:
            return False
        if not _point_in_ring(px, py, rings[0]):
            return False
        for hole in rings[1:]:
            if _point_in_ring(px, py, hole):
                return False
        return True
    if t == 'MultiPolygon':
        for poly in (geom.get('coordinates') or []):
            if not poly:
                continue
            if not _point_in_ring(px, py, poly[0]):
                continue
            hit = True
            for hole in poly[1:]:
                if _point_in_ring(px, py, hole):
                    hit = False
                    break
            if hit:
                return True
    return False


def _find_tract_for_point(lat, lon, tract_geo):
    """Return the tract feature whose geometry contains (lon, lat), else None.

    Built as a linear scan — fine for Bernalillo's 176 tracts × 600 nbhds
    (~100k PIP checks, runs in <1s on commodity hardware).
    """
    if not tract_geo or not tract_geo.get('features'):
        return None
    for tract in tract_geo['features']:
        if _point_in_geom(lon, lat, tract.get('geometry')):
            return tract
    return None


def _ols_fit(pairs):
    """Single-predictor OLS: returns {slope, intercept, n} or None if n<8."""
    n = sx = sy = sxx = sxy = 0
    for x, y in pairs:
        if x is None or y is None:
            continue
        try:
            xf, yf = float(x), float(y)
        except (TypeError, ValueError):
            continue
        n += 1; sx += xf; sy += yf; sxx += xf * xf; sxy += xf * yf
    if n < 8:
        return None
    mx, my = sx / n, sy / n
    vx = sxx - n * mx * mx
    if vx <= 0:
        return {'slope': 0.0, 'intercept': my, 'n': n}
    slope = (sxy - n * mx * my) / vx
    return {'slope': slope, 'intercept': my - slope * mx, 'n': n}


def _mean_of(field, nbhd_stats):
    """Mean of a single field across all neighborhoods. None if no data."""
    n = 0; s = 0.0
    for p in nbhd_stats.values():
        v = p.get(field)
        if v is None:
            continue
        try:
            s += float(v); n += 1
        except (TypeError, ValueError):
            continue
    return s / n if n else None


def _compute_exemption_gaps(nbhd_stats):
    """Populate hoh_gap / vet_gap / vf_gap per neighborhood — both for the
    latest year (bare field, e.g. 'hoh_gap') AND for every per-year field
    the roll produced (e.g. 'hoh_gap_13' .. 'hoh_gap_25') so the UI's
    tax-year selector can switch these layers just like the base rates.

    Each field: residual = actual − predicted, where predicted is either
    a single-predictor OLS against zip_poverty_rate or the county mean
    as a fallback when the regression has no data.
    """
    # Discover which per-year suffixes are available on at least one
    # neighborhood — we'll loop over those plus a "" (bare/latest) pass.
    year_suffixes = set()
    import re as _re
    year_pat = _re.compile(r'^pct_hoh_(\d+)$')
    for p in nbhd_stats.values():
        for k in p.keys():
            m = year_pat.match(k)
            if m:
                year_suffixes.add(m.group(1))

    # Process one base+suffix at a time. Empty suffix means the
    # "current/latest" field the rest of the code already expects.
    def _run_pair(base, predictor_field, gap_base, suffix):
        suf = f'_{suffix}' if suffix else ''
        field = f'{base}{suf}'
        predictor = predictor_field  # zip_poverty_rate is year-invariant
        pairs = [(p.get(predictor), p.get(field)) for p in nbhd_stats.values()]
        fit = _ols_fit(pairs)
        mean = _mean_of(field, nbhd_stats)
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None:
                continue
            if fit and p.get(predictor) is not None:
                try:
                    pred = fit['intercept'] + fit['slope'] * float(p[predictor])
                except (TypeError, ValueError):
                    pred = mean
            else:
                pred = mean
            if pred is None:
                continue
            p[f'{gap_base}{suf}'] = round(v - pred, 4)

    def _run_mean_baseline(base, gap_base, suffix):
        """Vet gaps use a plain county-mean baseline (no ZIP predictor) —
        vet claims track Kirtland / VA proximity more than ZIP income."""
        suf = f'_{suffix}' if suffix else ''
        field = f'{base}{suf}'
        mean = _mean_of(field, nbhd_stats)
        if mean is None:
            return
        for p in nbhd_stats.values():
            v = p.get(field)
            if v is None:
                continue
            p[f'{gap_base}{suf}'] = round(v - mean, 4)

    # Latest-year (bare) pass
    _run_pair('pct_hoh', 'zip_poverty_rate', 'hoh_gap', '')
    _run_pair('pct_val_freeze', 'zip_poverty_rate', 'vf_gap', '')
    _run_mean_baseline('pct_vet', 'vet_gap', '')

    # Per-year passes
    for ys in sorted(year_suffixes):
        _run_pair('pct_hoh', 'zip_poverty_rate', 'hoh_gap', ys)
        _run_pair('pct_val_freeze', 'zip_poverty_rate', 'vf_gap', ys)
        _run_mean_baseline('pct_vet', 'vet_gap', ys)


def _boost_outreach_with_gaps(nbhd_stats):
    """After exemption gaps are computed, bump outreach_need for neighborhoods
    that under-claim relative to their demographic prediction. The boost is
    capped so it augments the existing score rather than overriding it."""
    for p in nbhd_stats.values():
        need = p.get('outreach_need')
        if need is None:
            continue
        boost = 0.0
        hg = p.get('hoh_gap')
        if hg is not None and hg < -0.05:
            # −5 pp below predicted = +0.05 boost, caps at 0.15 (−15 pp below)
            boost += min(abs(hg) - 0.05, 0.10)
        vfg = p.get('vf_gap')
        if vfg is not None and vfg < -0.03:
            # Smaller caps — value freeze is a much smaller base rate
            boost += min(abs(vfg) - 0.03, 0.05)
        if boost > 0:
            p['outreach_need_gap_boost'] = round(boost, 4)
            p['outreach_need'] = round(min(1.0, need + boost), 4)


def _compute_gi_star_per_year(nbhd_stats, centroid_lookup, k=8):
    """Getis-Ord Gi* z-scores for the per-year outreach_need_YY and
    pct_vf_denied_YY series so the frontend's year selector can flip the
    hot/cold-spot cluster layers. Mirrors the client-side Gi* in the map
    HTML, including the fix that scales the denominator by the actual
    neighbor count when some neighbors have missing data.

    Writes gi_outreach_need_YY and gi_pct_vf_denied_YY onto each nbhd.
    Skips a (field, year) pair entirely if <20 nbhds have finite values
    or the overall stdev collapses.
    """
    nbhds = sorted(nbhd_stats.keys())
    n_total = len(nbhds)
    if n_total <= k:
        return
    centers = [centroid_lookup.get(n) for n in nbhds]
    knn = {}
    for i, name in enumerate(nbhds):
        c = centers[i]
        if not c:
            knn[name] = None
            continue
        dists = []
        for j, other in enumerate(nbhds):
            oc = centers[j]
            if not oc:
                continue
            dy = c[0] - oc[0]
            dx = c[1] - oc[1]
            dists.append((nbhds[j], dx * dx + dy * dy))
        dists.sort(key=lambda d: d[1])
        knn[name] = [pair[0] for pair in dists[:k]]

    year_suffixes = set()
    for p in nbhd_stats.values():
        for key in p.keys():
            m = re.match(r'^pct_vf_denied_(\d+)$', key)
            if m:
                year_suffixes.add(m.group(1))

    for base in ('outreach_need', 'pct_vf_denied'):
        for ys in sorted(year_suffixes):
            field = f'{base}_{ys}'
            values = {}
            flat = []
            for name in nbhds:
                v = nbhd_stats[name].get(field)
                if v is None:
                    continue
                if not isinstance(v, (int, float)) or v != v:  # NaN check
                    continue
                values[name] = v
                flat.append(v)
            if len(flat) < 20:
                continue
            mean = sum(flat) / len(flat)
            sq = sum((v - mean) ** 2 for v in flat)
            stdev = (sq / (len(flat) - 1)) ** 0.5 if len(flat) > 1 else 0
            if stdev <= 0:
                continue
            for name in nbhds:
                neighbors = knn.get(name)
                if not neighbors:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                local_sum = 0.0
                local_cnt = 0
                for m in neighbors:
                    v = values.get(m)
                    if v is not None:
                        local_sum += v
                        local_cnt += 1
                if local_cnt < k / 2:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                scale = stdev * ((local_cnt * (n_total - local_cnt)) / (n_total - 1)) ** 0.5
                if scale <= 0:
                    nbhd_stats[name][f'gi_{field}'] = None
                    continue
                nbhd_stats[name][f'gi_{field}'] = round(
                    (local_sum - local_cnt * mean) / scale, 4,
                )


def compute_nbhd_stats(by_nbhd_yr, existing_props, census=None, tract_geo=None):
    """
    Compute per-neighborhood aggregated stats from tax roll data.
    Merges with existing properties to preserve contact center data.
    census: optional dict with {'zips': {'87102': {'pop','income','poverty',...}}}
    tract_geo: optional TIGER GeoJSON with tract-level ACS fields merged in.
               Each nbhd centroid is tested against each tract polygon so
               tract_poverty_rate / tract_median_age / tract_elderly_alone
               flow into the vulnerability calculation below.
    """
    zip_data = (census or {}).get('zips', {}) if census else {}
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
    centroid_lookup = {}  # int(nbhd) -> (lat, lon)
    for feat in existing_props:
        n = feat['properties'].get('nbhd')
        if n is None:
            continue
        existing[int(n)] = feat['properties']
        geom = feat.get('geometry')
        if not geom:
            continue
        coords = []
        if geom.get('type') == 'Polygon':
            coords = (geom.get('coordinates') or [[]])[0]
        elif geom.get('type') == 'MultiPolygon':
            for poly in (geom.get('coordinates') or []):
                if poly and poly[0]:
                    coords.extend(poly[0])
        if coords:
            centroid_lookup[int(n)] = (
                sum(c[1] for c in coords) / len(coords),
                sum(c[0] for c in coords) / len(coords),
            )

    # Precompute tract-ACS lookup for each nbhd centroid so the vulnerability
    # calc below can use tract-level poverty / elderly-alone — finer-grained
    # than ZIP-level averages. Falls back silently if tract_geo has no ACS
    # enrichment (poverty_rate etc. will just be missing).
    tract_acs_by_nbhd = {}
    TRACT_ACS_FIELDS = ('poverty_rate', 'median_age', 'spanish_at_home', 'elderly_alone')
    if tract_geo and tract_geo.get('features'):
        matched = 0
        for n, (lat, lon) in centroid_lookup.items():
            tract = _find_tract_for_point(lat, lon, tract_geo)
            if not tract:
                continue
            tp = tract.get('properties', {})
            acs = {}
            for k in TRACT_ACS_FIELDS:
                v = tp.get(k)
                if v is not None:
                    acs[f'tract_{k}'] = v
            if acs:
                tract_acs_by_nbhd[n] = acs
                matched += 1
        if matched:
            print(f"  Tract→nbhd ACS join: {matched}/{len(centroid_lookup)} neighborhoods")

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
        # Medians below MIN_MED indicate a sparsely-valued baseline year — dividing
        # by them produces divide-by-near-zero artifacts (e.g. chg=92749). Require
        # a real baseline and clamp the ratio to a plausible range.
        MIN_MED = 1000
        YOY_MIN, YOY_MAX = -0.99, 5.0
        yr_pairs = [(all_years[i], all_years[i+1]) for i in range(len(all_years)-1)]
        yoy_changes = {}
        for y1, y2 in yr_pairs:
            recs1 = yr_data.get(y1, [])
            recs2 = yr_data.get(y2, [])
            if recs1 and recs2:
                med1 = median_safe([safe_float(r.get('TOTVALUE')) for r in recs1 if safe_float(r.get('TOTVALUE')) > 0])
                med2 = median_safe([safe_float(r.get('TOTVALUE')) for r in recs2 if safe_float(r.get('TOTVALUE')) > 0])
                if med1 >= MIN_MED and med2 >= 0:
                    ratio = (med2 - med1) / med1
                    ratio = max(YOY_MIN, min(YOY_MAX, ratio))
                    yoy_changes[f"chg_{y1%100}_{y2%100}"] = round(ratio, 4)

        # Appraisal volatility — weight recent YoY changes 2× so a
        # long-ago crisis (2008) doesn't dominate present-day outreach
        # priority. Sort by second-year of each chg_YY1_YY2 key so the
        # weighting is chronological regardless of dict insertion order.
        def _chg_sort_key(k):
            # k = 'chg_7_8' → (7, 8). Compare by the SECOND year (end of
            # the interval) so the newest interval sorts last.
            try:
                a, b = k[4:].split('_')
                return (int(b), int(a))
            except Exception:
                return (0, 0)
        chg_items_sorted = sorted(yoy_changes.items(), key=lambda kv: _chg_sort_key(kv[0]))
        chg_vals = [v for _, v in chg_items_sorted]
        if chg_vals:
            n_chg = len(chg_vals)
            # Last 3 intervals get weight 2.0, earlier ones weight 1.0.
            weights = [2.0 if i >= n_chg - 3 else 1.0 for i in range(n_chg)]
            total_w = sum(weights)
            wmean = sum(w * v for w, v in zip(weights, chg_vals)) / total_w
            if n_chg >= 2:
                wvar = sum(w * (v - wmean) ** 2 for w, v in zip(weights, chg_vals)) / total_w
                appr_vol = round(math.sqrt(wvar), 4)
            else:
                appr_vol = None
            # Cumulative volatility, weighted mean of |YoY|. Scaled by the
            # full-weight count (n_chg) so the magnitude stays comparable to
            # the prior unweighted sum.
            volatility = round(sum(w * abs(v) for w, v in zip(weights, chg_vals)) / total_w * n_chg, 4)
        else:
            appr_vol = None
            volatility = None

        # Owner turnover: share of parcels sold within the last 3 tax years.
        # LSALEDATE is the last-sale-date on the roll — in Bernalillo most
        # parcels carry a value (any prior sale leaves it populated), so a
        # bare "is LSALEDATE non-empty?" check would tag nearly every parcel
        # as turnover and flood the outreach "new homeowner" recommendation.
        TURNOVER_WINDOW = 3
        turnover_cutoff = latest_yr - TURNOVER_WINDOW + 1
        owner_chg = 0
        for r in recs:
            sale_yr = extract_year(r.get('LSALEDATE'))
            if sale_yr is not None and sale_yr >= turnover_cutoff:
                owner_chg += 1
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
        })
        # Multi-year fields: only overwrite when THIS build actually computed
        # a value. A roll-only rebuild with a single year (e.g. just
        # Asr_Tax25.dbf) can't derive hoh_churn / exemp_drift / volatility —
        # they'd come out None and silently clobber the valid values stored
        # by an earlier multi-roll build.
        _multi_yr = {
            'appr_volatility': appr_vol,
            'volatility': volatility,
            'hoh_churn': hoh_churn,
            'hoh_20': hoh_20,
            'vet_20': vet_20,
            'pct_hoh_20': pct_hoh_20,
            'pct_vet_20': pct_vet_20,
            'exemp_drift': exemp_drift,
        }
        for _k, _v in _multi_yr.items():
            if _v is not None:
                props[_k] = _v
        # Add year-over-year changes
        for key, val in yoy_changes.items():
            props[key] = val

        # Overall value change
        if chg_vals:
            cumulative = 1.0
            for c in chg_vals:
                cumulative *= (1 + c)
            props['val_change_pct'] = round(cumulative - 1, 4)

        # Per-year stats for Property characteristics by year
        for yr in all_years:
            yr_recs = yr_data.get(yr, [])
            if not yr_recs:
                continue
            ys = str(yr % 100)
            yp = len(yr_recs)
            yv = [safe_float(r.get('TOTVALUE')) for r in yr_recs if safe_float(r.get('TOTVALUE')) > 0]
            yh = sum(1 for r in yr_recs if safe_float(r.get('HOHEXEMP')) > 0)
            yvet = sum(1 for r in yr_recs if safe_float(r.get('VETEXEMP')) > 0)
            yvf = sum(1 for r in yr_recs if str(r.get('EXEMCODE', '')).strip().upper() in ('VF', 'F', 'FREEZE'))
            yyb = [safe_int(r.get('YRBUILT')) for r in yr_recs if safe_int(r.get('YRBUILT')) > 0]
            # Match the recent-window semantics used for owner_turnover above
            # so per-year owner_turnover_YY lines up with the scalar field.
            yr_cutoff = yr - TURNOVER_WINDOW + 1
            yot = sum(1 for r in yr_recs
                      if (extract_year(r.get('LSALEDATE')) or 0) >= yr_cutoff)
            props[f'avg_appraised_{ys}'] = int(statistics.mean(yv)) if yv else 0
            props[f'median_val_{ys}'] = int(median_safe(yv)) if yv else 0
            props[f'median_yrbuilt_{ys}'] = int(median_safe(yyb)) if yyb else 0
            props[f'pct_hoh_{ys}'] = round(yh / yp, 4) if yp else 0
            props[f'pct_vet_{ys}'] = round(yvet / yp, 4) if yp else 0
            props[f'pct_val_freeze_{ys}'] = round(yvf / yp, 4) if yp else 0
            props[f'owner_turnover_{ys}'] = round(yot / yp, 4) if yp else 0
            props[f'parcels_{ys}'] = float(yp)

        # Fallback: compute exemp_drift (and hoh_20 / vet_20 / earliest_yr /
        # latest_yr) from the per-year fields stored on this feature across
        # all historical builds, even when the current roll is single-year.
        # The per-year fields survive roll-only rebuilds because we initialize
        # props from existing.get(nbhd). Without this fallback, the drift
        # layer shows "no data" for anyone who first built the site with
        # a single roll.
        _hoh_by_yr = {}
        _vet_by_yr = {}
        for _k, _v in props.items():
            if _v is None or not isinstance(_v, (int, float)):
                continue
            if _k.startswith('pct_hoh_') and _k[8:].isdigit():
                _hoh_by_yr[int(_k[8:])] = _v
            elif _k.startswith('pct_vet_') and _k[8:].isdigit():
                _vet_by_yr[int(_k[8:])] = _v
        _common = sorted(set(_hoh_by_yr) & set(_vet_by_yr))
        # Defensive against legacy sentinels: historically build_data may
        # have written pct_hoh_YY=0 / pct_vet_YY=0 when a year had no
        # parcels in a nbhd. Drop a zero-pair ONLY when the year has no
        # parcels_YY on record — a legitimate 0% rate is paired with
        # parcels_YY > 0 and should stay in the tooltip range.
        def _year_has_parcels(y):
            return safe_float(props.get(f'parcels_{y}')) > 0
        _common = [
            y for y in _common
            if _hoh_by_yr[y] or _vet_by_yr[y] or _year_has_parcels(y)
        ]
        if len(_common) >= 2:
            _early, _late = _common[0], _common[-1]
            _drift = round(
                abs(_hoh_by_yr[_late] - _hoh_by_yr[_early])
                + abs(_vet_by_yr[_late] - _vet_by_yr[_early]), 4,
            )
            # Only write if this build didn't already compute a value —
            # the multi-year branch above is authoritative when present.
            if props.get('exemp_drift') is None:
                props['exemp_drift'] = _drift
            if props.get('hoh_20') is None:
                props['hoh_20'] = _hoh_by_yr[_early]
            if props.get('vet_20') is None:
                props['vet_20'] = _vet_by_yr[_early]
            if props.get('pct_hoh_20') is None:
                props['pct_hoh_20'] = _hoh_by_yr[_early]
            if props.get('pct_vet_20') is None:
                props['pct_vet_20'] = _vet_by_yr[_early]
            # earliest_yr / latest_yr drive the tooltip "HOH 2007→2025" labels.
            props['earliest_yr'] = 2000 + _early
            props['latest_yr'] = 2000 + _late

        # Fallback: hoh_churn ≈ |Δ pct_hoh| between the two most recent
        # years present. Same motivation as exemp_drift above — this
        # stays populated on single-roll rebuilds that can't derive
        # hoh_churn from fresh roll records.
        if props.get('hoh_churn') is None:
            _hoh_yrs = sorted(_hoh_by_yr.keys())
            if len(_hoh_yrs) >= 2:
                _y1, _y2 = _hoh_yrs[-2], _hoh_yrs[-1]
                props['hoh_churn'] = round(abs(_hoh_by_yr[_y2] - _hoh_by_yr[_y1]), 4)

        # Fallback: appr_volatility (std dev of YoY changes) and
        # volatility (sum of |YoY|) from preserved chg_* fields.
        if props.get('appr_volatility') is None or props.get('volatility') is None:
            _preserved_chgs = []
            for _k, _v in props.items():
                if _v is None or not isinstance(_v, (int, float)):
                    continue
                if _k.startswith('chg_') and '_' in _k[4:]:
                    _rest = _k[4:]
                    _a, _sep, _b = _rest.partition('_')
                    if _a.isdigit() and _b.isdigit():
                        _preserved_chgs.append(_v)
            if _preserved_chgs:
                if props.get('volatility') is None:
                    props['volatility'] = round(sum(abs(v) for v in _preserved_chgs), 4)
                if len(_preserved_chgs) >= 2 and props.get('appr_volatility') is None:
                    _mean = sum(_preserved_chgs) / len(_preserved_chgs)
                    _var = sum((v - _mean) ** 2 for v in _preserved_chgs) / len(_preserved_chgs)
                    props['appr_volatility'] = round(math.sqrt(_var), 4)

        # Outreach need score (0-1): multi-signal weighted composite
        # Design: need = severity × vulnerability × service_gap
        # High score requires ALL three: a real problem, a vulnerable group,
        # AND inadequate current service. Mitigates false positives from
        # wealthy residential areas that simply have high HOH rates.
        def _cap(v, ceil): return min((v or 0) / ceil, 1.0) if ceil else 0

        # Attach ZIP(s) for this neighborhood: scan roll records for common
        # ZIP field names across Bernalillo CAMA schemas. SITUSZIP (property
        # location) wins over OWNZIPCODE (owner mailing) because we want the
        # neighborhood's actual ZIP, not where absentee landlords live.
        nbhd_zips = set()
        for r in recs:
            for zf in ('SITUSZIP', 'SITEZIP', 'PROPZIP', 'OWNZIPCODE', 'OWNZIP', 'ZIPCODE', 'ZIP'):
                z = str(r.get(zf, '') or '').strip()[:5]
                if z.isdigit() and z.startswith('87'):
                    nbhd_zips.add(z)
                    break
        # Always store the roll-derived zip_codes list — the front-end
        # needs this for ZIP-based neighborhood search regardless of
        # whether ACS data is available. Demographic amplifiers below
        # still require ACS, but the raw ZIP tag doesn't.
        if nbhd_zips:
            props['zip_codes'] = ','.join(sorted(nbhd_zips))
        zip_poverty = 0
        zip_low_income = 0
        if nbhd_zips and zip_data:
            povs, incs = [], []
            for z in nbhd_zips:
                zd = zip_data.get(z)
                if not zd or not zd.get('pop'):
                    continue
                povs.append(zd['poverty'] / zd['pop'])
                if zd.get('income', 0) > 0:
                    incs.append(zd['income'])
            if povs:
                zip_poverty = sum(povs) / len(povs)   # avg ZIP poverty rate
            if incs:
                med_inc = sum(incs) / len(incs)
                # Low income = 1 when ≤ $35k, 0 when ≥ $100k
                zip_low_income = max(0, min(1, (100000 - med_inc) / 65000))
            props['zip_poverty_rate'] = round(zip_poverty, 4)
            props['zip_income_factor'] = round(zip_low_income, 4)

        # ── Severity, vulnerability, service-gap aggregation ─────────────
        # Switched from max() to a complement-product ("noisy-OR") so
        # multiple moderate issues correctly rank ABOVE a single extreme
        # issue. Each signal is still capped to [0,1] by _cap, then
        # combined as: 1 - Π(1 - vᵢ). Bounded in [0,1] by construction.
        def _noisy_or(*vals):
            p = 1.0
            for v in vals:
                p *= (1.0 - max(0.0, min(1.0, v or 0.0)))
            return 1.0 - p

        # Severity: current problems. Capturing HOH churn, VF denial, and
        # appraisal volatility. A nbhd with all three moderately elevated
        # should rank higher than one with a single extreme issue.
        sev_vf_denied = _cap(props.get('pct_vf_denied'), 0.4)
        sev_hoh_churn = _cap(hoh_churn, 0.04)
        sev_volatility = _cap(volatility, 0.5)
        severity = _noisy_or(sev_vf_denied, sev_hoh_churn, sev_volatility)

        # Vulnerability: concentration of at-risk residents. Prefer
        # tract-level poverty/elderly-alone (ACS, finer-grained) when
        # available and fall back to ZIP-level averages otherwise.
        tract_acs = tract_acs_by_nbhd.get(nbhd, {})
        for k, v in tract_acs.items():
            props[k] = v  # store on feature so it's exportable / visible
        # Tract poverty supersedes ZIP when available — a 176-tract grid
        # is much finer than the ~30-ZIP geography around ABQ.
        eff_poverty = tract_acs.get('tract_poverty_rate') if tract_acs.get('tract_poverty_rate') is not None else zip_poverty
        # Elderly-alone (householder 65+ living alone) is a strong signal
        # for HOH/VF outreach relevance that ZIP data doesn't capture.
        vul_elderly_alone = _cap(tract_acs.get('tract_elderly_alone'), 0.15)

        vul_elderly = _cap(props.get('pct_val_freeze'), 0.15)
        vul_veterans = _cap(props.get('pct_vet'), 0.12)
        vul_new_owners = _cap(owner_turnover, 0.25)
        vul_poverty = _cap(eff_poverty, 0.25)        # 25% poverty → max
        vul_low_income = zip_low_income               # already 0-1 (<=$35k → max)
        vulnerability = _noisy_or(
            vul_elderly, vul_veterans, vul_new_owners,
            vul_poverty, vul_low_income, vul_elderly_alone,
        )

        # Service gap: low engagement is the strongest equity signal.
        # Still uses max() — these two components (low contact rate and
        # high failure rate) measure the SAME underlying deficiency from
        # two angles, so compounding them would double-count.
        cpp = props.get('contacts_per_parcel', 0) or 0
        gap_low_contact = max(0, 1.0 - cpp / 0.5)    # <0.1 cpp → ~max
        gap_failure = _cap(props.get('failure_rate'), 0.15)
        # Amplify service gap when crossed with Census disadvantage signals
        # Low contacts + high poverty = classic underserved
        demographic_disadvantage = max(vul_poverty, vul_low_income)
        if gap_low_contact > 0.5 and demographic_disadvantage > 0.3:
            gap_low_contact = min(1.0, gap_low_contact * (1 + demographic_disadvantage * 0.5))
        service_gap = max(gap_low_contact, gap_failure)

        # Weighted geometric mean: sqrt(severity * vulnerability) *
        # service_gap. Each dimension must be non-trivial for a high
        # score. Noisy-OR aggregation above already inflates the
        # per-dimension values, so dropped the arbitrary 1.6 multiplier
        # that was previously needed to push max-aggregated scores up.
        sev_f = max(severity, 0.15)
        vul_f = max(vulnerability, 0.15)
        gap_f = max(service_gap, 0.15)
        base = math.sqrt(sev_f * vul_f)
        props['outreach_need'] = round(min(1.0, base * gap_f), 4)
        # Store component scores for transparency (both aggregates and
        # the individual signals so users can debug rankings).
        props['outreach_severity'] = round(severity, 4)
        props['outreach_vulnerability'] = round(vulnerability, 4)
        props['outreach_service_gap'] = round(service_gap, 4)
        props['sev_vf_denied'] = round(sev_vf_denied, 4)
        props['sev_hoh_churn'] = round(sev_hoh_churn, 4)
        props['sev_volatility'] = round(sev_volatility, 4)
        props['vul_elderly'] = round(vul_elderly, 4)
        props['vul_veterans'] = round(vul_veterans, 4)
        props['vul_new_owners'] = round(vul_new_owners, 4)

        # ── Per-year hoh_churn and outreach_need ────────────────────────
        # hoh_churn_YY = |pct_hoh_YY - pct_hoh_{YY-1}|. Iterates the
        # preserved pct_hoh_YY history so old historical years get churn
        # values too, not just the ones in the current roll.
        _hoh_series = {}
        for _k, _v in props.items():
            if _v is None or not isinstance(_v, (int, float)):
                continue
            if _k.startswith('pct_hoh_') and _k[8:].isdigit():
                _hoh_series[int(_k[8:])] = _v
        _hoh_years = sorted(_hoh_series.keys())
        for _i in range(1, len(_hoh_years)):
            _y = _hoh_years[_i]
            _prev = _hoh_years[_i - 1]
            props[f'hoh_churn_{_y}'] = round(
                abs(_hoh_series[_y] - _hoh_series[_prev]), 4,
            )

        # Per-year outreach_need_YY: same composite as the scalar above
        # but swapping in per-year severity/vulnerability inputs. Service
        # gap (contact-center volume, failure rate) has no per-year
        # history so we reuse the current-year value for every year — the
        # year selector reshapes WHICH risks are elevated, not capacity.
        # Volatility is also a multi-year aggregate; static is intended.
        for _k in list(props.keys()):
            _m = re.match(r'^pct_vf_denied_(\d+)$', _k)
            if not _m:
                continue
            _ys = _m.group(1)

            def _py(base, fallback=None):
                v = props.get(f'{base}_{_ys}')
                return v if (v is not None and isinstance(v, (int, float))) else fallback

            _y_sev_vf = _cap(_py('pct_vf_denied'), 0.4)
            _y_sev_ch = _cap(_py('hoh_churn'), 0.04)
            _y_sev_vl = _cap(volatility, 0.5)  # multi-year aggregate — static
            _y_severity = _noisy_or(_y_sev_vf, _y_sev_ch, _y_sev_vl)

            _y_vul_elderly = _cap(_py('pct_val_freeze'), 0.15)
            _y_vul_vet = _cap(_py('pct_vet'), 0.12)
            _y_vul_turn = _cap(_py('owner_turnover'), 0.25)
            _y_vulnerability = _noisy_or(
                _y_vul_elderly, _y_vul_vet, _y_vul_turn,
                vul_poverty, vul_low_income, vul_elderly_alone,
            )

            # service_gap is contact-center-driven and has no per-year
            # history — reuse the scalar service_gap computed above.
            _y_sev_f = max(_y_severity, 0.15)
            _y_vul_f = max(_y_vulnerability, 0.15)
            _y_gap_f = max(service_gap, 0.15)
            _y_base = math.sqrt(_y_sev_f * _y_vul_f)
            props[f'outreach_need_{_ys}'] = round(min(1.0, _y_base * _y_gap_f), 4)
            props[f'outreach_severity_{_ys}'] = round(_y_severity, 4)
            props[f'outreach_vulnerability_{_ys}'] = round(_y_vulnerability, 4)

        # Generate outreach recommendations with explicit reasoning.
        # Format: "Title::Why::What" split by | for multiple entries. Named
        # outreach_recs (not recs) so it doesn't shadow the year-records
        # list that's still in scope from earlier in this loop iteration.
        outreach_recs = []
        pct_hoh = props.get('pct_hoh', 0) or 0
        pct_vf_denied = props.get('pct_vf_denied', 0) or 0
        pct_vet = props.get('pct_vet', 0) or 0
        cpp = props.get('contacts_per_parcel', 0) or 0
        ot_val = owner_turnover or 0

        if pct_hoh > 0.25:
            outreach_recs.append(
                f'HOH exemption clinic::'
                f'{pct_hoh*100:.0f}% of parcels claim HOH — well above the county norm (~18%). '
                f'High claim rates mean many residents rely on this exemption and need help keeping it active.::'
                f'Walk-in clinic with application help, eligibility review, and renewal tips.'
            )
        if pct_vf_denied > 0.3:
            outreach_recs.append(
                f'Value freeze workshop::'
                f'{pct_vf_denied*100:.0f}% of VF applications denied — seniors/disabled residents '
                f'are filing but failing. Common reasons: missing income docs, over the limit, wrong form.::'
                f'Workshop covering income limits, required documents, and how to re-apply successfully.'
            )
        if volatility is not None and volatility > 0.3:
            outreach_recs.append(
                f'Property value town hall::'
                f'Appraised values swung {volatility*100:.0f}% cumulatively over recent years — '
                f'residents likely confused or frustrated by sudden increases.::'
                f'Town hall explaining the reappraisal cycle, protest rights, and what drives value changes.'
            )
        if ot_val > 0.15:
            outreach_recs.append(
                f'New homeowner orientation::'
                f'{ot_val*100:.0f}% owner turnover — a large share of residents are new to the area '
                f'and may not know about exemptions, deadlines, or how to read an assessment notice.::'
                f'Welcome session on HOH/VF/vet exemptions, deadlines, and how to read the annual notice.'
            )
        if hoh_churn is not None and hoh_churn > 0.02:
            outreach_recs.append(
                f'Exemption renewal drive::'
                f'{hoh_churn*100:.1f}% HOH churn — residents are losing their exemption year over year. '
                f'This usually means they moved, forgot to renew, or the property changed hands.::'
                f'Door-to-door or mailer campaign reminding residents to re-apply for HOH.'
            )
        if cpp < 0.3:
            outreach_recs.append(
                f'Pop-up office day::'
                f'Only {cpp:.2f} contacts/parcel — residents here rarely call or visit. '
                f'Low engagement usually signals lack of awareness, language barriers, or access issues, '
                f'not absence of need.::'
                f'Bring assessor staff on-site for a full day: Q&A, account lookups, general info.'
            )
        if pct_vet > 0.08:
            outreach_recs.append(
                f'Veteran exemption outreach::'
                f'{pct_vet*100:.0f}% veteran exemption rate — significantly above county average. '
                f'Many eligible veterans may also qualify for the disabled veteran waiver but not know it.::'
                f'Partner with VFW/American Legion for a benefits session covering both programs.'
            )
        props['outreach_recs'] = '|'.join(outreach_recs) if outreach_recs else ''

        # Top drivers summary: which signals pushed the score highest
        drivers = []
        if pct_hoh > 0.25: drivers.append(('high HOH rate', pct_hoh / 0.5))
        if pct_vf_denied > 0.3: drivers.append(('high VF denial', pct_vf_denied / 0.5))
        if volatility and volatility > 0.3: drivers.append(('value volatility', volatility / 0.5))
        if ot_val > 0.15: drivers.append(('owner turnover', ot_val / 0.3))
        if hoh_churn and hoh_churn > 0.02: drivers.append(('HOH churn', hoh_churn / 0.05))
        if cpp < 0.3: drivers.append(('low contact rate', 1.0 - cpp / 1.0))
        if pct_vet > 0.08: drivers.append(('high vet exemption', pct_vet / 0.2))
        drivers.sort(key=lambda x: -x[1])
        props['outreach_why'] = ', '.join(d[0] for d in drivers[:3]) if drivers else ''

        # Nearest community center by straight-line distance (quick,
        # geometry-free). Drive-time via OSRM is added later in main()
        # and overrides this if --osrm succeeds.
        nbhd_center = centroid_lookup.get(nbhd)
        if nbhd_center:
            best_cc = min(CC_LOCATIONS, key=lambda c: (c[1]-nbhd_center[0])**2 + (c[2]-nbhd_center[1])**2)
            props['nearest_cc'] = best_cc[0]

        updated[nbhd] = props

    # Exemption gap computation deferred to AFTER the MDF merge in main() —
    # Bernalillo's plain tax roll has no Value Freeze indicator, so
    # pct_val_freeze is 0 everywhere here and vf_gap would degenerate
    # to 0 for every neighborhood. MDF's agg_nbhd_summary fills in the
    # real pct_val_freeze a few steps later, and only then can gaps
    # carry meaningful signal.
    # Return centroid_lookup alongside so callers (main) can reuse it for
    # downstream spatial queries like OSRM drive-time catchments without
    # rebuilding the per-nbhd centroid map.
    return updated, centroid_lookup


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

    # Helper to get lat/lon and nbhd (XCOORD=longitude, YCOORD=latitude, already WGS84)
    def ll(r):
        return round(safe_float(r.get('YCOORD')), 6), round(safe_float(r.get('XCOORD')), 6)
    def nb(r):
        return safe_int(r.get('NBHD'))

    # ── Unified protest layer (all years, year-filtered) ──
    pro_all = []
    for yr, recs in enriched_by_yr.items():
        for r in recs:
            protested = str(r.get('PROTESTED', '') or '').strip()
            if protested.upper() not in ('Y', 'YES', '1', 'TRUE'):
                continue
            la, ln = ll(r)
            pro_all.append({
                'la': la, 'ln': ln, 'y': yr, 'n': nb(r),
                'ht': str(r.get('HEARING TYPE', '') or '').strip()[:1] or '',
                'st': str(r.get('HEARING STATUS', '') or '').strip()[:1] or '',
                'ra': str(r.get('RESULT ACTION', '') or '').strip() or '',
                'nv': safe_int(r.get('NOTICE VALUE')),
                'tv': safe_int(r.get('TAXPAYER VALUE')),
            })
    layers['PRO'] = pro_all
    print(f"  Protests (all years): {len(pro_all):,}")

    # ── Unified value freeze layer (all years, year-filtered) ──
    vf_active_all, vf_denied_all, vf_removed_all = [], [], []
    for yr, recs in enriched_by_yr.items():
        for r in recs:
            status = str(r.get('VAL_FREEZE_STATUS', '') or '').strip().lower()
            if not status:
                continue
            la, ln = ll(r)
            v = safe_int(r.get('APRTOTAL'))
            vf_yr = str(r.get('VAL_FREEZE_YEAR', '') or '').strip() or None
            pt = {'la': la, 'ln': ln, 'y': yr, 'n': nb(r), 'v': v, 'yr': vf_yr}
            if status == 'active':
                vf_active_all.append(pt)
            elif status == 'denied':
                vf_denied_all.append(pt)
            elif status == 'removed':
                vf_removed_all.append(pt)
    layers['VFA'] = vf_active_all
    layers['VFD'] = vf_denied_all
    layers['VFR'] = vf_removed_all
    print(f"  VF all years: active {len(vf_active_all):,}, denied {len(vf_denied_all):,}, removed {len(vf_removed_all):,}")

    # ── Unified disabled veteran waiver (all years, year-filtered) ──
    dvw_all = []
    for yr, recs in enriched_by_yr.items():
        for r in recs:
            waiver = str(r.get('DISABLED VETERAN TAX WAIVER', '') or '').strip()
            if waiver.upper() in ('Y', 'YES', '1', 'TRUE'):
                la, ln = ll(r)
                dvw_all.append({'la': la, 'ln': ln, 'y': yr, 'n': nb(r), 'v': safe_int(r.get('APRTOTAL'))})
    layers['DVW'] = dvw_all
    print(f"  Disabled vet waiver (all years): {len(dvw_all):,}")

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
            n = safe_int(r2.get('NBHD'))
            if not hoh1 and hoh2:
                eg_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': n})
            if not vet1 and vet2:
                eg_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': n})
            if hoh1 and not hoh2:
                el_h.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': n})
            if vet1 and not vet2:
                el_v.append({'la': la, 'ln': ln, 'c': 1, 'y': y2, 'n': n})

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
        if nbhd_type in ('commercial', 'comm', 'c'):
            props['nbhd_type'] = 'commercial'
        elif nbhd_type in ('residential', 'res', 'r'):
            props['nbhd_type'] = 'residential'
        elif nbhd_type in ('vacant', 'vac', 'v'):
            props['nbhd_type'] = 'vacant'
        elif nbhd_type:
            props['nbhd_type'] = nbhd_type

        # VF denial rate from enriched data
        vf_recs = [r for r in recs if str(r.get('VAL_FREEZE_STATUS', '') or '').strip()]
        if vf_recs:
            denied = sum(1 for r in vf_recs if str(r.get('VAL_FREEZE_STATUS', '')).strip().lower() == 'denied')
            props['pct_vf_denied'] = round(denied / len(vf_recs), 4)

    # Per-year VF rate from enriched data (by nbhd + year)
    by_nbhd_yr_e = defaultdict(lambda: defaultdict(list))
    for yr, recs in enriched_by_yr.items():
        for r in recs:
            nbhd = safe_int(r.get('NBHD'))
            if nbhd:
                by_nbhd_yr_e[nbhd][yr].append(r)

    for nbhd, yr_data in by_nbhd_yr_e.items():
        if nbhd not in nbhd_stats:
            continue
        props = nbhd_stats[nbhd]
        sorted_yrs = sorted(yr_data.keys())
        # Widen earliest_yr / latest_yr rather than overwrite. The HOH/VET
        # fallback in compute_nbhd_stats already set these from the full
        # pct_hoh_YY history (e.g. 2007→2025); enriched files typically
        # only cover the last 1-2 years, so a blind overwrite would shrink
        # the tooltip range to "HOH 2024→2025".
        e_new, l_new = sorted_yrs[0], sorted_yrs[-1]
        e_old, l_old = props.get('earliest_yr'), props.get('latest_yr')
        props['earliest_yr'] = min(e_new, e_old) if isinstance(e_old, (int, float)) else e_new
        props['latest_yr'] = max(l_new, l_old) if isinstance(l_old, (int, float)) else l_new
        for yr in sorted_yrs:
            recs = yr_data[yr]
            ys = str(yr % 100)
            yp = len(recs)
            vf_active = sum(1 for r in recs if str(r.get('VAL_FREEZE_STATUS', '') or '').strip().lower() == 'active')
            vf_all = [r for r in recs if str(r.get('VAL_FREEZE_STATUS', '') or '').strip()]
            vf_denied = sum(1 for r in vf_all if str(r.get('VAL_FREEZE_STATUS', '')).strip().lower() == 'denied')
            hoh_count = sum(1 for r in recs if str(r.get('HEAD OF HOUSEHOLD', '') or '').strip().upper() in ('Y', 'YES', '1', 'TRUE'))
            props[f'pct_val_freeze_{ys}'] = round(vf_active / yp, 4) if yp else 0
            props[f'pct_vf_denied_{ys}'] = round(vf_denied / len(vf_all), 4) if vf_all else 0
            props[f'pct_hoh_e_{ys}'] = round(hoh_count / yp, 4) if yp else 0


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
    2005:'#2e5e8e',2006:'#3a7a30',2007:'#b03030',2008:'#c45b00',2009:'#7b4ea3',
    2010:'#997700',2011:'#b15928',2012:'#6a3d9a',2013:'#d45f00',2014:'#1a7a1a',
    2015:'#c41a1a',2016:'#1565a0',2017:'#8c4520',2018:'#c44a90',2019:'#666666',
    2020:'#3a9e75',2021:'#d45a20',2022:'#7b3ea3',2023:'#1a5090',2024:'#c47a00',
    2025:'#157040',2026:'#c41a1a',
}


def fetch_drive_times_osrm(centroid_lookup, cc_coords, outdir,
                           osrm_url='https://router.project-osrm.org',
                           timeout=60, batch_size=90):
    """Query OSRM's Table service for driving duration from each community
    center to every neighborhood centroid. Returns {nbhd_id: minutes} using
    the minimum duration across all community centers.

    Results are cached in data/osrm_drive_times.json so only the first build
    after a change to centroid_lookup or cc_coords hits the network. If the
    OSRM request fails (timeout, rate limit, service down), the fallback
    Haversine proxy in index.html still produces a usable cc_time_min on
    the client side — this function just returns {}.

    The public OSRM demo (router.project-osrm.org) is rate-limited and may
    truncate very long URLs. We batch at ~90 destinations per request so
    each call stays well under 4 KB of query string.
    """
    cache_path = Path(outdir) / 'osrm_drive_times.json'
    # Cache key: fingerprint of the inputs. Change either the centroids
    # (new nbhds added) or the CC list and the cache is invalidated.
    nbhd_ids = sorted(centroid_lookup.keys())
    fingerprint_parts = [
        ';'.join(f'{n}:{centroid_lookup[n][0]:.4f},{centroid_lookup[n][1]:.4f}'
                 for n in nbhd_ids),
        ';'.join(f'{cc[0]}:{cc[1]:.4f},{cc[2]:.4f}' for cc in cc_coords),
    ]
    import hashlib
    fingerprint = hashlib.sha1('\n'.join(fingerprint_parts).encode()).hexdigest()[:12]

    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if cached.get('fingerprint') == fingerprint:
                print(f"  OSRM drive times: cache hit ({len(cached.get('times', {}))} nbhds)")
                return {int(k): v for k, v in cached.get('times', {}).items()}
        except Exception as e:
            print(f"  OSRM drive-time cache read failed: {e}")

    print(f"  OSRM drive times: querying {osrm_url} for {len(nbhd_ids)} nbhds × {len(cc_coords)} CCs...")

    # Source coords = community centers. Destination coords = nbhd centroids.
    # A single Table request carries len(ccs) sources and up to batch_size
    # destinations. OSRM format: "lon1,lat1;lon2,lat2;...?sources=0;1;...&destinations=N;N+1;..."
    cc_lonlat = [(cc[2], cc[1]) for cc in cc_coords]  # CC_DATA is (name,lat,lon)
    num_ccs = len(cc_lonlat)
    times = {}
    batches = [nbhd_ids[i:i + batch_size] for i in range(0, len(nbhd_ids), batch_size)]

    for batch_idx, batch in enumerate(batches):
        batch_lonlat = [(centroid_lookup[n][1], centroid_lookup[n][0]) for n in batch]
        all_coords = cc_lonlat + batch_lonlat
        coord_str = ';'.join(f'{lon:.5f},{lat:.5f}' for lon, lat in all_coords)
        sources = ';'.join(str(i) for i in range(num_ccs))
        destinations = ';'.join(str(num_ccs + i) for i in range(len(batch)))
        url = (
            f'{osrm_url.rstrip("/")}/table/v1/driving/{coord_str}'
            f'?sources={sources}&destinations={destinations}&annotations=duration'
        )
        # Per OSRM public-demo etiquette: explicit User-Agent so requests
        # aren't blocked as anonymous, and a short pause between batches
        # so we don't trip the rate limiter. Public demo's max-table-size
        # defaults to 100 coords total, which is why batch_size=90 +
        # 9 CCs = 99 fits under the limit.
        if batch_idx > 0:
            time.sleep(0.3)
        req = Request(url, headers={
            'User-Agent': 'map-bernalillo-outreach/1.0 (+build_data.py)',
            'Accept': 'application/json',
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except (URLError, Exception) as e:
            print(f"  OSRM batch {batch_idx + 1}/{len(batches)} failed: {e}")
            # Return whatever we've collected so far rather than discarding
            # partial results — a batch failure shouldn't lose the earlier
            # successful ones.
            return times
        code = data.get('code')
        if code == 'TooBig':
            # Retry this batch at half the size. A permanent failure at
            # minimum batch size falls through to the generic error path.
            if batch_size > 20:
                print(f"  OSRM TooBig at batch_size={batch_size}; retrying whole run at {batch_size // 2}")
                return fetch_drive_times_osrm(
                    centroid_lookup, cc_coords, outdir,
                    osrm_url=osrm_url, timeout=timeout, batch_size=batch_size // 2,
                )
        if code != 'Ok':
            print(f"  OSRM returned: {data.get('message', code or 'unknown')}")
            return times
        durations = data.get('durations') or []  # [num_sources][num_destinations]
        for j, nbhd_id in enumerate(batch):
            col = [durations[i][j] for i in range(num_ccs)
                   if i < len(durations) and j < len(durations[i]) and durations[i][j] is not None]
            if col:
                times[nbhd_id] = round(min(col) / 60.0, 1)

    # Save to cache
    try:
        cache_path.parent.mkdir(exist_ok=True)
        with open(cache_path, 'w') as f:
            json.dump({
                'fingerprint': fingerprint,
                'source': osrm_url,
                'times': {str(k): v for k, v in times.items()},
            }, f)
        print(f"  OSRM drive times: cached {len(times)} nbhds to {cache_path}")
    except Exception as e:
        print(f"  OSRM drive-time cache write failed: {e}")

    return times


def fetch_tract_acs(tract_geo):
    """Merge tract-level ACS 5-year variables into TRACT_GEO.features.

    Adds: poverty_rate, median_age, spanish_at_home, elderly_alone.
    Silently no-ops on network failure so the frontend degrades gracefully
    (tract layers other than addr_density will just render as missing).
    """
    if not tract_geo or not tract_geo.get('features'):
        return
    feats = tract_geo['features']
    # Variables:
    #   B01003_001E total population
    #   B01002_001E median age
    #   B17001_001E poverty-status universe; B17001_002E below poverty
    #   C16001_001E language universe (pop 5+); C16001_005E Spanish speakers
    #   B11007_001E total households; B11007_003E householder 65+ living alone
    vars_ = ','.join([
        'B01003_001E','B01002_001E','B17001_001E','B17001_002E',
        'C16001_001E','C16001_005E','B11007_001E','B11007_003E',
    ])
    for yr in [2023, 2022, 2021]:
        url = (
            f'https://api.census.gov/data/{yr}/acs/acs5?get={vars_}'
            f'&for=tract:*&in=state:35+county:001'
        )
        try:
            with urlopen(url, timeout=10) as resp:
                rows = json.loads(resp.read())
        except (URLError, Exception) as e:
            print(f"  Tract ACS {yr} failed: {e}")
            continue
        header = rows[0]
        idx = {k: header.index(k) for k in header}
        by_geoid = {}
        for row in rows[1:]:
            geoid = f"{row[idx['state']]}{row[idx['county']]}{row[idx['tract']]}"

            def _vf(k):
                """Parse an ACS cell as a float. ACS returns decimals for
                values like median age, and uses large negative sentinels
                (e.g. -666666666) for missing/suppressed data. Normalize
                both to None so downstream math doesn't blow up."""
                try:
                    raw = row[idx[k]]
                    if raw in (None, '', '-', '*'):
                        return None
                    v = float(raw)
                    if v < -1e6:  # ACS missing-data sentinels
                        return None
                    return v
                except (ValueError, TypeError):
                    return None

            def _vi(k):
                v = _vf(k)
                return int(v) if v is not None else None

            pop = _vi('B01003_001E') or 0
            pov_univ = _vi('B17001_001E') or 0
            pov_below = _vi('B17001_002E') or 0
            lang_univ = _vi('C16001_001E') or 0
            spanish = _vi('C16001_005E') or 0
            hh_univ = _vi('B11007_001E') or 0
            elderly_alone = _vi('B11007_003E') or 0
            median_age = _vf('B01002_001E')  # decimal — keep as float
            by_geoid[geoid] = {
                'poverty_rate': round(pov_below / pov_univ, 4) if pov_univ else None,
                'median_age': round(median_age, 1) if median_age is not None else None,
                'spanish_at_home': round(spanish / lang_univ, 4) if lang_univ else None,
                'elderly_alone': round(elderly_alone / hh_univ, 4) if hh_univ else None,
                'acs_year': yr,
                'tract_pop': pop,
            }
        merged = 0
        for feat in feats:
            geoid = feat.get('properties', {}).get('GEOID', '')
            if geoid in by_geoid:
                for k, v in by_geoid[geoid].items():
                    feat['properties'][k] = v
                merged += 1
        print(f"  Tract ACS {yr}: merged {merged}/{len(feats)} tracts")
        return  # stop after first successful year


def fetch_census_acs():
    """Fetch ACS 5-year data for Bernalillo County from Census API.

    County and ZIP queries fall through independently: if the 2023 county
    query succeeds but the 2023 ZIP query fails, we still try 2022 / 2021
    ZIPs instead of shipping a ZIP-less build.
    """
    vars = 'NAME,B01001_001E,B19013_001E,B17001_002E,B02001_002E,B02001_003E,B02001_004E,B02001_005E,B03003_003E,B25001_001E,B25077_001E'
    result = {}
    for yr in [2023, 2022, 2021]:
        if 'county' in result and 'zips' in result:
            break
        # County-level (only if we haven't already captured a good one)
        if 'county' not in result:
            url = f'https://api.census.gov/data/{yr}/acs/acs5?get={vars}&for=county:001&in=state:35'
            try:
                with urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read())
                    h, v = data[0], data[1]

                    # Parse a single ACS cell as int, treating missing-data
                    # sentinels (large negative values like -666666666) as 0
                    # so they don't render as literal -$666,666,666 in the UI.
                    def _cv(k):
                        raw = v[h.index(k)]
                        try:
                            x = int(raw) if raw not in (None, '', '-', '*') else 0
                        except (ValueError, TypeError):
                            return 0
                        return x if x >= 0 else 0

                    pop = _cv('B01001_001E')
                    result['county'] = {
                        'year': yr, 'name': v[h.index('NAME')], 'population': pop,
                        'median_income': _cv('B19013_001E'),
                        'poverty': _cv('B17001_002E'),
                        'hispanic': _cv('B03003_003E'),
                        'white': _cv('B02001_002E'),
                        'black': _cv('B02001_003E'),
                        'native_american': _cv('B02001_004E'),
                        'asian': _cv('B02001_005E'),
                        'housing_units': _cv('B25001_001E'),
                        'median_home_value': _cv('B25077_001E'),
                    }
                    print(f"  County ACS {yr}: pop {pop:,}")
            except (URLError, Exception) as e:
                print(f"  County ACS {yr} failed: {e}")

        # ZIP-level (ZCTAs in NM, filter to Bernalillo area)
        bern_zips = {
            '87002','87004','87008','87015','87031','87035','87042','87043',
            '87047','87048','87059','87068','87101','87102','87103','87104',
            '87105','87106','87107','87108','87109','87110','87111','87112',
            '87113','87114','87116','87117','87119','87120','87121','87122',
            '87123','87124','87131','87144','87153','87154','87158','87176',
            '87181','87184','87185','87187','87190','87191','87192','87193',
            '87194','87196','87197','87198','87199',
        }
        if 'zips' not in result:
            zip_vars = 'NAME,B01001_001E,B19013_001E,B17001_002E,B03003_003E,B25001_001E,B25077_001E'
            # Census deprecated the `in=state:` filter for ZCTA queries a few
            # years ago; the endpoint now returns HTTP 400 if you include it.
            # Query nationwide and filter by the Bernalillo-area ZCTA set below.
            zip_url = f'https://api.census.gov/data/{yr}/acs/acs5?get={zip_vars}&for=zip%20code%20tabulation%20area:*'
            try:
                with urlopen(zip_url, timeout=10) as resp:
                    zdata = json.loads(resp.read())
                    zh = zdata[0]
                    zips = {}
                    # ACS missing-data sentinels — large negative integers
                    # (-666666666 etc.) mean "suppressed for privacy" on
                    # low-population ZCTAs. Normalize those to 0 so they
                    # don't flow into the UI as literal -$666,666,666.
                    def _zv(row, col):
                        raw = row[zh.index(col)]
                        try:
                            v = int(raw) if raw not in (None, '', '-', '*') else 0
                        except (ValueError, TypeError):
                            return 0
                        return v if v >= 0 else 0
                    for row in zdata[1:]:
                        zcta = row[zh.index('zip code tabulation area')]
                        if zcta not in bern_zips:
                            continue
                        zpop = _zv(row, 'B01001_001E')
                        if zpop == 0:
                            continue
                        zips[zcta] = {
                            'name': row[zh.index('NAME')],
                            'pop': zpop,
                            'income': _zv(row, 'B19013_001E'),
                            'poverty': _zv(row, 'B17001_002E'),
                            'hispanic': _zv(row, 'B03003_003E'),
                            'units': _zv(row, 'B25001_001E'),
                            'home_val': _zv(row, 'B25077_001E'),
                        }
                    result['zips'] = zips
                    print(f"  ZIP ACS {yr}: {len(zips)} ZCTAs in Bernalillo area")
            except (URLError, Exception) as e:
                print(f"  ZIP ACS {yr} failed: {e}")
    return result if result else None


def update_html_sidebar(final_layers, html_path, stats=None):
    """Update index.html sidebar counts and year filter checkboxes
    to match the actual generated layer data.
    stats: optional dict with total_parcels, latest_yr, nbhd_count,
           snapshot_parcels={2020: N, 2021: N}"""
    if stats is None:
        stats = {}
    html_file = Path(html_path)
    if not html_file.exists():
        print(f"  Skipping HTML update: {html_path} not found")
        return

    raw = html_file.read_bytes()
    crlf = b'\r\n' in raw
    html = raw.decode('utf-8').replace('\r\n', '\n')

    # ── 1. Year filter checkboxes for tax roll point layers ──
    yr_set = set()
    for k in ['SL', 'EG_H', 'EG_V', 'EL_H', 'EL_V', 'PRO', 'VFA', 'VFD', 'VFR', 'DVW']:
        for p in final_layers.get(k, []):
            if 'y' in p:
                yr_set.add(p['y'])

    if yr_set:
        min_yr, max_yr = min(yr_set), max(yr_set)
        # Update header range
        html = re.sub(
            r'(Market activity <span[^>]*>)\d+&ndash;\d+( tax rolls)',
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
            r'(  <div style="display:flex;gap:6px;margin:4px 0 6px 2px;font-size:1[01]px;flex-wrap:wrap">\n)'
            r'(?:    <label style="margin:0"><input type="checkbox" class="yrf"[^\n]*\n)+'
            r'(  </div>)',
            rf'\1{new_block}\n\2',
            html,
        )

    # ── 2. Property characteristics header ──
    total_parcels = stats.get('total_parcels')
    latest_yr = stats.get('latest_yr')
    if total_parcels and latest_yr:
        html = re.sub(
            r'(Property characteristics <span[^>]*>)[^<]*(</span>)',
            rf'\g<1>{latest_yr} roll &middot; {total_parcels:,} parcels\2',
            html,
        )

    # ── 3. Sidebar note ──
    nbhd_count = stats.get('nbhd_count')
    if total_parcels and nbhd_count:
        html = re.sub(
            r'(\d+ neighborhoods &middot; 176 census tracts &middot; )[\d,]+ parcels \(\d+\)',
            rf'{nbhd_count} neighborhoods &middot; 176 census tracts &middot; {total_parcels:,} parcels ({latest_yr})',
            html,
        )

    # ── Census ACS data ──
    census = stats.get('census')
    if census and census.get('county'):
        c = census['county']
        pop = c['population']
        pct = lambda n: f'{n/pop*100:.1f}%' if pop else '—'
        census_html = (
            f'<div style="margin-bottom:2px;color:#888">{c["name"]} &middot; {c["year"]} ACS 5-Year</div>'
            f'<div class="s"><span>Population</span><span class="v">{pop:,}</span></div>'
            f'<div class="s"><span>Median income</span><span class="v">${c["median_income"]:,}</span></div>'
            f'<div class="s"><span>Poverty rate</span><span class="v">{pct(c["poverty"])}</span></div>'
            f'<div class="s"><span>Hispanic/Latino</span><span class="v">{pct(c["hispanic"])}</span></div>'
            f'<div class="s"><span>White alone</span><span class="v">{pct(c["white"])}</span></div>'
            f'<div class="s"><span>Black</span><span class="v">{pct(c["black"])}</span></div>'
            f'<div class="s"><span>Native American</span><span class="v">{pct(c["native_american"])}</span></div>'
            f'<div class="s"><span>Asian</span><span class="v">{pct(c["asian"])}</span></div>'
            f'<div class="s"><span>Housing units</span><span class="v">{c["housing_units"]:,}</span></div>'
            f'<div class="s"><span>Median home value</span><span class="v">${c["median_home_value"]:,}</span></div>'
        )
        zips = census.get('zips', {})
        if zips:
            sorted_zips = sorted(zips.items(), key=lambda kv: -kv[1]['pop'])
            total_pop = sum(z['pop'] for _, z in sorted_zips)
            avg_income = sum(z['income'] * z['pop'] for _, z in sorted_zips if z['income'] > 0) / max(total_pop, 1)
            avg_home = sum(z['home_val'] * z['pop'] for _, z in sorted_zips if z['home_val'] > 0) / max(total_pop, 1)
            census_html += (
                f'<details style="margin-top:6px" open>'
                f'<summary style="cursor:pointer;font-size:10px;color:#555;font-weight:600">'
                f'By ZIP &middot; {len(zips)} ZCTAs &middot; pop {total_pop:,} &middot; '
                f'avg income ${int(avg_income):,} &middot; avg home ${int(avg_home):,}</summary>'
                f'<table style="width:100%;font-size:10px;border-collapse:collapse;margin-top:4px">'
                f'<thead><tr style="border-bottom:1px solid #ddd;color:#777">'
                f'<th style="text-align:left;padding:2px 4px">ZIP</th>'
                f'<th style="text-align:right;padding:2px 4px">Pop</th>'
                f'<th style="text-align:right;padding:2px 4px">Income</th>'
                f'<th style="text-align:right;padding:2px 4px">Home val</th>'
                f'<th style="text-align:right;padding:2px 4px">Pov</th>'
                f'<th style="text-align:right;padding:2px 4px">Hisp</th>'
                f'</tr></thead><tbody>'
            )
            for z, zd in sorted_zips:
                zpct = lambda n: f'{n/zd["pop"]*100:.0f}%' if zd['pop'] else '—'
                # Low-population ZCTAs (e.g. Kirtland AFB at pop ~24)
                # have income/home_val suppressed by Census. Show '—'
                # for any zeroed-out value rather than a misleading $0.
                dollar = lambda v: f'${v:,}' if v and v > 0 else '—'
                census_html += (
                    f'<tr style="border-bottom:1px solid #f0f0f0">'
                    f'<td style="padding:2px 4px"><b>{z}</b></td>'
                    f'<td style="text-align:right;padding:2px 4px">{zd["pop"]:,}</td>'
                    f'<td style="text-align:right;padding:2px 4px">{dollar(zd["income"])}</td>'
                    f'<td style="text-align:right;padding:2px 4px">{dollar(zd["home_val"])}</td>'
                    f'<td style="text-align:right;padding:2px 4px">{zpct(zd["poverty"])}</td>'
                    f'<td style="text-align:right;padding:2px 4px">{zpct(zd["hispanic"])}</td>'
                    f'</tr>'
                )
            census_html += '</tbody></table></details>'
        # Replace census div by finding start and matching the balanced closing
        # tag (non-greedy .*? breaks because content has nested <div>).
        m = re.search(r'<div id="censusData"[^>]*>', html)
        if m:
            start = m.start()
            open_tag_end = m.end()
            # Scan forward to find the matching </div> by tracking depth
            depth = 1
            i = open_tag_end
            while i < len(html) and depth > 0:
                nxt_open = html.find('<div', i)
                nxt_close = html.find('</div>', i)
                if nxt_close == -1:
                    break
                if nxt_open != -1 and nxt_open < nxt_close:
                    depth += 1
                    i = nxt_open + 4
                else:
                    depth -= 1
                    i = nxt_close + 6
            if depth == 0:
                new_div = (
                    f'<div id="censusData" style="font-size:11px;color:#666">'
                    f'{census_html}</div>'
                )
                html = html[:start] + new_div + html[i:]

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
    parser.add_argument('--no-census', action='store_true', help='Skip Census ACS API fetch')
    parser.add_argument('--osrm-url', default='https://router.project-osrm.org',
                        help='OSRM Table-service endpoint for drive-time catchments '
                             '(default: public demo; rate-limited). Set to your own instance '
                             'for production use.')
    parser.add_argument('--no-osrm', action='store_true',
                        help='Skip OSRM drive-time query and fall back to the client-side '
                             'Haversine proxy in index.html.')
    parser.add_argument('--mdf', help='Path to MDF Complete .xlsx (multi-sheet data warehouse)')
    parser.add_argument('--mdf-dir', help='Path to folder of MDF CSV exports (faster than xlsx)')
    args = parser.parse_args()

    # Determine mode
    if args.enriched and args.coords:
        print("Warning: --enriched and --coords are mutually exclusive; using --enriched.",
              file=sys.stderr)
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

    # Fetch Census ACS early so ZIP demographics can feed outreach scores
    census = None
    if not args.no_census:
        print("\nFetching Census ACS data...")
        census = fetch_census_acs()
        # Enrich TRACT_GEO with tract-level ACS so the tract choropleth can
        # color poverty/median-age/language/elderly-alone signals.
        fetch_tract_acs(existing_core.get('TRACT_GEO'))
    else:
        print("\nSkipping Census ACS fetch (--no-census)")

    # Compute neighborhood stats from roll (census is used to amplify outreach score)
    print("\nComputing neighborhood stats...")
    nbhd_stats, centroid_lookup = compute_nbhd_stats(
        by_nbhd_yr,
        existing_core['DATA']['features'],
        census=census,
        tract_geo=existing_core.get('TRACT_GEO'),
    )

    # Drive-time catchments via OSRM (optional). Produces real driving
    # minutes that respect the street network / Rio Grande crossings
    # instead of the Haversine proxy the client falls back to.
    if not args.no_osrm:
        print("\nFetching drive-time catchments from OSRM...")
        osrm_times = fetch_drive_times_osrm(
            centroid_lookup, CC_LOCATIONS, outdir, osrm_url=args.osrm_url,
        )
        if osrm_times:
            for nbhd_id, t_min in osrm_times.items():
                if nbhd_id in nbhd_stats:
                    nbhd_stats[nbhd_id]['cc_time_min'] = t_min
                    nbhd_stats[nbhd_id]['cc_time_source'] = 'osrm'
            print(f"  OSRM cc_time_min: {len(osrm_times)}/{len(nbhd_stats)} nbhds")
        else:
            print("  OSRM unavailable — index.html will compute Haversine proxy on load")
    else:
        print("\nSkipping OSRM drive-time fetch (--no-osrm)")

    # All possible layer keys
    all_layer_keys = [
        'SL', 'RPT', 'CO', 'VO', 'MC',
        'VET_V', 'VF_V', 'HOH_V', 'PRO_V', 'SP_GEO',
        'PRO', 'VFA', 'VFD', 'VFR', 'DVW',
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
            'SL', 'PRO', 'VFA', 'VFD', 'VFR', 'DVW',
            'EG_H', 'EG_V', 'EL_H', 'EL_V'
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

    # ── MDF integration ──────────────────────────────────────────────────────────
    mdf_source = args.mdf_dir or args.mdf
    if mdf_source:
        is_dir = args.mdf_dir is not None
        print(f"\n── MDF integration: {mdf_source} ──")

        def read_mdf(sheet_name):
            if is_dir:
                csv_path = Path(mdf_source) / f'{sheet_name}.csv'
                if csv_path.exists():
                    return read_csv(str(csv_path))
                else:
                    raise FileNotFoundError(f'{csv_path} not found')
            else:
                return read_xlsx(mdf_source, sheet_name)

        # agg_nbhd_summary: merge contact center stats + anything the roll
        # can't derive directly. Value Freeze in particular isn't encoded
        # anywhere in the plain tax-roll DBF (EXEMCODE only has HOHX/VET*
        # codes for Bernalillo), so we trust the MDF's pct_val_freeze and
        # vf_count as the authoritative VF source for this neighborhood.
        try:
            nbhd_rows = read_mdf('agg_nbhd_summary')
            merged = 0
            for r in nbhd_rows:
                n = safe_int(r.get('nbhd'))
                if n and n in nbhd_stats:
                    for k in ['total_contacts','total_calls','total_failures',
                              'contacts_per_parcel','calls_per_parcel','failure_rate',
                              'sale_contact_rate','post_sale_contacts',
                              'pct_val_freeze','vf_count']:
                        if r.get(k) is not None and r.get(k) != '':
                            nbhd_stats[n][k] = safe_float(r[k])
                    merged += 1
            print(f"  agg_nbhd_summary: merged {merged} neighborhoods")
        except Exception as e:
            print(f"  agg_nbhd_summary: {e}")

        # agg_tract_summary: update tract address counts
        try:
            tract_rows = read_mdf('agg_tract_summary')
            tract_counts = {str(r.get('geoid_tract','')): safe_int(r.get('address_count'))
                           for r in tract_rows if r.get('geoid_tract')}
            for feat in existing_core.get('TRACT_GEO', {}).get('features', []):
                geoid = feat['properties'].get('GEOID', '')
                if geoid in tract_counts:
                    feat['properties']['address_count'] = tract_counts[geoid]
            print(f"  agg_tract_summary: {len(tract_counts)} tracts")
        except Exception as e:
            print(f"  agg_tract_summary: {e}")

        # dim_property: use for VF status, exemptions, property class
        try:
            prop_rows = read_mdf('dim_property')
            # Group by nbhd for class breakdown
            class_by_nbhd = defaultdict(lambda: {'R':0,'C':0,'V':0})
            for r in prop_rows:
                n = safe_int(r.get('nbhd'))
                cls = str(r.get('class', '') or '').strip().upper()
                if n and cls in ('R','C','V'):
                    class_by_nbhd[n][cls] += 1
            for n, counts in class_by_nbhd.items():
                if n in nbhd_stats:
                    total = sum(counts.values())
                    nbhd_stats[n]['res_count'] = counts['R']
                    nbhd_stats[n]['comm_count'] = counts['C']
                    nbhd_stats[n]['vacant_count'] = counts['V']
                    nbhd_stats[n]['pct_residential'] = round(counts['R'] / total, 4) if total else 0
            print(f"  dim_property: class breakdown for {len(class_by_nbhd)} neighborhoods")
        except Exception as e:
            print(f"  dim_property: {e}")

        # fact_visitors: rebuild visitor layers from service types
        try:
            vis_rows = read_mdf('fact_visitors')
            svc_types = read_mdf('dim_service_type')
            svc_map = {safe_int(s.get('service_type_id')): s.get('service_type_name','')
                      for s in svc_types}
            vet_v, vf_v, hoh_v, pro_v, sp_geo = [], [], [], [], []
            for r in vis_rows:
                x = safe_float(r.get('prop_xcoord') or r.get('xcoord'))
                y = safe_float(r.get('prop_ycoord') or r.get('ycoord'))
                if not x or not y:
                    continue
                la, ln = round(y, 6), round(x, 6)
                dk = str(r.get('date_key', '') or '')
                yr = safe_int(dk[:4]) if len(dk) >= 4 else 0
                if not yr:
                    continue
                dur = safe_float(r.get('visit_duration_min'))
                svc = svc_map.get(safe_int(r.get('service_type_id')), '').lower()
                pt = {'la': la, 'ln': ln, 'y': yr, 'd': round(dur, 1)}
                if 'veteran' in svc or 'vet' in svc:
                    vet_v.append(pt)
                elif 'freeze' in svc or 'value freeze' in svc:
                    vf_v.append(pt)
                elif 'head of household' in svc or 'hoh' in svc:
                    hoh_v.append(pt)
                elif 'protest' in svc:
                    pro_v.append(pt)
                if str(r.get('is_spanish_speaker', '') or '').strip().upper() in ('Y','YES','1','TRUE'):
                    sp_geo.append(pt)
            new_layers['VET_V'] = vet_v
            new_layers['VF_V'] = vf_v
            new_layers['HOH_V'] = hoh_v
            new_layers['PRO_V'] = pro_v
            new_layers['SP_GEO'] = sp_geo
            if 'VET_V' not in rebuilt_keys:
                rebuilt_keys.extend(['VET_V','VF_V','HOH_V','PRO_V','SP_GEO'])
            print(f"  fact_visitors: VET={len(vet_v)}, VF={len(vf_v)}, HOH={len(hoh_v)}, PRO={len(pro_v)}, SP={len(sp_geo)}")
        except Exception as e:
            print(f"  fact_visitors: {e}")

        # fact_calls + bridge_phone + bridge_property: phone channel layers
        try:
            phone_rows = read_mdf('bridge_phone')
            prop_rows2 = read_mdf('bridge_property')
            prop_map = {}
            for r in prop_rows2:
                ph = str(r.get('phone_hash', '') or '').strip()
                x = safe_float(r.get('xcoord'))
                y = safe_float(r.get('ycoord'))
                if ph and x and y:
                    prop_map[ph] = (round(y, 6), round(x, 6))
            rpt, co, vo, mc = [], [], [], []
            for r in phone_rows:
                ph = str(r.get('phone_hash', '') or '').strip()
                if ph not in prop_map:
                    continue
                la, ln = prop_map[ph]
                calls = safe_int(r.get('call_count'))
                visits = safe_int(r.get('visit_count'))
                is_mc = str(r.get('is_multichannel', '') or '').strip().upper() in ('Y','YES','1','TRUE')
                if calls >= 5:
                    rpt.append({'la': la, 'ln': ln, 'c': calls})
                if calls > 0 and visits == 0:
                    co.append({'la': la, 'ln': ln})
                elif visits > 0 and calls == 0:
                    vo.append({'la': la, 'ln': ln})
                if is_mc:
                    mc.append({'la': la, 'ln': ln})
            new_layers['RPT'] = rpt
            new_layers['CO'] = co[:2000]
            new_layers['VO'] = vo[:5000]
            new_layers['MC'] = mc
            if 'RPT' not in rebuilt_keys:
                rebuilt_keys.extend(['RPT','CO','VO','MC'])
            print(f"  Phone channel: RPT={len(rpt)}, CO={len(co)}, VO={len(vo)}, MC={len(mc)}")
        except Exception as e:
            print(f"  Phone channel: {e}")

        # fact_sale_contact_lag: sale-to-contact timing per neighborhood
        try:
            lag_rows = read_mdf('fact_sale_contact_lag')
            lag_by_nbhd = defaultdict(lambda: {'sales': 0, 'contacted': 0})
            for r in lag_rows:
                n = safe_int(r.get('NBHD'))
                if n:
                    lag_by_nbhd[n]['sales'] += 1
                    if r.get('first_contact') or r.get('first_call'):
                        lag_by_nbhd[n]['contacted'] += 1
            for n, d in lag_by_nbhd.items():
                if n in nbhd_stats and d['sales'] > 0:
                    nbhd_stats[n]['sale_contact_rate'] = round(d['contacted'] / d['sales'], 4)
            print(f"  fact_sale_contact_lag: {len(lag_by_nbhd)} neighborhoods")
        except Exception as e:
            print(f"  fact_sale_contact_lag: {e}")

        # ref_county_demographics + ref_zcta_demographics: log row counts.
        # We already populate the sidebar from fetch_census_acs (live ACS),
        # so these rows are just read to verify the MDF has them.
        try:
            county_rows = read_mdf('ref_county_demographics')
            zcta_rows = read_mdf('ref_zcta_demographics')
            print(f"  Demographics: county={len(county_rows)} rows, zcta={len(zcta_rows)} rows")
        except Exception as e:
            print(f"  Demographics: {e}")

    # ── Post-MDF: now that pct_val_freeze etc. carry real MDF values,
    # compute the exemption gaps and boost outreach_need for nbhds
    # under-claiming relative to demographic prediction. Doing this here
    # rather than inside compute_nbhd_stats is important — the roll
    # doesn't carry VF data, so running gaps before MDF merge produced
    # vf_gap=0 for every neighborhood.
    print("\nRecomputing exemption gaps (post-MDF)...")
    _compute_exemption_gaps(nbhd_stats)
    _boost_outreach_with_gaps(nbhd_stats)
    print(f"  Gaps computed for {sum(1 for p in nbhd_stats.values() if p.get('vf_gap') is not None)} neighborhoods")

    # Per-year Gi* for hot/cold-spot cluster layers. Runs AFTER the gap
    # boost so the current-year outreach_need_YY already carries the
    # gap-feedback signal (only the scalar outreach_need is boosted
    # today, so the per-year Gi* is unboosted for historical years —
    # documented on the layer help text).
    print("\nComputing per-year Gi* cluster z-scores...")
    _compute_gi_star_per_year(nbhd_stats, centroid_lookup)
    _gi_yrs = len({k.rsplit('_', 1)[-1] for p in nbhd_stats.values()
                   for k in p.keys() if k.startswith('gi_outreach_need_')})
    print(f"  Per-year Gi*: {_gi_yrs} years")

    # ── Assemble core.json ──
    print("\nAssembling core.json...")
    for feat in existing_core['DATA']['features']:
        # .get('nbhd', 0) can still return None when nbhd is present but null
        # in core.json — int(None) would crash here. Use safe_int so missing
        # / null / non-numeric nbhds just fall through the lookup.
        nbhd = safe_int(feat['properties'].get('nbhd'))
        if nbhd and nbhd in nbhd_stats:
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

    # ── Compute sidebar stats from data ──
    all_roll_years = set()
    for nbhd, yr_data in by_nbhd_yr.items():
        all_roll_years.update(yr_data.keys())
    latest_yr = max(all_roll_years) if all_roll_years else 2025
    total_parcels = sum(int(props.get('parcels', 0)) for props in nbhd_stats.values())

    snapshot_parcels = {}
    for snap_yr in [2020, 2021]:
        snap_count = sum(len(yr_data.get(snap_yr, []))
                         for yr_data in by_nbhd_yr.values())
        if snap_count:
            snapshot_parcels[snap_yr] = snap_count

    sidebar_stats = {
        'total_parcels': total_parcels,
        'latest_yr': latest_yr,
        'nbhd_count': len(nbhd_stats),
        'snapshot_parcels': snapshot_parcels,
        'census': census,
    }

    # ── Update HTML sidebar counts ──
    html_path = outdir.parent / 'index.html'
    print(f"\nUpdating {html_path}...")
    update_html_sidebar(final_layers, html_path, stats=sidebar_stats)

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
    for k in ['SL', 'EG_H', 'EG_V', 'EL_H', 'EL_V', 'PRO', 'VFA', 'VFD', 'VFR', 'DVW']:
        for p in final_layers.get(k, []):
            if 'y' in p:
                yr_set.add(p['y'])
    if yr_set:
        print(f"\n  Tax roll point years: {sorted(yr_set)}")
        print(f"  Note: include the prior year's roll file to get points for the earliest year.")

    print("\nDone! Commit and push to update the site.")


if __name__ == '__main__':
    main()
