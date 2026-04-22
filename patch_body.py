#!/usr/bin/env python3
"""
patch_body.py — apply the four map-body fixes to a local plaintext HTML
copy so it can be re-encrypted into index_body.html.enc.

Why this script exists
  The plaintext map body (typically named index_body.html) is intentionally
  kept out of the repo — only the AES-GCM ciphertext ships. That means a
  direct code change to the body has to happen locally against your own
  plaintext copy, then be re-encrypted with encrypt_data.py.

What it fixes

  1. getNbhdColor treats missing values as 0. `p[f]||0` coerces
     null/undefined into 0, which paints "no data for this year" as the
     lowest-value color on the choropleth (e.g. a nbhd missing avg_appraised
     shows up painted as "$0"). Swap to Number.isFinite guards so missing
     renders as gray — matching what median_yrbuilt / pct_vf_denied already do.

  2. hiNbhd / rhNbhd highlight hidden features. When a threshold filter
     hides a neighborhood (fillOpacity=0), the transparent polygon still
     receives pointer events, so hovering it still pops the border in and
     rewrites the tooltip. Short-circuit when featureHidden() is true.

  3. Getis-Ord Gi* denominator uses K even when some neighbors are missing
     values. If only localCnt of the K nearest neighbors have finite values,
     subtracting K*mean and dividing by a K-based scale inflates the z-score
     in direct proportion to how much data is missing. Use localCnt in both
     the centering term and the denominator.

  4. propYrFields omits hoh_churn, outreach_need, and the two Gi* cluster
     layers. build_data.py now writes hoh_churn_YY / outreach_need_YY /
     gi_outreach_need_YY / gi_pct_vf_denied_YY so the year selector can
     flip those layers too — but the HTML only honors the selector for
     the fields listed in propYrFields. Extend the map.

Usage
  python patch_body.py path/to/index_body.html

  Writes a backup alongside as path/to/index_body.html.bak before editing.
  Safe to re-run — each patch is idempotent (looks for the original text
  and prints a note if already applied).
"""

import argparse
import shutil
import sys
from pathlib import Path


# ── Patch 1: getNbhdColor null-as-zero bug ────────────────────────────────────
P1_OLD = (
    "  if(layer==='avg_appraised'){const f=LAYER_FIELD[layer]||'avg_appraised';return interp(CS.avg_appraised,p[f]||0);}\n"
    "  if(layer==='median_yrbuilt'){const f=LAYER_FIELD[layer]||'median_yrbuilt';return(p[f]>0)?interp(CS.median_yrbuilt,p[f]):'#f0f0f0';}\n"
    "  if(layer==='valfreeze'){const f=LAYER_FIELD[layer]||'pct_val_freeze';return interp(CS.valfreeze,p[f]||0);}\n"
    "  if(layer==='pct_vf_denied'){const f=LAYER_FIELD[layer]||'pct_vf_denied';return(p[f]>0)?interp(CS.pct_vf_denied,p[f]):'#f0f0f0';}\n"
    "  if(layer==='pct_hoh'){const f=LAYER_FIELD[layer]||'pct_hoh';return interp(CS.pct_hoh,p[f]||0);}\n"
    "  if(layer==='pct_vet'){const f=LAYER_FIELD[layer]||'pct_vet';return interp(CS.pct_vet,p[f]||0);}\n"
    "  if(layer==='owner_turnover'){const f=LAYER_FIELD[layer]||'owner_turnover';return interp(CS.owner_turnover,p[f]||0);}\n"
    "  if(layer==='hoh_churn')return interp(CS.hoh_churn,p.hoh_churn||0);\n"
)
P1_NEW = (
    "  if(layer==='avg_appraised'){const f=LAYER_FIELD[layer]||'avg_appraised';return(Number.isFinite(p[f])&&p[f]>0)?interp(CS.avg_appraised,p[f]):'#f0f0f0';}\n"
    "  if(layer==='median_yrbuilt'){const f=LAYER_FIELD[layer]||'median_yrbuilt';return(Number.isFinite(p[f])&&p[f]>0)?interp(CS.median_yrbuilt,p[f]):'#f0f0f0';}\n"
    "  if(layer==='valfreeze'){const f=LAYER_FIELD[layer]||'pct_val_freeze';return Number.isFinite(p[f])?interp(CS.valfreeze,p[f]):'#f0f0f0';}\n"
    "  if(layer==='pct_vf_denied'){const f=LAYER_FIELD[layer]||'pct_vf_denied';return Number.isFinite(p[f])?interp(CS.pct_vf_denied,p[f]):'#f0f0f0';}\n"
    "  if(layer==='pct_hoh'){const f=LAYER_FIELD[layer]||'pct_hoh';return Number.isFinite(p[f])?interp(CS.pct_hoh,p[f]):'#f0f0f0';}\n"
    "  if(layer==='pct_vet'){const f=LAYER_FIELD[layer]||'pct_vet';return Number.isFinite(p[f])?interp(CS.pct_vet,p[f]):'#f0f0f0';}\n"
    "  if(layer==='owner_turnover'){const f=LAYER_FIELD[layer]||'owner_turnover';return Number.isFinite(p[f])?interp(CS.owner_turnover,p[f]):'#f0f0f0';}\n"
    "  if(layer==='hoh_churn')return Number.isFinite(p.hoh_churn)?interp(CS.hoh_churn,p.hoh_churn):'#f0f0f0';\n"
)


