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


#  Patch 7: INSIGHTS_V1 — analytical panels that explain a tract's score.
#
#  Three reader-facing features, all client-side (no new data fetch):
#    - Auto-narration: a sentence on hover/click summarizing the tract's
#      percentile rank for the current layer + a short list of "most-similar
#      tracts" (Euclidean distance on the loaded ACS demographic vector).
#    - Why-this-color: an expander on the tooltip that breaks composite
#      scores (outreach_need, dpi_*) into their input components with the
#      contributing values + soft "weight" estimate.
#    - Top movers: a collapsible right-side widget listing the 10 tracts
#      with the largest YoY change in outreach_need (or current layer if
#      it has YoY data). Driven entirely off properties already in core.json.
#
#  All three read from `nbhdLayer.eachLayer(...).feature.properties` so they
#  inherit whatever filters / years the user has set. They share a small
#  percentile cache that invalidates when the layer or year changes.
P7_OLD = "/*PDF_EXPORT_V1*/"
P7_NEW = """/*PDF_EXPORT_V1*//*INSIGHTS_V1*/
;(function(){
try{
  // ── Cache: per-field sorted values for percentile lookup ────────────────
  var pctCache={};
  function rebuildCache(){
    pctCache={};
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return;
    var byField={};
    nbhdLayer.eachLayer(function(lyr){
      var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p)return;
      Object.keys(p).forEach(function(k){
        var v=p[k]; if(typeof v!=='number'||!isFinite(v))return;
        (byField[k]=byField[k]||[]).push(v);
      });
    });
    Object.keys(byField).forEach(function(k){byField[k].sort(function(a,b){return a-b;});});
    pctCache=byField;
  }
  function pctRank(field,v){
    var arr=pctCache[field]; if(!arr||!arr.length||v==null||!isFinite(v))return null;
    // Binary search for first index >= v.
    var lo=0,hi=arr.length;
    while(lo<hi){var mid=(lo+hi)>>1; if(arr[mid]<v)lo=mid+1; else hi=mid;}
    return Math.round(100*lo/arr.length);
  }
  // Demographic vector for similarity. Uses the keys already shipped on
  // each tract's properties — silently skips any that aren't present.
  var DEMO_KEYS=['poverty_rate','median_age','spanish_at_home','elderly_alone',
                 'pct_65plus','tract_pop','zip_poverty_rate','val_change_pct'];
  function similarTracts(target,n){
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return [];
    // Per-field stdev for z-score normalization so big-magnitude fields
    // don't dominate the distance.
    var stats={};
    DEMO_KEYS.forEach(function(k){
      var arr=pctCache[k]; if(!arr||arr.length<2)return;
      var m=arr.reduce(function(s,v){return s+v;},0)/arr.length;
      var ss=arr.reduce(function(s,v){return s+(v-m)*(v-m);},0)/(arr.length-1);
      stats[k]={mean:m,sd:Math.sqrt(ss)||1};
    });
    var ranked=[];
    nbhdLayer.eachLayer(function(lyr){
      var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p||p===target)return;
      var d=0,used=0;
      DEMO_KEYS.forEach(function(k){
        var s=stats[k]; if(!s)return;
        var a=target[k],b=p[k];
        if(a==null||b==null||!isFinite(a)||!isFinite(b))return;
        d+=Math.pow((a-b)/s.sd,2); used++;
      });
      if(used>=3){ranked.push({nbhd:p.nbhd,d:Math.sqrt(d/used)});}
    });
    ranked.sort(function(a,b){return a.d-b.d;});
    return ranked.slice(0,n||3);
  }
  // Composite-score breakdown. The build_data scoring functions document
  // how outreach_need composes from gap fields; we surface the inputs the
  // viewer can verify against the score.
  function explainScore(p){
    var rows=[];
    function row(label,val){if(val!=null&&isFinite(val))rows.push([label,val]);}
    var layer=(document.querySelector('input[type=radio]:checked')||{}).value||'';
    if(layer==='outreach_need'||layer==='outreach_need_slope'){
      row('hoh_gap (HOH residual)',p.hoh_gap);
      row('vf_gap (VF residual)',p.vf_gap);
      row('vet_gap (VET residual)',p.vet_gap);
      row('outreach_need_gap_boost',p.outreach_need_gap_boost);
      row('outreach_need (final)',p.outreach_need);
    }else if(layer.indexOf('dpi')===0||layer==='dpi'){
      row('owner_turnover',p.owner_turnover);
      row('hoh_churn',p.hoh_churn);
      row('val_change_pct',p.val_change_pct);
      row('tract_poverty_rate',p.tract_poverty_rate);
      row('zip_poverty_rate',p.zip_poverty_rate);
      row('dpi (final)',p.dpi);
    }else if(layer==='hoh_uptake'||layer==='vf_uptake'||layer==='vet_uptake'){
      row('actual share',p[layer.replace('_uptake','').replace('hoh','pct_hoh').replace('vf','pct_val_freeze').replace('vet','pct_vet')]);
      row('zip_poverty_rate (predictor)',p.zip_poverty_rate);
      row('uptake ratio',p[layer]);
    }
    return rows;
  }

  // ── Tooltip enrichment ──────────────────────────────────────────────────
  function buildTooltip(p){
    var layer=(document.querySelector('input[type=radio]:checked')||{}).value||'';
    var field=(window.LAYER_FIELD&&window.LAYER_FIELD[layer])||layer;
    var v=p[field];
    var pct=pctRank(field,v);
    var lines=[];
    if(pct!=null){
      lines.push('<b>'+field+'</b>: '+(typeof v==='number'?v.toFixed(3):v)+
                 ' &middot; <b>'+pct+'th</b> percentile');
    }
    if(p.low_confidence){
      lines.push('<span style="color:#a05">Low confidence: '+
                 (p.low_confidence_reason||'')+'</span>');
    }
    var sims=similarTracts(p,3);
    if(sims.length){
      lines.push('Most similar: '+sims.map(function(s){return s.nbhd;}).join(', '));
    }
    var explain=explainScore(p);
    if(explain.length){
      lines.push('<details><summary>Why this color?</summary>'+
        '<table style="font:11px monospace;border-collapse:collapse">'+
        explain.map(function(r){
          return '<tr><td style="padding:1px 6px">'+r[0]+
                 '</td><td style="padding:1px 6px;text-align:right">'+
                 (typeof r[1]==='number'?r[1].toFixed(4):r[1])+'</td></tr>';
        }).join('')+'</table></details>');
    }
    return lines.join('<br>');
  }

  // Hook into the existing `info` control's update if it exists, so the
  // narrative shows in whatever sidebar the body already provides. Falls
  // back to a floating bubble.
  var bubble=null;
  function showBubble(html){
    if(!bubble){
      bubble=document.createElement('div');
      bubble.style.cssText='position:fixed;left:10px;top:10px;z-index:9998;'+
        'background:#fff;border:1px solid #ccc;border-radius:4px;padding:8px 12px;'+
        'max-width:340px;font:12px system-ui;box-shadow:0 1px 4px rgba(0,0,0,.12);'+
        'display:none;';
      document.body.appendChild(bubble);
    }
    bubble.innerHTML=html; bubble.style.display=html?'block':'none';
  }

  function attachHandlers(){
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return false;
    nbhdLayer.eachLayer(function(lyr){
      lyr.on('mouseover',function(e){
        var p=e.target.feature&&e.target.feature.properties; if(!p)return;
        if(window.featureHidden&&window.featureHidden(p))return;
        showBubble(buildTooltip(p));
      });
      lyr.on('mouseout',function(){showBubble('');});
    });
    return true;
  }

  // ── Top movers widget ──────────────────────────────────────────────────
  function topMovers(n){
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return [];
    var movers=[];
    nbhdLayer.eachLayer(function(lyr){
      var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p)return;
      // Pick the latest two outreach_need_YY values present.
      var years=Object.keys(p).filter(function(k){return /^outreach_need_\\d+$/.test(k);})
        .map(function(k){return parseInt(k.split('_').pop(),10);}).sort(function(a,b){return a-b;});
      if(years.length<2)return;
      var cur=p['outreach_need_'+String(years[years.length-1]).padStart(2,'0')];
      var prv=p['outreach_need_'+String(years[years.length-2]).padStart(2,'0')];
      if(cur==null||prv==null||!isFinite(cur)||!isFinite(prv))return;
      movers.push({nbhd:p.nbhd,delta:cur-prv,cur:cur,prv:prv});
    });
    movers.sort(function(a,b){return Math.abs(b.delta)-Math.abs(a.delta);});
    return movers.slice(0,n||10);
  }
  var moversPanel=null;
  function renderMovers(){
    if(!moversPanel){
      moversPanel=document.createElement('div');
      moversPanel.style.cssText='position:fixed;right:10px;top:48px;z-index:9998;'+
        'background:#fff;border:1px solid #ccc;border-radius:4px;padding:8px 12px;'+
        'max-width:300px;font:12px system-ui;box-shadow:0 1px 4px rgba(0,0,0,.12);'+
        'display:none;';
      document.body.appendChild(moversPanel);
    }
    var ms=topMovers(10);
    if(!ms.length){moversPanel.innerHTML='<i>no YoY data</i>';moversPanel.style.display='block';return;}
    moversPanel.innerHTML='<b>Top movers (outreach_need)</b>'+
      '<table style="font:11px monospace;border-collapse:collapse;margin-top:4px">'+
      '<tr><th style="text-align:left">nbhd</th><th>Δ</th><th>now</th></tr>'+
      ms.map(function(m){
        var color=m.delta>0?'#a30':'#063';
        return '<tr><td style="padding:1px 6px">'+m.nbhd+'</td>'+
          '<td style="padding:1px 6px;text-align:right;color:'+color+'">'+
          (m.delta>0?'+':'')+m.delta.toFixed(3)+'</td>'+
          '<td style="padding:1px 6px;text-align:right">'+m.cur.toFixed(3)+'</td></tr>';
      }).join('')+'</table>';
    moversPanel.style.display='block';
  }

  // ── UI: add Movers + Insights toggle to the extension panel ──────────
  function ready(){
    rebuildCache();
    if(!attachHandlers())return false;
    var btns=document.querySelectorAll('button');
    var panel=null;
    for(var i=0;i<btns.length;i++)if(btns[i].textContent==='Copy link'){panel=btns[i].parentElement;break;}
    if(!panel)return true;
    function btn(label,fn){
      var b=document.createElement('button'); b.type='button'; b.textContent=label;
      b.style.cssText='padding:6px 10px;border:1px solid #ccc;border-radius:4px;'+
        'background:#fff;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.08);'+
        'font:12px system-ui,-apple-system,sans-serif;';
      b.addEventListener('click',fn); return b;
    }
    panel.appendChild(btn('Movers ▢',function(){
      if(moversPanel&&moversPanel.style.display==='block'){moversPanel.style.display='none';}
      else{renderMovers();}
    }));
    // Rebuild the percentile cache when the layer changes — the active
    // field changes, so cached arrays go stale.
    document.querySelectorAll('input[type=radio]').forEach(function(r){
      r.addEventListener('change',function(){setTimeout(rebuildCache,150);});
    });
    return true;
  }

  // The body wires its layers async; retry briefly so we don't lose the race.
  var tries=0; var ivl=setInterval(function(){
    if(ready()||++tries>20){clearInterval(ivl);}
  },500);
}catch(err){console.warn('INSIGHTS_V1 init failed:',err);}
})();"""


