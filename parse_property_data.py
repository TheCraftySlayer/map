#!/usr/bin/env python3
"""
Parse raw Bernalillo County property assessment TSV data into JSON formats
used by the Spatial Equity Map v2 application.

Input:  Tab-separated property data (raw_property_data.tsv)
Output: JSON files for each data constant the map app consumes

Column mapping (0-indexed):
  0  year             Tax year
  1  parcel_id        Unique parcel identifier
  2  owner            Owner name
  3  phone            Phone number
  4  mail_type        Mail type (always "maildat")
  5  longitude        Parcel longitude
  6  latitude         Parcel latitude
  7  land_val         Land assessed value
  8  improve_val      Improvement assessed value
  9  total_val        Total assessed value
 10  prop_type        Property type code (R/V/C)
 11  prop_type_desc   Property type description
 12  nbhd             Neighborhood code
 13  nbhd_type        Neighborhood type (Residential/Commercial)
 14  res_class        Residential class code (RES/NR)
 15  res_class_desc   Residential class description
 17  hoh_flag         Head of Household exemption (Y)
 19  vet_flag         Veteran exemption (Y)
 20  val_freeze       Active value freeze (Y)
 24  spanish_flag     Spanish-speaking contact (Y)
 26  vf_history       Value freeze history (e.g. "2022 GRANTED")
 27  vf_status        Value freeze status (Active/Removed/Denied)
 28  vf_year          Value freeze year
 29  protest_flag     Protest filed (Y)
 30  protest_type     Protest type (B)
 31  protest_date     Protest date
 33  sale_type        Recent sale type (R)
 34  sale_assessed    Sale assessed value
 35  sale_price       Sale price
"""

import csv
import json
import sys
import os
from collections import defaultdict
from statistics import median


def parse_int(val, default=0):
    try:
        return int(val.strip())
    except (ValueError, AttributeError):
        return default


def parse_float(val, default=0.0):
    try:
        return float(val.strip())
    except (ValueError, AttributeError):
        return default


def get_col(row, idx, default=''):
    if idx < len(row):
        return row[idx].strip()
    return default


def load_data(filepath):
    """Load and parse the raw TSV file into structured records."""
    records = []
    with open(filepath, newline='') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if len(row) < 16:
                continue
            rec = {
                'year': parse_int(row[0]),
                'parcel_id': get_col(row, 1),
                'owner': get_col(row, 2),
                'phone': get_col(row, 3),
                'lon': parse_float(row[5]),
                'lat': parse_float(row[6]),
                'land_val': parse_int(row[7]),
                'improve_val': parse_int(row[8]),
                'total_val': parse_int(row[9]),
                'prop_type': get_col(row, 10),
                'prop_type_desc': get_col(row, 11),
                'nbhd': get_col(row, 12),
                'nbhd_type': get_col(row, 13),
                'res_class': get_col(row, 14),
                'is_residential': get_col(row, 14) == 'RES',
                'is_commercial': get_col(row, 13) == 'Commercial',
                'hoh': get_col(row, 17) == 'Y',
                'vet': get_col(row, 19) == 'Y',
                'val_freeze': get_col(row, 20) == 'Y',
                'spanish': get_col(row, 24) == 'Y',
                'vf_history': get_col(row, 26),
                'vf_status': get_col(row, 27),
                'vf_year': get_col(row, 28),
                'protest': get_col(row, 29) == 'Y',
                'protest_type': get_col(row, 30),
                'protest_date': get_col(row, 31),
                'sale_type': get_col(row, 33),
                'sale_assessed': parse_int(get_col(row, 34)),
                'sale_price': parse_int(get_col(row, 35)),
            }
            records.append(rec)
    return records


