"""Geometry and small statistics helpers.

Pure-Python, no external deps. Used by scoring and by the main ETL in
build_data.py (centroid lookup, tract attribution, value-vs-demographic
fits).
"""
from __future__ import annotations


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
    """Single-predictor OLS: returns {slope, intercept, n} or None if n<8.

    The n>=8 threshold targets demographic regressions (pct_hoh vs
    zip_poverty_rate across nbhds). For short per-nbhd time series
    (where n is the number of tax years, typically 6-19), scoring.py
    uses its own small OLS with a lower minimum.
    """
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
    n = 0
    s = 0.0
    for p in nbhd_stats.values():
        v = p.get(field)
        if v is None:
            continue
        try:
            s += float(v); n += 1
        except (TypeError, ValueError):
            continue
    return s / n if n else None