#  Patch 8: TOOLS_V1 — spatial tools that turn the map into a workbench.
#
#  Four interactive tools, all using raw Leaflet primitives (no plugin
#  dependency) so we don't have to pin third-party SRI hashes:
#
#    - Address search: input box → Nominatim geocoder → pan/zoom + show a
#      pin + open the matching tract's tooltip. Throttled to 1 req/sec
#      per Nominatim's usage policy and adds a Referer-friendly fetch.
#    - Free-draw polygon: click to add vertices, double-click to finish;
#      aggregates the current layer's field across nbhds whose centroid
#      falls inside the polygon. Drives the "district-on-the-fly" use case.
#    - Buffer rings: toggle that draws Euclidean km radii around the four
#      Assessor community centers. (Drive-time isochrones live server-side
#      in fetch_drive_times_osrm; we approximate with Euclidean rings here
#      because the body doesn't have OSRM.)
#    - Measure: click to drop vertices for a polyline; popup shows total
#      distance (km + mi) and, if you double-click to close the path,
#      the polygon's area.
P8_OLD = "/*INSIGHTS_V1*/"
P8_NEW = """/*INSIGHTS_V1*//*TOOLS_V1*/
;(function(){
try{
  // map handle: prior patches reference Leaflet via globals; we look for
  // a Leaflet map under any common name.
  function findMap(){
    var cands=['map','mymap','leafletMap','LMAP'];
    for(var i=0;i<cands.length;i++){
      var m=window[cands[i]];
      if(m&&typeof m.getBounds==='function'&&typeof m.addLayer==='function')return m;
    }
    // Fallback: scan all globals for an object that looks like a Leaflet map.
    for(var k in window){
      try{var v=window[k];
        if(v&&typeof v==='object'&&typeof v.getBounds==='function'&&
           typeof v.addLayer==='function'&&typeof v.eachLayer==='function')return v;
      }catch(_){}
    }
    return null;
  }

  // Bernalillo County Assessor community centers (mirrors CC_LOCATIONS in
  // buildlib/io_utils.py — kept in sync by code review).
  var CC=[
    ['Main Office',         35.0853,-106.6498],
    ['South Valley',        35.0010,-106.6730],
    ['North Valley',        35.1660,-106.6510],
    ['East Mountain',       35.0710,-106.4400],
  ];

  // ── Address search (Nominatim) ──────────────────────────────────────────
  var lastQueryAt=0;
  function geocode(q){
    var dt=Date.now()-lastQueryAt;
    var wait=dt<1000?(1000-dt):0;  // honor 1 req/sec
    return new Promise(function(resolve,reject){
      setTimeout(function(){
        lastQueryAt=Date.now();
        // Constrain to NM + Bernalillo to cut down on misses.
        var url='https://nominatim.openstreetmap.org/search?format=json&limit=1'+
          '&countrycodes=us&state=New+Mexico&county=Bernalillo&q='+encodeURIComponent(q);
        fetch(url,{headers:{'Accept':'application/json'}}).then(function(r){return r.json();})
          .then(function(j){
            if(!j||!j.length){reject(new Error('no match'));return;}
            resolve({lat:parseFloat(j[0].lat),lng:parseFloat(j[0].lon),name:j[0].display_name});
          }).catch(reject);
      },wait);
    });
  }
  // Point-in-polygon using Leaflet's contains() via L.polygon.getBounds —
  // bounds is a fast pre-filter, then exact PIP via ray casting on the
  // geometry coords. Returns the matching layer or null.
  function findContaining(map,lat,lng){
    var hit=null;
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return null;
    nbhdLayer.eachLayer(function(lyr){
      if(hit)return;
      var b=lyr.getBounds&&lyr.getBounds();
      if(!b||!b.contains([lat,lng]))return;
      // Ray cast on the underlying GeoJSON.
      var feat=lyr.feature; if(!feat||!feat.geometry)return;
      if(pointInGeom(lat,lng,feat.geometry))hit=lyr;
    });
    return hit;
  }
  function pointInRing(py,px,ring){
    // ring: [[lng,lat], ...]; py=lat, px=lng (matches GeoJSON lng,lat order).
    var inside=false,n=ring.length,j=n-1;
    for(var i=0;i<n;i++){
      var xi=ring[i][0],yi=ring[i][1],xj=ring[j][0],yj=ring[j][1];
      if(((yi>py)!==(yj>py))&&(px<(xj-xi)*(py-yi)/((yj-yi)||1e-12)+xi))inside=!inside;
      j=i;
    }
    return inside;
  }
  function pointInGeom(lat,lng,g){
    if(g.type==='Polygon'){return pointInRing(lat,lng,g.coordinates[0]);}
    if(g.type==='MultiPolygon'){
      for(var i=0;i<g.coordinates.length;i++){
        if(pointInRing(lat,lng,g.coordinates[i][0]))return true;
      }
    }
    return false;
  }
  var searchPin=null;
  function doSearch(map,q){
    geocode(q).then(function(r){
      map.setView([r.lat,r.lng],14);
      if(searchPin)map.removeLayer(searchPin);
      searchPin=L.circleMarker([r.lat,r.lng],{radius:8,color:'#185FA5',
        weight:3,fillColor:'#fff',fillOpacity:1}).addTo(map)
        .bindPopup(r.name).openPopup();
      var hit=findContaining(map,r.lat,r.lng);
      if(hit&&hit.fire)hit.fire('mouseover');
    }).catch(function(e){
      flash2('Address not found ('+(e.message||e)+')');
    });
  }

  // ── Free-draw polygon aggregator ────────────────────────────────────────
  var drawState=null;
  function startDraw(map){
    cancelDraw(map);
    var verts=[];
    var poly=L.polyline(verts,{color:'#a30',weight:2,dashArray:'4,3'}).addTo(map);
    function onClick(e){verts.push([e.latlng.lat,e.latlng.lng]);poly.setLatLngs(verts);}
    function onDbl(e){
      verts.push([e.latlng.lat,e.latlng.lng]);
      map.off('click',onClick); map.off('dblclick',onDbl);
      map.removeLayer(poly);
      var fill=L.polygon(verts,{color:'#a30',fillColor:'#a30',fillOpacity:0.15,weight:2}).addTo(map);
      var stats=aggregateInside(verts);
      fill.bindPopup(stats.html).openPopup();
      drawState={poly:fill};
    }
    map.on('click',onClick); map.on('dblclick',onDbl);
    flash2('Click to add vertices, double-click to finish.');
  }
  function cancelDraw(map){
    if(drawState&&drawState.poly){map.removeLayer(drawState.poly);drawState=null;}
  }
  function aggregateInside(verts){
    var ring=verts.map(function(p){return [p[1],p[0]];}); // to lng,lat
    var rows=[]; var values=[]; var hidden=window.featureHidden||function(){return false;};
    var layer=(document.querySelector('input[type=radio]:checked')||{}).value||'';
    var field=(window.LAYER_FIELD&&window.LAYER_FIELD[layer])||layer;
    if(typeof nbhdLayer!=='undefined'&&nbhdLayer&&nbhdLayer.eachLayer){
      nbhdLayer.eachLayer(function(lyr){
        var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p||hidden(p))return;
        var c=lyr.getBounds&&lyr.getBounds().getCenter(); if(!c)return;
        if(!pointInRing(c.lat,c.lng,ring))return;
        rows.push(p);
        var v=p[field]; if(typeof v==='number'&&isFinite(v))values.push(v);
      });
    }
    var sum=values.reduce(function(s,v){return s+v;},0);
    var mean=values.length?sum/values.length:null;
    var med=null;
    if(values.length){
      var s=values.slice().sort(function(a,b){return a-b;});
      med=s.length%2?s[(s.length-1)/2]:(s[s.length/2-1]+s[s.length/2])/2;
    }
    var html='<b>'+rows.length+' nbhds in selection</b><br>'+
      'field: '+field+'<br>'+
      'count(numeric): '+values.length+'<br>'+
      (mean!=null?'mean: '+mean.toFixed(4)+'<br>':'')+
      (med!=null?'median: '+med.toFixed(4)+'<br>':'')+
      'sum: '+sum.toFixed(4);
    return {html:html,rows:rows,mean:mean,median:med,sum:sum};
  }

  // ── Buffer rings around community centers ──────────────────────────────
  var ringLayer=null;
  function toggleRings(map){
    if(ringLayer){map.removeLayer(ringLayer);ringLayer=null;return;}
    ringLayer=L.layerGroup();
    var radii=[1.6,3.2,8];  // km ≈ 1mi, 2mi, 5mi
    CC.forEach(function(cc){
      L.marker([cc[1],cc[2]]).addTo(ringLayer).bindTooltip(cc[0]);
      radii.forEach(function(r,i){
        L.circle([cc[1],cc[2]],{radius:r*1000,color:'#185FA5',
          weight:1,fillOpacity:0.04+0.03*(2-i)}).addTo(ringLayer);
      });
    });
    ringLayer.addTo(map);
  }

  // ── Measure (polyline distance + optional polygon area) ─────────────────
  var measureState=null;
  function startMeasure(map){
    cancelMeasure(map);
    var verts=[];
    var line=L.polyline(verts,{color:'#063',weight:2}).addTo(map);
    function dist(a,b){
      // Haversine; lat/lng in degrees.
      var R=6371,toRad=Math.PI/180;
      var dLat=(b[0]-a[0])*toRad,dLng=(b[1]-a[1])*toRad;
      var s=Math.sin(dLat/2)*Math.sin(dLat/2)+
        Math.cos(a[0]*toRad)*Math.cos(b[0]*toRad)*Math.sin(dLng/2)*Math.sin(dLng/2);
      return 2*R*Math.atan2(Math.sqrt(s),Math.sqrt(1-s));
    }
    function pathKm(){var d=0;for(var i=1;i<verts.length;i++)d+=dist(verts[i-1],verts[i]);return d;}
    function shoelaceKm2(){
      if(verts.length<3)return 0;
      // Project to local equirectangular meters then shoelace.
      var lat0=verts[0][0]*Math.PI/180;
      var sx=Math.cos(lat0)*111.32, sy=110.57;
      var a=0;
      for(var i=0;i<verts.length;i++){
        var j=(i+1)%verts.length;
        var xi=verts[i][1]*sx, yi=verts[i][0]*sy;
        var xj=verts[j][1]*sx, yj=verts[j][0]*sy;
        a+=xi*yj-xj*yi;
      }
      return Math.abs(a)/2;
    }
    function update(){
      var km=pathKm();
      var msg='dist: '+km.toFixed(2)+' km / '+(km*0.621371).toFixed(2)+' mi';
      if(verts.length>=3){
        var ar=shoelaceKm2();
        msg+='<br>area (closed): '+ar.toFixed(3)+' km² / '+(ar*0.386102).toFixed(3)+' mi²';
      }
      line.bindTooltip(msg,{permanent:true,direction:'top'}).openTooltip();
    }
    function onClick(e){verts.push([e.latlng.lat,e.latlng.lng]);line.setLatLngs(verts);update();}
    function onDbl(){map.off('click',onClick);map.off('dblclick',onDbl);measureState={line:line};}
    map.on('click',onClick); map.on('dblclick',onDbl);
    flash2('Measure: click to add vertices, double-click to finish.');
  }
  function cancelMeasure(map){
    if(measureState&&measureState.line){map.removeLayer(measureState.line);measureState=null;}
  }

  // ── UI: input box + four toggle buttons ────────────────────────────────
  function flash2(msg){
    var t=document.querySelector('div[data-tools-toast]');
    if(!t){t=document.createElement('div');
      t.setAttribute('data-tools-toast','1');
      t.style.cssText='position:fixed;left:50%;bottom:50px;transform:translateX(-50%);'+
        'z-index:10000;background:#222;color:#fff;padding:6px 12px;border-radius:4px;'+
        'font:12px system-ui;display:none;';
      document.body.appendChild(t);}
    t.textContent=msg; t.style.display='block';
    setTimeout(function(){t.style.display='none';},2200);
  }

  function ready(){
    var map=findMap(); if(!map)return false;
    if(typeof L==='undefined')return false;

    // Search box: top-center floating bar.
    var bar=document.createElement('div');
    bar.style.cssText='position:fixed;left:50%;top:8px;transform:translateX(-50%);'+
      'z-index:9999;display:flex;gap:4px;background:#fff;padding:4px;border:1px solid #ccc;'+
      'border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.12);font:12px system-ui;';
    var input=document.createElement('input');
    input.type='search'; input.placeholder='Search address (Bernalillo County)…';
    input.style.cssText='padding:4px 8px;border:1px solid #ccc;border-radius:3px;width:280px;font:12px system-ui;';
    var go=document.createElement('button'); go.type='button'; go.textContent='Find';
    go.style.cssText='padding:4px 10px;border:1px solid #ccc;border-radius:3px;background:#185FA5;color:#fff;cursor:pointer;font:12px system-ui;';
    function fire(){var q=input.value.trim();if(q)doSearch(map,q);}
    go.addEventListener('click',fire);
    input.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();fire();}});
    bar.appendChild(input); bar.appendChild(go);
    document.body.appendChild(bar);

    // Append the four tool toggles to the existing extension panel.
    var btns=document.querySelectorAll('button'); var panel=null;
    for(var i=0;i<btns.length;i++)if(btns[i].textContent==='Copy link'){panel=btns[i].parentElement;break;}
    if(!panel)return true;
    function tb(label,fn){
      var b=document.createElement('button'); b.type='button'; b.textContent=label;
      b.style.cssText='padding:6px 10px;border:1px solid #ccc;border-radius:4px;'+
        'background:#fff;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.08);'+
        'font:12px system-ui,-apple-system,sans-serif;';
      b.addEventListener('click',fn); return b;
    }
    panel.appendChild(tb('Draw ▱',function(){startDraw(map);}));
    panel.appendChild(tb('Rings ◎',function(){toggleRings(map);}));
    panel.appendChild(tb('Measure ↔',function(){startMeasure(map);}));
    panel.appendChild(tb('Clear ✕',function(){
      cancelDraw(map); cancelMeasure(map);
      if(searchPin){map.removeLayer(searchPin);searchPin=null;}
    }));
    return true;
  }

  var tries=0; var ivl=setInterval(function(){
    if(ready()||++tries>20){clearInterval(ivl);}
  },500);
}catch(err){console.warn('TOOLS_V1 init failed:',err);}
})();"""