def aggregate_by_neighborhood(records):
    """Aggregate parcel-level data into neighborhood-level statistics.

    Returns a dict keyed by neighborhood code with computed metrics
    matching the DATA FeatureCollection properties schema.
    """
    groups = defaultdict(list)
    for rec in records:
        groups[rec['nbhd']].append(rec)

    results = {}
    for nbhd, parcels in groups.items():
        n = len(parcels)
        total_vals = [p['total_val'] for p in parcels if p['total_val'] > 0]
        avg_appraised = round(sum(total_vals) / len(total_vals)) if total_vals else 0

        hoh_count = sum(1 for p in parcels if p['hoh'])
        vet_count = sum(1 for p in parcels if p['vet'])
        vf_count = sum(1 for p in parcels if p['val_freeze'])
        sale_count = sum(1 for p in parcels if p['sale_type'] == 'R')
        protest_count = sum(1 for p in parcels if p['protest'])
        spanish_count = sum(1 for p in parcels if p['spanish'])

        # Value freeze denial rate: denied / (denied + active + removed) for parcels with VF history
        vf_with_history = [p for p in parcels if p['vf_status']]
        vf_denied = sum(1 for p in vf_with_history if p['vf_status'] == 'Denied')
        pct_vf_denied = round(vf_denied / len(vf_with_history), 4) if vf_with_history else 0

        is_commercial = 1 if all(p['is_commercial'] for p in parcels) else 0

        results[nbhd] = {
            'nbhd': float(nbhd) if nbhd.replace('.', '').isdigit() else nbhd,
            'parcels': n,
            'avg_appraised': avg_appraised,
            'pct_hoh': round(hoh_count / n, 4) if n else 0,
            'pct_vet': round(vet_count / n, 4) if n else 0,
            'pct_val_freeze': round(vf_count / n, 4) if n else 0,
            'vf_count': vf_count,
            'pct_vf_denied': pct_vf_denied,
            'pct_recent_sale': round(sale_count / n, 4) if n else 0,
            'recent_sales': sale_count,
            'pct_spanish': round(spanish_count / n, 4) if n else 0,
            'is_commercial': is_commercial,
            'protest_count': protest_count,
        }

    return results


def build_sale_points(records):
    """Extract recent sale point data in the SL format.

    SL format: {la, ln, d, p, b, y}
      la = latitude, ln = longitude
      d  = days since sale (not available in this data, default 0)
      p  = sale price
      b  = bucket label
      y  = year
    """
    points = []
    for rec in records:
        if rec['sale_type'] != 'R' or rec['sale_price'] <= 0:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': 0,
            'p': rec['sale_price'],
            'b': 'recent',
            'y': rec['year'],
        })
    return points


def build_exemption_points(records, flag_key, year_key='year'):
    """Extract exemption gained/lost point data in the EG/EL format.

    EG/EL format: {la, ln, h, v, c, y}
      h = HOH flag (1/0), v = VET flag (1/0), c = count, y = year
    """
    points = []
    for rec in records:
        if not rec.get(flag_key):
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'h': 1 if rec['hoh'] else 0,
            'v': 1 if rec['vet'] else 0,
            'c': 1,
            'y': rec.get(year_key, rec['year']),
        })
    return points


def build_hoh_points(records):
    """Extract HOH exemption point data in HOH_V format: {la, ln, d, y}
    d = total_val as a proxy for the metric displayed.
    """
    points = []
    for rec in records:
        if not rec['hoh']:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': round(rec['total_val'] / 1000, 1),
            'y': rec['year'],
        })
    return points


def build_vet_points(records):
    """Extract Veteran exemption point data in VET_V format: {la, ln, d, y}"""
    points = []
    for rec in records:
        if not rec['vet']:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': round(rec['total_val'] / 1000, 1),
            'y': rec['year'],
        })
    return points


def build_vf_points(records):
    """Extract value freeze point data in VF_V format: {la, ln, d, y}"""
    points = []
    for rec in records:
        if not rec['val_freeze']:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': round(rec['total_val'] / 1000, 1),
            'y': rec['year'],
        })
    return points


def build_protest_points(records):
    """Extract protest point data in PRO_V format: {la, ln, d, y}"""
    points = []
    for rec in records:
        if not rec['protest']:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': round(rec['total_val'] / 1000, 1),
            'y': rec['year'],
        })
    return points


def build_spanish_points(records):
    """Extract Spanish-speaking contact points in SP_GEO format: {la, ln, d, y}"""
    points = []
    for rec in records:
        if not rec['spanish']:
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'd': round(rec['total_val'] / 1000, 1),
            'y': rec['year'],
        })
    return points


def build_vf_denied_points(records):
    """Extract value freeze denied points in VF_DENIED format: {la, ln, v, yr}"""
    points = []
    for rec in records:
        if rec['vf_status'] != 'Denied':
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'v': rec['total_val'],
            'yr': rec['vf_year'],
        })
    return points


def build_vf_inproc_points(records):
    """Extract value freeze active/in-process points in VF_INPROC format: {la, ln, v}"""
    points = []
    for rec in records:
        if rec['vf_status'] != 'Active':
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'v': rec['total_val'],
        })
    return points


def build_turnover_points(records):
    """Extract owner turnover point data in TL format: {la, ln, c, v, y}
    We mark parcels with recent sales as turnover events.
    """
    points = []
    for rec in records:
        if rec['sale_type'] != 'R':
            continue
        points.append({
            'la': round(rec['lat'], 6),
            'ln': round(rec['lon'], 6),
            'c': 1,
            'v': 0,
            'y': rec['year'],
        })
    return points


