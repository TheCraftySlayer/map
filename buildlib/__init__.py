"""buildlib — shared helpers extracted from build_data.py.

The split (as of this commit) isolates the pure-Python, reusable functions
so other scripts can import them without pulling the whole ETL.

Modules:
  io_utils   — safe_float/int, extract_year, median_safe, dbf/xlsx/csv readers,
               coord transform, community-center list.
  spatial    — point-in-polygon, tract lookup, OLS fit, column mean.
  census     — ACS disk cache, fetch_census_acs, fetch_tract_acs, OSRM drive
               times.
  scoring    — _cap, _noisy_or, exemption gap/boost, Gi* clusters, DPI_YY,
               uptake ratios, trend slopes, low-confidence flag.
  pipeline   — extracted assembly + write-out tail (merge stats into core,
               assemble layers, write JSON). Re-usable from one-off scripts.

build_data.py re-exports everything listed here so existing callers and the
test suite don't need to change their imports.
"""
