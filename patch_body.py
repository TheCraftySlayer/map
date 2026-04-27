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


#  Patch 5: extensions bundle — permalinks, split-screen compare, worklist CSV.
#
#  This is a single injection at end-of-body so it doesn't depend on internal
#  variable layout we can't see from the encrypted side. The injected code:
#
#   - reads/writes ?layer=…&year=…&q=… on the URL so a tract or layer/year
#     can be linked. On load it dispatches synthetic 'change' events on the
#     matching radio inputs and year selector.
#   - adds a fixed-position "Copy link" / "Compare ▤" / "Worklist CSV ⇩"
#     control panel. "Compare" opens the same URL in a new window with the
#     current hash state preserved — no shared-memory split, but cheap and
#     works on the password-gated deploy.
#   - "Worklist CSV" iterates Leaflet's nbhdLayer.eachLayer, filters to
#     features whose current-layer field is non-null AND not hidden, and
#     downloads a CSV with a stable column set.
#
#  Each block is wrapped in try/catch so the page still renders if a piece
#  of the harness has been renamed since this patch was written. The marker
#  comment '/*MAP_EXT_V1*/' is what patch_body.py uses for idempotency.
P5_OLD = "</body></html>\n"
P5_NEW = """<script>/*MAP_EXT_V1*/
(function(){
try{
  // ── Permalinks ────────────────────────────────────────────────────────────
  // Read ?layer=…&year=…&q=… on load and re-apply by dispatching change
  // events. Hooks into existing radios by VALUE, not by selector — so the
  // patch survives most CSS-class renames.
  function applyState(){
    var p=new URLSearchParams(window.location.hash.replace(/^#/,''));
    var l=p.get('layer'); var y=p.get('year'); var q=p.get('q');
    if(l){
      var radio=document.querySelector('input[type=radio][value="'+l+'"]');
      if(radio){radio.checked=true;radio.dispatchEvent(new Event('change',{bubbles:true}));}
    }
    if(y){
      var sel=document.querySelector('select[name="year"], select#year, select.year-select');
      if(sel){sel.value=y;sel.dispatchEvent(new Event('change',{bubbles:true}));}
    }
    if(q){var box=document.querySelector('input[type=search], input[name="q"]');
          if(box){box.value=q;box.dispatchEvent(new Event('input',{bubbles:true}));}}
  }
  function snapshotState(){
    var l=(document.querySelector('input[type=radio]:checked')||{}).value||'';
    var sel=document.querySelector('select[name="year"], select#year, select.year-select');
    var y=sel?sel.value:'';
    var box=document.querySelector('input[type=search], input[name="q"]');
    var q=box?box.value:'';
    var p=new URLSearchParams();
    if(l)p.set('layer',l); if(y)p.set('year',y); if(q)p.set('q',q);
    return p.toString();
  }
  function copyPermalink(){
    var s=snapshotState();
    var url=window.location.origin+window.location.pathname+(s?'#'+s:'');
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(url).then(function(){flash('Link copied');},
                                              function(){window.prompt('Copy link:',url);});
    }else{window.prompt('Copy link:',url);}
  }

  // ── Split-screen compare ──────────────────────────────────────────────────
  // Open a second window of the same URL with the current hash. The viewer
  // arranges them side by side. No shared state — both windows are fully
  // independent (they each prompted for the password).
  function openCompare(){
    var s=snapshotState();
    var url=window.location.origin+window.location.pathname+(s?'#'+s:'');
    window.open(url,'_blank','width=900,height=900,noopener');
  }

  // ── Worklist CSV ──────────────────────────────────────────────────────────
  // Iterates nbhdLayer (Leaflet) and exports a CSV of every visible feature.
  // Falls back to all features if featureHidden() isn't defined yet.
  function downloadWorklist(){
    var rows=[]; var headers=['nbhd','parcels','outreach_need','hoh_gap','vf_gap',
        'pct_hoh','pct_vet','pct_val_freeze','dpi','low_confidence','low_confidence_reason'];
    var hidden=window.featureHidden||function(){return false;};
    var seen=0; var emitted=0;
    try{
      if(typeof nbhdLayer!=='undefined'&&nbhdLayer&&nbhdLayer.eachLayer){
        nbhdLayer.eachLayer(function(lyr){
          var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p)return; seen++;
          if(hidden(p))return;
          var r=headers.map(function(h){var v=p[h];
            if(v==null)return ''; v=String(v);
            if(/[",\\n]/.test(v))v='"'+v.replace(/"/g,'""')+'"';
            return v;});
          rows.push(r.join(',')); emitted++;
        });
      }
    }catch(e){console.warn('worklist export:',e);}
    if(!rows.length){flash('No visible nbhds (seen '+seen+'). Check filters.');return;}
    var csv=headers.join(',')+'\\n'+rows.join('\\n')+'\\n';
    var blob=new Blob([csv],{type:'text/csv;charset=utf-8'});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    var ts=new Date().toISOString().slice(0,10);
    a.download='outreach-worklist-'+ts+'.csv';
    document.body.appendChild(a); a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},0);
    flash('Worklist: '+emitted+' nbhds');
  }

  // ── UI panel + flash toast ────────────────────────────────────────────────
  var panel=document.createElement('div');
  panel.style.cssText='position:fixed;right:10px;bottom:10px;z-index:9999;'+
    'display:flex;gap:6px;font:12px system-ui,-apple-system,sans-serif;';
  function btn(label,fn){
    var b=document.createElement('button'); b.type='button'; b.textContent=label;
    b.style.cssText='padding:6px 10px;border:1px solid #ccc;border-radius:4px;'+
      'background:#fff;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.08);';
    b.addEventListener('click',fn); return b;
  }
  panel.appendChild(btn('Copy link',copyPermalink));
  panel.appendChild(btn('Compare ▤',openCompare));
  panel.appendChild(btn('Worklist CSV ⇩',downloadWorklist));
  var toast=document.createElement('div');
  toast.style.cssText='position:fixed;left:50%;top:18px;transform:translateX(-50%);'+
    'z-index:10000;background:#222;color:#fff;padding:6px 12px;border-radius:4px;'+
    'font:12px system-ui;display:none;';
  function flash(msg){toast.textContent=msg;toast.style.display='block';
    setTimeout(function(){toast.style.display='none';},1800);}
  document.body.appendChild(panel);
  document.body.appendChild(toast);

  // Apply hash state once the body has had a chance to wire up its listeners.
  if(window.location.hash){setTimeout(applyState,400);}
}catch(err){console.warn('MAP_EXT_V1 init failed:',err);}
})();
</script>
</body></html>
"""