def build_nbhd_centers(records):
    """Compute neighborhood centroids from parcel coordinates."""
    groups = defaultdict(list)
    for rec in records:
        if rec['lat'] != 0 and rec['lon'] != 0:
            groups[rec['nbhd']].append((rec['lat'], rec['lon']))

    centers = {}
    for nbhd, coords in groups.items():
        avg_lat = round(sum(c[0] for c in coords) / len(coords), 6)
        avg_lon = round(sum(c[1] for c in coords) / len(coords), 6)
        centers[nbhd] = [avg_lat, avg_lon]
    return centers


def write_json(data, filepath, compact=True):
    """Write data to a JSON file."""
    with open(filepath, 'w') as f:
        if compact:
            json.dump(data, f, separators=(',', ':'))
        else:
            json.dump(data, f, indent=2)
    size = os.path.getsize(filepath)
    print(f"  {filepath}: {size:,} bytes")


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'raw_property_data.tsv'
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'parsed_data'

    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    print(f"Loading data from {input_file}...")
    records = load_data(input_file)
    print(f"  Loaded {len(records):,} parcel records")

    year = records[0]['year'] if records else 'unknown'
    print(f"  Tax year: {year}")

    # Count key categories
    res = sum(1 for r in records if r['prop_type'] == 'R')
    vac = sum(1 for r in records if r['prop_type'] == 'V')
    com = sum(1 for r in records if r['prop_type'] == 'C')
    print(f"  Types: {res:,} residential, {vac:,} vacant, {com:,} commercial")

    nbhds = set(r['nbhd'] for r in records)
    print(f"  Neighborhoods: {len(nbhds)}")

    print(f"\nGenerating output files in {output_dir}/...")

    # Neighborhood aggregated stats
    nbhd_stats = aggregate_by_neighborhood(records)
    write_json(nbhd_stats, os.path.join(output_dir, 'neighborhood_stats.json'), compact=False)

    # Point data layers
    sl = build_sale_points(records)
    print(f"  Sales points: {len(sl)}")
    write_json(sl, os.path.join(output_dir, 'SL.json'))

    tl = build_turnover_points(records)
    print(f"  Turnover points: {len(tl)}")
    write_json(tl, os.path.join(output_dir, 'TL.json'))

    hoh = build_hoh_points(records)
    print(f"  HOH points: {len(hoh)}")
    write_json(hoh, os.path.join(output_dir, 'HOH_V.json'))

    vet = build_vet_points(records)
    print(f"  VET points: {len(vet)}")
    write_json(vet, os.path.join(output_dir, 'VET_V.json'))

    vf = build_vf_points(records)
    print(f"  Value freeze points: {len(vf)}")
    write_json(vf, os.path.join(output_dir, 'VF_V.json'))

    pro = build_protest_points(records)
    print(f"  Protest points: {len(pro)}")
    write_json(pro, os.path.join(output_dir, 'PRO_V.json'))

    sp = build_spanish_points(records)
    print(f"  Spanish-speaking points: {len(sp)}")
    write_json(sp, os.path.join(output_dir, 'SP_GEO.json'))

    vf_denied = build_vf_denied_points(records)
    print(f"  VF denied points: {len(vf_denied)}")
    write_json(vf_denied, os.path.join(output_dir, 'VF_DENIED.json'))

    vf_inproc = build_vf_inproc_points(records)
    print(f"  VF in-process points: {len(vf_inproc)}")
    write_json(vf_inproc, os.path.join(output_dir, 'VF_INPROC.json'))

    # Neighborhood centers
    centers = build_nbhd_centers(records)
    write_json(centers, os.path.join(output_dir, 'NBHD_CENTERS.json'))

    # Summary report
    summary = {
        'tax_year': year,
        'total_parcels': len(records),
        'residential': res,
        'vacant': vac,
        'commercial': com,
        'neighborhoods': len(nbhds),
        'hoh_exemptions': sum(1 for r in records if r['hoh']),
        'vet_exemptions': sum(1 for r in records if r['vet']),
        'val_freeze_active': sum(1 for r in records if r['val_freeze']),
        'vf_history_total': sum(1 for r in records if r['vf_status']),
        'vf_denied': len(vf_denied),
        'vf_in_process': len(vf_inproc),
        'protests': sum(1 for r in records if r['protest']),
        'recent_sales': len(sl),
        'spanish_contacts': len(sp),
        'neighborhood_codes': sorted(nbhds),
    }
    write_json(summary, os.path.join(output_dir, 'summary.json'), compact=False)

    print(f"\nDone. {len(os.listdir(output_dir))} files written to {output_dir}/")


if __name__ == '__main__':
    main()
