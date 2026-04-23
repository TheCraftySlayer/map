"""Populate DPI, uptake ratios, and trend slopes on an existing core.json
WITHOUT rebuilding from the tax-roll DBFs. All inputs are already present
from the prior build — the scoring is pure-Python over the properties dict.

Usage:
    python scripts/enrich_core.py <core.json> [<out.json>]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from buildlib.scoring import (
    _compute_dpi_per_year,
    _compute_uptake_ratios,
    _compute_trend_slopes,
)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: enrich_core.py <core.json> [<out.json>]")
    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else in_path

    core = json.loads(in_path.read_text())
    feats = core["DATA"]["features"]
    # The scoring functions expect {nbhd_id: props_dict}. Build that view,
    # let them mutate the dicts in place, then write back.
    nbhd_stats = {}
    for f in feats:
        p = f.get("properties") or {}
        nbhd_id = p.get("nbhd")
        if nbhd_id is None:
            continue
        nbhd_stats[int(nbhd_id)] = p

    before = sum(1 for p in nbhd_stats.values() if any(k.startswith("dpi_") for k in p))
    _compute_dpi_per_year(nbhd_stats)
    _compute_uptake_ratios(nbhd_stats)
    _compute_trend_slopes(nbhd_stats)
    after = sum(1 for p in nbhd_stats.values() if any(k.startswith("dpi_") for k in p))
    uptake = sum(1 for p in nbhd_stats.values() if p.get("hoh_uptake") is not None)
    slope = sum(1 for p in nbhd_stats.values() if p.get("outreach_need_slope") is not None)

    out_path.write_text(json.dumps(core, separators=(",", ":")))
    print(f"DPI populated: {after}/{len(nbhd_stats)} nbhds (was {before})")
    print(f"HOH uptake:    {uptake}/{len(nbhd_stats)} nbhds")
    print(f"Slope fields:  {slope}/{len(nbhd_stats)} nbhds")
    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
