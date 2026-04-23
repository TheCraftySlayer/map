"""File I/O, coercion helpers, coord transform, community center list."""
from __future__ import annotations

import csv
import re
import statistics
import sys

try:
    from dbfread import DBF
except ImportError:  # pragma: no cover
    sys.exit("Install dbfread: pip install dbfread")

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover
    sys.exit("Install pyproj: pip install pyproj")

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# NM State Plane Central (feet) → WGS84
TRANSFORMER = Transformer.from_crs("EPSG:2903", "EPSG:4326", always_xy=True)


# Community center locations (name, lat, lon) — used by nearest-CC and
# drive-time catchments in build_data.py / scoring.
CC_LOCATIONS = [
    ('North Domingo Baca', 35.1846923, -106.5488586),
    ('Manzano Mesa', 35.0557327, -106.5168607),
    ('Bear Canyon', 35.1247139, -106.5335327),
    ('Paradise Hills', 35.2022263, -106.6977911),
    ('North Valley', 35.1630248, -106.6494293),
    ('Los Volcanes', 35.0926628, -106.7391891),
    ('Palo Duro', 35.0785637, -106.5935669),
    ('South Valley Senior', 35.069824, -106.6881653),
    ('Alamosa', 35.0714679, -106.7101371),
]


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