# ── Patch 2: hiNbhd / rhNbhd highlight hidden features ────────────────────────
P2_OLD = (
    "function hiNbhd(e){if(pinnedNbhd)return;"
    "e.target.setStyle({weight:2,color:'#fff',fillOpacity:Math.min(FILL_OPAC+0.1,0.95)});"
    "e.target.bringToFront();info.update(e.target.feature.properties);"
    "const f=LAYER_FIELD[NBL];if(f)markHistogram(e.target.feature.properties[f]);}\n"
    "function rhNbhd(e){if(pinnedNbhd)return;"
    "nbhdLayer&&nbhdLayer.resetStyle(e.target);info.update();markHistogram(null);}\n"
)
P2_NEW = (
    "function hiNbhd(e){if(pinnedNbhd)return;"
    "if(featureHidden(e.target.feature.properties))return;"
    "e.target.setStyle({weight:2,color:'#fff',fillOpacity:Math.min(FILL_OPAC+0.1,0.95)});"
    "e.target.bringToFront();info.update(e.target.feature.properties);"
    "const f=LAYER_FIELD[NBL];if(f)markHistogram(e.target.feature.properties[f]);}\n"
    "function rhNbhd(e){if(pinnedNbhd)return;"
    "if(featureHidden(e.target.feature.properties))return;"
    "nbhdLayer&&nbhdLayer.resetStyle(e.target);info.update();markHistogram(null);}\n"
)


# ── Patch 3: Getis-Ord Gi* partial-neighbor denominator ───────────────────────
P3_OLD = (
    "    if(stdev<=0)continue;\n"
    "    const scale=stdev*Math.sqrt((K*(N-K))/(N-1));\n"
    "    for(let i=0;i<N;i++){\n"
    "      if(!knn[i]){feats[i].properties['gi_'+field]=null;continue;}\n"
    "      let localSum=0,localCnt=0;\n"
    "      for(const j of knn[i]){const v=vals[j];if(v!=null&&Number.isFinite(v)){localSum+=v;localCnt++;}}\n"
    "      if(localCnt<K/2){feats[i].properties['gi_'+field]=null;continue;}\n"
    "      feats[i].properties['gi_'+field]=(localSum-K*mean)/scale;\n"
    "    }\n"
)
P3_NEW = (
    "    if(stdev<=0)continue;\n"
    "    for(let i=0;i<N;i++){\n"
    "      if(!knn[i]){feats[i].properties['gi_'+field]=null;continue;}\n"
    "      let localSum=0,localCnt=0;\n"
    "      for(const j of knn[i]){const v=vals[j];if(v!=null&&Number.isFinite(v)){localSum+=v;localCnt++;}}\n"
    "      if(localCnt<K/2){feats[i].properties['gi_'+field]=null;continue;}\n"
    "      // Use the actual neighbor count in both the centering term and\n"
    "      // the denominator — otherwise partial coverage inflates z-scores.\n"
    "      const localScale=stdev*Math.sqrt((localCnt*(N-localCnt))/(N-1));\n"
    "      feats[i].properties['gi_'+field]=localScale>0?(localSum-localCnt*mean)/localScale:null;\n"
    "    }\n"
)