#  Patch 9: REPORTS_V1 — multi-page PDF reports + Commission packet template.
#
#  Two new buttons that build paginated PDFs entirely in jsPDF (no
#  html2canvas screenshots — we draw the data with text + lines so the
#  output is selectable / searchable / smaller).
#
#    - Report ⎙: a multi-page county report. Cover, methodology, per-
#      quartile breakdown of outreach_need, and an appendix listing the
#      top movers + low-confidence tracts.
#    - Commission ⎘: the quarterly template — county totals on the cover,
#      then top-10 tables for outreach_need, dpi, and (if present)
#      outreach_dose_ratio. Title page reads "Quarter <Q>, <Y>" auto-
#      derived from today's date.
#
#  jsPDF is already loaded by PDF_EXPORT_V1; we reuse window.jspdf.jsPDF
#  and wait until it shows up before binding the buttons.
P9_OLD = "/*TOOLS_V1*/"
P9_NEW = """/*TOOLS_V1*//*REPORTS_V1*/
;(function(){
try{
  function waitForJsPDF(cb){
    var tries=0; var ivl=setInterval(function(){
      if((window.jspdf&&window.jspdf.jsPDF)||++tries>40){
        clearInterval(ivl);
        if(window.jspdf&&window.jspdf.jsPDF)cb(window.jspdf.jsPDF);
      }
    },250);
  }

  // Pull every nbhd's properties into an array we can sort/slice.
  function collectProps(){
    var out=[]; var hidden=window.featureHidden||function(){return false;};
    if(typeof nbhdLayer==='undefined'||!nbhdLayer||!nbhdLayer.eachLayer)return out;
    nbhdLayer.eachLayer(function(lyr){
      var p=lyr&&lyr.feature&&lyr.feature.properties; if(!p||hidden(p))return;
      out.push(p);
    });
    return out;
  }
  function quantile(values,q){
    if(!values.length)return null;
    var s=values.slice().sort(function(a,b){return a-b;});
    var idx=Math.min(s.length-1,Math.max(0,Math.floor(q*(s.length-1))));
    return s[idx];
  }

  // Tiny declarative table renderer — header + rows, auto-paginates.
  function drawTable(pdf,opts){
    var x=opts.x||40, y=opts.y||60;
    var widths=opts.widths;
    var header=opts.header;
    var rows=opts.rows;
    var rowH=opts.rowH||14;
    var pageH=pdf.internal.pageSize.getHeight();
    pdf.setFont('helvetica','bold'); pdf.setFontSize(10);
    header.forEach(function(h,i){pdf.text(String(h),x+offsetX(widths,i),y);});
    pdf.setFont('helvetica','normal');
    y+=rowH;
    for(var r=0;r<rows.length;r++){
      if(y>pageH-40){pdf.addPage();y=60;
        pdf.setFont('helvetica','bold');
        header.forEach(function(h,i){pdf.text(String(h),x+offsetX(widths,i),y);});
        pdf.setFont('helvetica','normal'); y+=rowH;}
      rows[r].forEach(function(c,i){
        var s=(c==null)?'':(typeof c==='number'?c.toFixed(3):String(c));
        pdf.text(s,x+offsetX(widths,i),y);
      });
      y+=rowH;
    }
    return y;
  }
  function offsetX(widths,i){
    var off=0; for(var k=0;k<i;k++)off+=widths[k];
    return off;
  }

  function coverPage(pdf,title,subtitle){
    var pw=pdf.internal.pageSize.getWidth();
    pdf.setFont('helvetica','bold'); pdf.setFontSize(22);
    pdf.text(title,pw/2,200,{align:'center'});
    pdf.setFont('helvetica','normal'); pdf.setFontSize(12);
    pdf.text(subtitle,pw/2,230,{align:'center'});
    pdf.setFontSize(9);
    pdf.text('Bernalillo County Assessor — Spatial Equity',pw/2,260,{align:'center'});
    var ts=new Date().toISOString().slice(0,10);
    pdf.text('Generated '+ts,pw/2,275,{align:'center'});
  }
  function methodologyPage(pdf){
    pdf.addPage();
    pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
    pdf.text('Methodology',40,60);
    pdf.setFont('helvetica','normal'); pdf.setFontSize(10);
    var lines=[
      'Source: Bernalillo County tax roll joined with U.S. Census ACS 5-year estimates.',
      '',
      'Layers:',
      '  • outreach_need — composite of exemption gaps (hoh, vf, vet) and demographics.',
      '  • dpi (Displacement Pressure Index) — owner_turnover × hoh_churn × poverty.',
      '  • *_uptake — actual exemption share / OLS-predicted share given demographics.',
      '  • Gi* z-scores — Getis-Ord local hot/cold-spot statistic over 8 nearest nbhds.',
      '  • low_confidence — flag for nbhds with parcels<100 or tract_pop<500.',
      '',
      'Caveats:',
      '  • ACS estimates carry margins of error not yet propagated through scoring.',
      '  • Per-year fields named *_YY where YY is the 2-digit tax year.',
      '  • Composite scores collapse to 0 when an input is missing — see "Why this color?"',
      '    in the on-screen tooltip for per-tract input breakdowns.',
    ];
    var y=85;
    lines.forEach(function(l){pdf.text(l,40,y);y+=14;});
  }

  function buildReport(){
    waitForJsPDF(function(jsPDF){
      var props=collectProps();
      if(!props.length){alert('No nbhds loaded.');return;}
      var pdf=new jsPDF({orientation:'portrait',unit:'pt',format:'letter'});
      coverPage(pdf,'County Spatial-Equity Report','Outreach Need · DPI · Exemption Uptake');
      methodologyPage(pdf);

      // Quartile pages by outreach_need.
      var withNeed=props.filter(function(p){return typeof p.outreach_need==='number'&&isFinite(p.outreach_need);});
      withNeed.sort(function(a,b){return b.outreach_need-a.outreach_need;});
      var quarts=[
        ['Top quartile (highest outreach need)',withNeed.slice(0,Math.ceil(withNeed.length/4))],
        ['2nd quartile',withNeed.slice(Math.ceil(withNeed.length/4),Math.ceil(withNeed.length/2))],
        ['3rd quartile',withNeed.slice(Math.ceil(withNeed.length/2),Math.ceil(3*withNeed.length/4))],
        ['Bottom quartile',withNeed.slice(Math.ceil(3*withNeed.length/4))],
      ];
      quarts.forEach(function(qq){
        pdf.addPage();
        pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
        pdf.text(qq[0]+' ('+qq[1].length+' nbhds)',40,60);
        var rows=qq[1].slice(0,40).map(function(p){
          return [p.nbhd,p.outreach_need,p.dpi||p.dpi_23||null,p.hoh_gap,p.vf_gap,
                  p.low_confidence?'Y':''];
        });
        drawTable(pdf,{
          y:80,
          header:['nbhd','outreach_need','dpi','hoh_gap','vf_gap','low_conf'],
          widths:[60,90,70,70,70,60],
          rows:rows,
        });
      });

      // Appendix: top movers + low-confidence list.
      pdf.addPage();
      pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
      pdf.text('Appendix A — Low-confidence neighborhoods',40,60);
      var lc=props.filter(function(p){return p.low_confidence;}).slice(0,80);
      drawTable(pdf,{
        y:80,
        header:['nbhd','reason','parcels','tract_pop'],
        widths:[60,200,80,80],
        rows:lc.map(function(p){return [p.nbhd,p.low_confidence_reason||'',
          p.parcels||'',p.tract_pop||''];}),
      });

      var ts=new Date().toISOString().slice(0,10);
      pdf.save('bernco-county-report-'+ts+'.pdf');
    });
  }

  function buildCommissionPacket(){
    waitForJsPDF(function(jsPDF){
      var props=collectProps();
      if(!props.length){alert('No nbhds loaded.');return;}
      var d=new Date();
      var quarter=Math.floor(d.getMonth()/3)+1;
      var year=d.getFullYear();
      var pdf=new jsPDF({orientation:'portrait',unit:'pt',format:'letter'});
      coverPage(pdf,'Quarterly Commission Packet','Q'+quarter+' '+year);

      // County totals page.
      pdf.addPage();
      pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
      pdf.text('County totals',40,60);
      var pTot=props.reduce(function(s,p){return s+(p.parcels||0);},0);
      var withOn=props.filter(function(p){return typeof p.outreach_need==='number';}).map(function(p){return p.outreach_need;});
      var medOn=quantile(withOn,0.5);
      var p90=quantile(withOn,0.9);
      var lc=props.filter(function(p){return p.low_confidence;}).length;
      pdf.setFont('helvetica','normal'); pdf.setFontSize(11);
      var stats=[
        ['Neighborhoods',String(props.length)],
        ['Parcels (sum)',pTot.toLocaleString()],
        ['Median outreach_need',medOn!=null?medOn.toFixed(3):'n/a'],
        ['90th-pct outreach_need',p90!=null?p90.toFixed(3):'n/a'],
        ['Low-confidence nbhds',String(lc)],
      ];
      var y=90;
      stats.forEach(function(r){pdf.text(r[0]+':',60,y);pdf.text(r[1],260,y);y+=18;});

      // Top 10 outreach_need.
      pdf.addPage();
      pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
      pdf.text('Top 10 — outreach_need',40,60);
      var topOn=props.filter(function(p){return typeof p.outreach_need==='number';})
        .sort(function(a,b){return b.outreach_need-a.outreach_need;}).slice(0,10);
      drawTable(pdf,{y:80,header:['nbhd','outreach_need','hoh_gap','vf_gap','dpi'],
        widths:[60,100,80,80,80],
        rows:topOn.map(function(p){return [p.nbhd,p.outreach_need,p.hoh_gap,p.vf_gap,p.dpi||p.dpi_23];})});

      // Top 10 dpi.
      pdf.addPage();
      pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
      pdf.text('Top 10 — DPI (displacement pressure)',40,60);
      var dpiKey=props.some(function(p){return typeof p.dpi==='number';})?'dpi':'dpi_23';
      var topD=props.filter(function(p){return typeof p[dpiKey]==='number';})
        .sort(function(a,b){return b[dpiKey]-a[dpiKey];}).slice(0,10);
      drawTable(pdf,{y:80,header:['nbhd','dpi','owner_turnover','hoh_churn','val_change_pct'],
        widths:[60,80,90,80,90],
        rows:topD.map(function(p){return [p.nbhd,p[dpiKey],p.owner_turnover,
          p.hoh_churn,p.val_change_pct];})});

      // Outreach dose vs need (if dose merged).
      var withDose=props.filter(function(p){
        return Object.keys(p).some(function(k){return /^outreach_dose_ratio_\\d+$/.test(k);});
      });
      if(withDose.length){
        pdf.addPage();
        pdf.setFont('helvetica','bold'); pdf.setFontSize(14);
        pdf.text('Outreach dose vs. need (most recent year)',40,60);
        var doseRows=withDose.map(function(p){
          var keys=Object.keys(p).filter(function(k){return /^outreach_dose_ratio_\\d+$/.test(k);})
            .sort();
          var k=keys[keys.length-1];
          var yy=k.split('_').pop();
          return [p.nbhd,p[k],p['outreach_need_'+yy]||p.outreach_need,p['outreach_dose_'+yy]];
        }).sort(function(a,b){return (a[1]||0)-(b[1]||0);}).slice(0,15);
        drawTable(pdf,{y:80,header:['nbhd','dose_ratio','need','dose_$'],
          widths:[60,100,80,80],rows:doseRows});
        pdf.setFont('helvetica','italic'); pdf.setFontSize(9);
        pdf.text('Sorted lowest-ratio first (under-served relative to median attention).',40,
          pdf.internal.pageSize.getHeight()-40);
      }

      pdf.save('bernco-commission-Q'+quarter+'-'+year+'.pdf');
    });
  }

  function ready(){
    var btns=document.querySelectorAll('button'); var panel=null;
    for(var i=0;i<btns.length;i++)if(btns[i].textContent==='Copy link'){panel=btns[i].parentElement;break;}
    if(!panel)return false;
    function rb(label,fn){
      var b=document.createElement('button'); b.type='button'; b.textContent=label;
      b.style.cssText='padding:6px 10px;border:1px solid #ccc;border-radius:4px;'+
        'background:#fff;cursor:pointer;box-shadow:0 1px 2px rgba(0,0,0,.08);'+
        'font:12px system-ui,-apple-system,sans-serif;';
      b.addEventListener('click',fn); return b;
    }
    panel.appendChild(rb('Report ⎙',buildReport));
    panel.appendChild(rb('Commission ⎘',buildCommissionPacket));
    return true;
  }
  var tries=0; var ivl=setInterval(function(){
    if(ready()||++tries>20){clearInterval(ivl);}
  },500);
}catch(err){console.warn('REPORTS_V1 init failed:',err);}
})();"""


