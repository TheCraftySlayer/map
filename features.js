/*
 * features.js — post-decrypt feature layer for the Spatial Equity map.
 *
 * Loaded by index.html into the decrypted document BEFORE any other script.
 * Uses a setter trap on window.L to capture the Leaflet map instance at
 * create time, without requiring any edits to the encrypted map body.
 *
 * Feature modules (each is defensive — if the hook it needs isn't there,
 * it silently no-ops rather than throwing):
 *
 *   - urlState      hash-encode layer/year/pinned nbhd so views are shareable
 *   - palette       colorblind-safe CSS filter toggle, persisted
 *   - print         print-view stylesheet + toolbar button
 *   - keyboard      `?` overlay listing shortcuts
 *   - csvExport     export pinned neighborhood properties as CSV
 *   - equityBadge   small badge showing the equity_index + completeness
 *                   from META / nbhd props when available
 *
 * The in-body features listed in patch_body.py (lasso, LISA, bivariate,
 * sparklines, animation, address search, mobile sheet, PDF one-pager)
 * rely on internals of the encrypted body and are implemented there via
 * patches you apply locally with `python patch_body.py index_body.html`.
 */
(function(){
  'use strict';

  const SE = window.__se = window.__se || {};
  SE.ready = false;
  SE.map = null;
  SE.L = null;
  SE.readyListeners = [];
  SE.onReady = function(fn){
    if(SE.ready) try{ fn(SE); }catch(e){ console.warn('[se] ready cb', e); }
    else SE.readyListeners.push(fn);
  };

  // ── Leaflet capture ───────────────────────────────────────────────────────
  // Trap window.L so we can wrap L.map before any body script creates the
  // map instance. Falls back to polling if Leaflet was already loaded when
  // this script ran (e.g. aggressive preload).
  function wrapLeaflet(L){
    if(!L || L.__seWrapped) return;
    L.__seWrapped = true;
    SE.L = L;
    const origMap = L.map;
    L.map = function(id, opts){
      const m = origMap.call(this, id, opts);
      if(!SE.map){
        SE.map = m;
        onMapReady(m);
      }
      return m;
    };
    // Copy static members so `L.map.FOO` keeps working.
    for(const k in origMap) try{ L.map[k] = origMap[k]; }catch(_){}
  }
  (function installLTrap(){
    if(Object.getOwnPropertyDescriptor(window,'L')){
      // L already defined — wrap directly.
      try{ wrapLeaflet(window.L); }catch(e){ console.warn('[se] wrap', e); }
      return;
    }
    let _L;
    try{
      Object.defineProperty(window,'L',{
        configurable:true,
        enumerable:true,
        get(){ return _L; },
        set(v){ _L = v; try{ wrapLeaflet(v); }catch(e){ console.warn('[se] wrap', e); } },
      });
    }catch(_){
      // Setter trap failed — poll as a fallback.
      const iv = setInterval(()=>{
        if(window.L){ clearInterval(iv); try{ wrapLeaflet(window.L); }catch(_){} }
      }, 50);
      setTimeout(()=>clearInterval(iv), 15000);
    }
  })();

  function onMapReady(map){
    // Some body code attaches the nbhd choropleth as a GeoJSON layer well
    // after map creation. Wait for it before calling modules that need
    // polygon features. If it never shows up, modules still run with
    // whatever they can find.
    SE.ready = true;
    map.whenReady(()=>{
      const run = ()=>{
        initToolbar();
        try{ modPalette(); }catch(e){ console.warn('[se] palette', e); }
        try{ modPrint(); }catch(e){ console.warn('[se] print', e); }
        try{ modKeyboard(); }catch(e){ console.warn('[se] keyboard', e); }
        try{ modUrlState(); }catch(e){ console.warn('[se] urlState', e); }
        try{ modCsvExport(); }catch(e){ console.warn('[se] csv', e); }
        try{ modEquityBadge(); }catch(e){ console.warn('[se] equityBadge', e); }
        SE.readyListeners.forEach(fn=>{ try{ fn(SE); }catch(_){} });
      };
      // Defer a tick so body-side `map.on('load',…)` handlers run first.
      setTimeout(run, 50);
    });
  }

  // ── Toolbar container ─────────────────────────────────────────────────────
  // Single floating toolbar in the top-right. Modules append their own
  // buttons to avoid stepping on body-side controls.
  function initToolbar(){
    if(document.getElementById('__se_toolbar')) return;
    const bar = document.createElement('div');
    bar.id = '__se_toolbar';
    bar.setAttribute('role','toolbar');
    bar.setAttribute('aria-label','Spatial Equity tools');
    bar.style.cssText =
      'position:fixed;top:10px;right:10px;z-index:10000;display:flex;'+
      'flex-direction:column;gap:6px;font:12px system-ui,-apple-system,sans-serif;';
    document.body.appendChild(bar);
    SE.toolbar = bar;
    const style = document.createElement('style');
    style.textContent =
      '#__se_toolbar button{background:#fff;color:#185FA5;border:1px solid #185FA5;'+
      'border-radius:4px;padding:5px 10px;cursor:pointer;font:inherit;min-width:120px;'+
      'text-align:left;box-shadow:0 1px 3px rgba(0,0,0,.12)}'+
      '#__se_toolbar button:hover{background:#185FA5;color:#fff}'+
      '#__se_toolbar button[aria-pressed="true"]{background:#185FA5;color:#fff}'+
      '@media print{#__se_toolbar{display:none!important}}'+
      'body.se-cb-viridis .leaflet-overlay-pane{filter:hue-rotate(30deg) saturate(1.1)}'+
      'body.se-cb-cividis .leaflet-overlay-pane{filter:hue-rotate(-20deg) saturate(.9)}'+
      '@media print{'+
        '.leaflet-control-container,#side,.sidebar,#legend{break-inside:avoid}'+
        'body{background:#fff!important}'+
      '}';
    document.head.appendChild(style);
  }
  function addButton(opts){
    if(!SE.toolbar) initToolbar();
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = opts.label;
    if(opts.title) b.title = opts.title;
    if(opts.id) b.id = opts.id;
    b.addEventListener('click', opts.onClick);
    SE.toolbar.appendChild(b);
    return b;
  }

  // ── Module: colorblind-safe palette toggle ────────────────────────────────
  // Applies a CSS filter to the overlay pane. Not a true palette swap (that
  // would require re-coloring the choropleth), but enough of a shift to
  // help red/green-deficient users distinguish bands. Persists choice.
  function modPalette(){
    const KEY = 'se_palette';
    const modes = ['default','viridis','cividis'];
    let idx = modes.indexOf(localStorage.getItem(KEY)||'default');
    if(idx < 0) idx = 0;
    apply();
    addButton({
      label: 'Palette: '+modes[idx],
      title: 'Cycle color filter (default → viridis → cividis)',
      id: '__se_btn_palette',
      onClick(){ idx = (idx+1) % modes.length; localStorage.setItem(KEY, modes[idx]); apply(); this.textContent = 'Palette: '+modes[idx]; },
    });
    function apply(){
      document.body.classList.remove('se-cb-viridis','se-cb-cividis');
      if(modes[idx]==='viridis') document.body.classList.add('se-cb-viridis');
      if(modes[idx]==='cividis') document.body.classList.add('se-cb-cividis');
    }
  }

  // ── Module: print view ────────────────────────────────────────────────────
  function modPrint(){
    addButton({
      label: 'Print map',
      title: 'Open the browser print dialog with a map-friendly stylesheet',
      onClick(){
        // Invalidate map size so the printed view matches the viewport
        // after the print dialog resizes things. Leaflet's quirk.
        try{ SE.map && SE.map.invalidateSize(); }catch(_){}
        window.print();
      },
    });
  }

  // ── Module: keyboard help overlay ────────────────────────────────────────
  function modKeyboard(){
    document.addEventListener('keydown', e=>{
      if(e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if(e.key === '?'){ toggleHelp(); e.preventDefault(); }
      else if(e.key === 'Escape'){ const o=document.getElementById('__se_help'); if(o) o.remove(); }
    });
    addButton({
      label: 'Shortcuts (?)',
      title: 'Show keyboard shortcuts',
      onClick: toggleHelp,
    });
    function toggleHelp(){
      const existing = document.getElementById('__se_help');
      if(existing){ existing.remove(); return; }
      const o = document.createElement('div');
      o.id = '__se_help';
      o.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20000;'+
        'display:flex;align-items:center;justify-content:center;'+
        'font:13px system-ui,-apple-system,sans-serif;color:#222';
      o.innerHTML =
        '<div style="background:#fff;padding:20px 26px;border-radius:8px;max-width:440px;box-shadow:0 4px 18px rgba(0,0,0,.3)">'+
        '<h2 style="margin:0 0 10px;font-size:15px;color:#08306b">Keyboard shortcuts</h2>'+
        '<table style="border-collapse:collapse;width:100%">'+
        row('?', 'Toggle this help')+
        row('Esc', 'Close help / unpin neighborhood')+
        row('P', 'Print map')+
        row('C', 'Cycle colorblind palette')+
        row('U', 'Copy shareable URL')+
        row('E', 'Export pinned neighborhood CSV')+
        '</table>'+
        '<p style="margin:12px 0 0;font-size:11px;color:#666">Click anywhere to close.</p>'+
        '</div>';
      o.addEventListener('click', ()=>o.remove());
      document.body.appendChild(o);
      function row(k,v){
        return '<tr><td style="padding:3px 10px 3px 0;font-family:ui-monospace,monospace;color:#185FA5">'+k+'</td><td style="padding:3px 0">'+v+'</td></tr>';
      }
    }
    // Letter shortcuts.
    document.addEventListener('keydown', e=>{
      if(e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if(e.ctrlKey||e.metaKey||e.altKey) return;
      const k = (e.key||'').toLowerCase();
      if(k==='p'){ document.getElementById('__se_toolbar').querySelector('button:nth-child(2)')?.click?.(); }
      else if(k==='c'){ document.getElementById('__se_btn_palette')?.click?.(); }
      else if(k==='u'){ SE.urlState && SE.urlState.copy && SE.urlState.copy(); }
      else if(k==='e'){ SE.csvExport && SE.csvExport.run && SE.csvExport.run(); }
    });
  }

  // ── Module: URL state ─────────────────────────────────────────────────────
  // Encodes layer + year + pinned nbhd in location.hash. Does NOT require
  // the body to cooperate — polls a few well-known globals and DOM nodes.
  // If none of them exist, the module still installs the copy-URL button;
  // it just won't capture state.
  function modUrlState(){
    const probes = {
      layer: ()=> window.NBL || readSel('#layerSelect,[name="layer"],select.layer'),
      year:  ()=> window.curYr || readSel('#yearSelect,[name="year"],select.year'),
      nbhd:  ()=> window.pinnedNbhd && (window.pinnedNbhd.feature?.properties?.nbhd ?? window.pinnedNbhd.nbhd ?? null),
    };
    function readSel(sel){
      const el = document.querySelector(sel);
      return el ? el.value : null;
    }
    function snapshot(){
      const s = {};
      for(const k in probes){ const v = probes[k](); if(v!=null && v!=='') s[k] = v; }
      return s;
    }
    function encode(s){
      const parts = [];
      for(const k in s) parts.push(encodeURIComponent(k)+'='+encodeURIComponent(s[k]));
      return parts.join('&');
    }
    function decode(hash){
      const s = {};
      (hash||'').replace(/^#/, '').split('&').filter(Boolean).forEach(p=>{
        const [k,v] = p.split('=');
        s[decodeURIComponent(k)] = decodeURIComponent(v||'');
      });
      return s;
    }
    function write(){
      const enc = encode(snapshot());
      if(enc) location.replace('#'+enc);
    }
    function applyIncoming(){
      const s = decode(location.hash);
      if(s.layer){
        const el = document.querySelector('#layerSelect,[name="layer"],select.layer');
        if(el && el.value !== s.layer){ el.value = s.layer; el.dispatchEvent(new Event('change',{bubbles:true})); }
      }
      if(s.year){
        const el = document.querySelector('#yearSelect,[name="year"],select.year');
        if(el && el.value !== s.year){ el.value = s.year; el.dispatchEvent(new Event('change',{bubbles:true})); }
      }
      // Pinning is body-internal; leave nbhd as a hint for the user.
    }
    applyIncoming();
    let lastEnc = '';
    setInterval(()=>{
      const enc = encode(snapshot());
      if(enc && enc !== lastEnc){ lastEnc = enc; history.replaceState(null,'','#'+enc); }
    }, 1500);
    SE.urlState = {
      copy(){
        const enc = encode(snapshot());
        const url = location.origin + location.pathname + (enc?('#'+enc):'');
        navigator.clipboard?.writeText?.(url).then(
          ()=>flash('URL copied'),
          ()=>flash('Copy failed — URL in console'));
        console.log('[se] share URL:', url);
      },
    };
    addButton({
      label: 'Copy URL',
      title: 'Copy a shareable URL that restores the current layer/year',
      onClick: SE.urlState.copy,
    });
    function flash(msg){
      const el = document.createElement('div');
      el.textContent = msg;
      el.style.cssText =
        'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);'+
        'background:#185FA5;color:#fff;padding:8px 16px;border-radius:4px;'+
        'font:13px system-ui;z-index:30000;box-shadow:0 2px 8px rgba(0,0,0,.25)';
      document.body.appendChild(el);
      setTimeout(()=>el.remove(), 1800);
    }
  }

  // ── Module: CSV export ────────────────────────────────────────────────────
  // Exports either the currently pinned neighborhood's properties, or —
  // if none pinned — every visible (un-filtered) neighborhood's row.
  function modCsvExport(){
    SE.csvExport = { run };
    addButton({
      label: 'Export CSV',
      title: 'Download the pinned neighborhood, or all visible rows, as CSV',
      onClick: run,
    });
    function run(){
      const rows = collect();
      if(!rows.length){ alert('No neighborhood data available to export.'); return; }
      const cols = unionCols(rows);
      const csv = [cols.join(',')].concat(
        rows.map(r => cols.map(c => csvCell(r[c])).join(','))
      ).join('\n');
      const blob = new Blob([csv], {type:'text/csv;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'spatial_equity_'+(new Date().toISOString().slice(0,10))+'.csv';
      document.body.appendChild(a);
      a.click();
      setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 0);
    }
    function collect(){
      const rows = [];
      // 1. Pinned neighborhood (body may expose window.pinnedNbhd).
      const pinned = window.pinnedNbhd;
      if(pinned && pinned.feature && pinned.feature.properties){
        rows.push(pinned.feature.properties);
        return rows;
      }
      // 2. All nbhd polygons — iterate every layer on the map, pick ones
      //    whose features carry an `nbhd` property.
      if(SE.map){
        SE.map.eachLayer(l => {
          if(l.feature && l.feature.properties && ('nbhd' in l.feature.properties)){
            rows.push(l.feature.properties);
          }
          if(l.eachLayer && typeof l.eachLayer === 'function'){
            try{
              l.eachLayer(sub => {
                if(sub.feature && sub.feature.properties && ('nbhd' in sub.feature.properties)){
                  rows.push(sub.feature.properties);
                }
              });
            }catch(_){}
          }
        });
      }
      // De-dup by nbhd.
      const seen = new Set();
      return rows.filter(r => {
        const k = r.nbhd; if(seen.has(k)) return false; seen.add(k); return true;
      });
    }
    function unionCols(rows){
      const set = new Set();
      rows.forEach(r => Object.keys(r).forEach(k => set.add(k)));
      // Keep nbhd first for readability.
      const cols = Array.from(set);
      cols.sort((a,b)=> a==='nbhd' ? -1 : b==='nbhd' ? 1 : a.localeCompare(b));
      return cols;
    }
    function csvCell(v){
      if(v === null || v === undefined) return '';
      const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s;
    }
  }

  // ── Module: equity badge ──────────────────────────────────────────────────
  // Small footer badge showing the as-of date (from META written by
  // build_data.py). Also shows equity_index + completeness for the hovered
  // or pinned neighborhood if those fields exist on the feature.
  function modEquityBadge(){
    const el = document.createElement('div');
    el.id = '__se_badge';
    el.style.cssText =
      'position:fixed;bottom:8px;right:10px;z-index:9999;background:rgba(255,255,255,.92);'+
      'color:#08306b;padding:5px 10px;border-radius:3px;font:11px system-ui;'+
      'box-shadow:0 1px 3px rgba(0,0,0,.1);pointer-events:none';
    document.body.appendChild(el);
    const asOf = (window.coreData && window.coreData.META && window.coreData.META.as_of) || '';
    function render(props){
      const parts = [];
      if(asOf) parts.push('Data as of '+asOf);
      if(props){
        if(typeof props.equity_index === 'number')
          parts.push('Equity idx: '+(props.equity_index*100).toFixed(0));
        if(typeof props.data_completeness === 'number')
          parts.push('Completeness: '+(props.data_completeness*100).toFixed(0)+'%');
      }
      el.textContent = parts.join(' · ');
      el.style.display = parts.length ? 'block' : 'none';
    }
    render(null);
    if(SE.map){
      SE.map.on('mouseover', ev=>{
        const f = ev.layer && ev.layer.feature;
        if(f && f.properties) render(f.properties);
      });
      SE.map.on('mouseout', ()=> render(window.pinnedNbhd?.feature?.properties||null));
    }
    SE.equityBadge = { render };
  }

})();