#  Patch 6: PDF export for the current map view.
#
#  Adds a "PDF" button that screenshots the visible map + sidebar via
#  html2canvas and packages it into a single-page PDF via jsPDF, both
#  loaded from a CDN with SRI hashes. Anchored on the MAP_EXT_V1 marker
#  so it only applies after Patch 5 has been installed (avoids two
#  control-panel injections).
#
#  Design choices:
#    - SRI hashes pin specific upstream releases. Changing the version
#      requires updating both the URL and the integrity attribute.
#    - The button only appears once html2canvas + jsPDF are both ready
#      so a slow CDN doesn't half-render the panel.
#    - Output is single-page Letter landscape, fitted to the viewport.
P6_OLD = "/*MAP_EXT_V1*/"
P6_NEW = """/*MAP_EXT_V1*//*PDF_EXPORT_V1*/
;(function(){
try{
  function load(src,integrity){
    return new Promise(function(res,rej){
      var s=document.createElement('script');
      s.src=src; s.crossOrigin='anonymous'; s.referrerPolicy='no-referrer';
      if(integrity)s.integrity=integrity;
      s.onload=res; s.onerror=function(){rej(new Error('load failed: '+src));};
      document.head.appendChild(s);
    });
  }
  // Pinned versions; SRI hashes are upstream-published. Bump both at once.
  var H2C='https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
  var H2C_SRI='sha512-BNaRQnYJYiPSqHHDb58B0yaPfCu+Wgds8Gp/gU33kqBtgNS4tSPHuGibyoeqMV/TJlSKda6FXzoEyYGjTe+vXA==';
  var JSPDF='https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js';
  var JSPDF_SRI='sha512-hMd5JNGqHNgz0mKK0NpjNXn6mXM2dr1QN6WvI9PEWh/0CQjkx7uDvphvuaNeomka4VXn/LgFp2HOCxsmkNBAVQ==';
  Promise.all([load(H2C,H2C_SRI),load(JSPDF,JSPDF_SRI)]).then(function(){
    var panel=document.querySelector('div[data-mapext]');
    if(!panel){
      // Tag the existing extension panel so we can find it.
      var btns=document.querySelectorAll('button');
      for(var i=0;i<btns.length;i++){
        if(btns[i].textContent==='Copy link'){panel=btns[i].parentElement;break;}
      }
      if(panel)panel.setAttribute('data-mapext','1');
    }
    if(!panel)return;
    var b=document.createElement('button'); b.type='button'; b.textContent='PDF ⬇';
    b.style.cssText='padding:6px 10px;border:1px solid #ccc;border-radius:4px;'+
      'background:#fff;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.08);'+
      'font:12px system-ui,-apple-system,sans-serif;';
    b.addEventListener('click',function(){
      var target=document.querySelector('#map')||document.body;
      window.html2canvas(target,{useCORS:true,backgroundColor:'#fff',scale:2})
        .then(function(canvas){
          var img=canvas.toDataURL('image/png');
          var jsPDF=window.jspdf&&window.jspdf.jsPDF;
          if(!jsPDF){alert('jsPDF unavailable');return;}
          var pdf=new jsPDF({orientation:'landscape',unit:'pt',format:'letter'});
          var pw=pdf.internal.pageSize.getWidth();
          var ph=pdf.internal.pageSize.getHeight();
          var iw=canvas.width, ih=canvas.height;
          var ratio=Math.min(pw/iw,ph/ih);
          pdf.addImage(img,'PNG',(pw-iw*ratio)/2,(ph-ih*ratio)/2,iw*ratio,ih*ratio);
          var ts=new Date().toISOString().slice(0,10);
          pdf.save('bernco-map-'+ts+'.pdf');
        }).catch(function(e){console.warn('pdf export:',e);});
    });
    panel.appendChild(b);
  }).catch(function(e){console.warn('PDF deps load failed:',e);});
}catch(err){console.warn('PDF_EXPORT_V1 init failed:',err);}
})();"""


PATCHES = [
    ("getNbhdColor: missing-as-zero", P1_OLD, P1_NEW),
    ("hiNbhd/rhNbhd: skip hidden", P2_OLD, P2_NEW),
    ("Gi*: partial-neighbor scale", P3_OLD, P3_NEW),
    ("propYrFields: new per-year layers", P4_OLD, P4_NEW),
    ("MAP_EXT_V1: permalinks + compare + worklist CSV", P5_OLD, P5_NEW),
    ("PDF_EXPORT_V1: html2canvas + jsPDF export button", P6_OLD, P6_NEW),
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