PATCHES = [
    ("getNbhdColor: missing-as-zero", P1_OLD, P1_NEW),
    ("hiNbhd/rhNbhd: skip hidden", P2_OLD, P2_NEW),
    ("Gi*: partial-neighbor scale", P3_OLD, P3_NEW),
    ("propYrFields: new per-year layers", P4_OLD, P4_NEW),
    # P5: MAP_EXT_V1 wraps existing </body></html>; the marker is the unique
    # /*MAP_EXT_V1*/ comment we inject. Without the marker, the engine would
    # see </body></html> still present after the first apply and double-inject.
    ("MAP_EXT_V1: permalinks + compare + worklist CSV", P5_OLD, P5_NEW, "/*MAP_EXT_V1*/"),
    # P6: PDF export. Same situation — the new content keeps /*MAP_EXT_V1*/
    # as a prefix so the engine would otherwise re-fire.
    ("PDF_EXPORT_V1: html2canvas + jsPDF export button", P6_OLD, P6_NEW, "/*PDF_EXPORT_V1*/"),
    # P7: insights — auto-narration tooltip, why-this-color, top-movers,
    # peer-tract similarity. Anchored on /*PDF_EXPORT_V1*/ so it requires
    # P6 already applied (depends on the extension panel being present).
    ("INSIGHTS_V1: narration + breakdown + movers + similar tracts", P7_OLD, P7_NEW, "/*INSIGHTS_V1*/"),
    # P8: spatial tools — address search, free-draw aggregator, buffer rings,
    # measure. Anchored on /*INSIGHTS_V1*/ so the panel's already wired.
    ("TOOLS_V1: address search + draw aggregator + rings + measure", P8_OLD, P8_NEW, "/*TOOLS_V1*/"),
    # P9: paginated PDF reports + quarterly Commission packet template.
    # Reuses jsPDF loaded by P6.
    ("REPORTS_V1: multi-page county report + commission packet", P9_OLD, P9_NEW, "/*REPORTS_V1*/"),
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

    for entry in PATCHES:
        # 3-tuple (name, old, new): the legacy form used by P1-P4. Idempotency
        # is detected by `new in text and old not in text`.
        # 4-tuple (name, old, new, marker): the marker is a unique substring
        # that, if present in the file, means the patch is already applied.
        # Use this whenever the new content embeds the old anchor (e.g. when
        # appending to a script block) — without it, re-running the patch
        # would double-inject because the old anchor still appears in the
        # new content.
        if len(entry) == 4:
            name, old, new, marker = entry
        else:
            name, old, new = entry
            marker = None

        if marker is not None and marker in text:
            already.append(name)
            continue
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