#  Patch 4: extend propYrFields so the year selector flips the new per-year
#  fields written by build_data.py. Without this the dropdown silently has
#  no effect on hoh_churn / outreach_need / the two Gi* cluster layers.
P4_OLD = (
    "const propYrFields={avg_appraised:'avg_appraised',median_yrbuilt:'median_yrbuilt',"
    "valfreeze:'pct_val_freeze',pct_vf_denied:'pct_vf_denied',pct_hoh:'pct_hoh',"
    "pct_vet:'pct_vet',owner_turnover:'owner_turnover',hoh_gap:'hoh_gap',"
    "vet_gap:'vet_gap',vf_gap:'vf_gap'};"
)
P4_NEW = (
    "const propYrFields={avg_appraised:'avg_appraised',median_yrbuilt:'median_yrbuilt',"
    "valfreeze:'pct_val_freeze',pct_vf_denied:'pct_vf_denied',pct_hoh:'pct_hoh',"
    "pct_vet:'pct_vet',owner_turnover:'owner_turnover',hoh_gap:'hoh_gap',"
    "vet_gap:'vet_gap',vf_gap:'vf_gap',hoh_churn:'hoh_churn',"
    "outreach_need:'outreach_need',gi_outreach_need:'gi_outreach_need',"
    "gi_pct_vf_denied:'gi_pct_vf_denied'};"
)


PATCHES = [
    ("getNbhdColor: missing-as-zero", P1_OLD, P1_NEW),
    ("hiNbhd/rhNbhd: skip hidden", P2_OLD, P2_NEW),
    ("Gi*: partial-neighbor scale", P3_OLD, P3_NEW),
    ("propYrFields: new per-year layers", P4_OLD, P4_NEW),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Path to the plaintext map body HTML (e.g. index_body.html)")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip writing a .bak copy before modifying the file.")
    args = ap.parse_args()

    src = Path(args.path)
    if not src.exists():
        sys.exit(f"File not found: {src}")

    text = src.read_text(encoding="utf-8")
    original = text
    applied, already, missing = [], [], []

    for name, old, new in PATCHES:
        if new in text and old not in text:
            already.append(name)
            continue
        if old not in text:
            missing.append(name)
            continue
        if text.count(old) > 1:
            sys.exit(f"Patch '{name}' matches >1 location; aborting. "
                     "Inspect the file manually.")
        text = text.replace(old, new, 1)
        applied.append(name)

    if not applied and not missing:
        print("All patches were already applied. No changes made.")
        return

    if missing:
        print("WARNING: could not locate the original text for:")
        for name in missing:
            print(f"  - {name}")
        print("The body has likely been edited since these patches were written. "
              "Inspect manually; no changes were committed to disk.")
        sys.exit(1)

    if text == original:
        print("No changes would be made; exiting.")
        return

    if not args.no_backup:
        backup = src.with_suffix(src.suffix + ".bak")
        shutil.copy2(src, backup)
        print(f"Backup: {backup}")

    src.write_text(text, encoding="utf-8")
    print(f"Patched {src}:")
    for name in applied:
        print(f"  applied: {name}")
    for name in already:
        print(f"  skipped (already applied): {name}")
    print()
    print("Next step — regenerate the ciphertext from the patched plaintext:")
    print(f'  python encrypt_data.py --password "YOUR_PWD" --body {src} --out .')


if __name__ == "__main__":
    main()
