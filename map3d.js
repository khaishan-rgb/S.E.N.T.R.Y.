/* SG Transport Pulse - shared 3D map (V16.24). Served by the backend at /map3d.js.
   Adds a "3D VIEW" button to a Leaflet map: the map tilts and the buildings stand up in 3D (MapLibre GL 4.7.1 + OpenFreeMap vector
   tiles: free, no key). The 3D map is a live mirror of the normal (2D) map: every line, stop, bus, marker, popup and tooltip on the 2D
   map is copied into it, so the page's own data, refreshes, filters and clicks keep working unchanged. Nothing is downloaded until the
   button is pressed for the first time; if the library, the tiles or WebGL are not available the page simply stays in 2D.

   Use, after leaflet.js:
     <script src="/map3d.js"></script>
     SG3D.attach(map, {...})          a map that already exists
     SG3D.auto("map", {...})          a map created later (also inside a page's private code) in the element with that id
   Options (all optional):
     key      remembers the last choice in this browser under this name (e.g. "rt.3d")
     host     element / selector the 3D map and its buttons go into (default: the map's parent element)
     z        z-index of the 3D map: above the 2D map, below the page's own floating panels (default 750)
     id       id for the 3D button; button: {into: selector, first: true} puts it into a toolbar instead of a round floating button
     vars     CSS variables for the positions (desktop), varsSmall for screens up to 760 px wide (see the CSS below)
     compass  corner of the turn / tilt compass (default "top-right")
     walls    {label, title, panes: {pane: half-width m}, heights: {colour: m}, hint, on}: raise lines of these colours in 3D
     bodies   function(SG3D) -> GeoJSON features (fill-extrusion: properties c colour, h height) e.g. live buses as 3D buses
     lift     selector of marker icons lifted above the 3D bodies when close up (e.g. ".bm")
     wire     function(popupElement) for buttons inside mirrored popups
     hide     selectors hidden while in 3D (e.g. a 2D-only tool)
     hint     first-time tip, mapClicks (default true): a tap on empty 3D map is a tap on the 2D map
     zoom     true: + / - zoom buttons on the 3D map (V16.26)
     drag     (default true) draggable 2D markers can be dragged in 3D too: the 2D marker moves and gets dragstart / drag / dragend
     lens     {label: fn -> text, can: fn -> bool, pick: fn(latlng)}: press and hold on the 3D map opens a magnifier (x4); let go to
              tap that point (default: a click on the 2D map there, when can() allows it)
     clickThrough  fn -> true while every tap must reach the 2D map (e.g. drawing), even on top of a line with a tooltip */
(function(){
  "use strict";
  if(typeof L === "undefined" || window.SG3D) return;
  var ML_JS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js", ML_CSS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css";
  var TILES = "https://tiles.openfreemap.org";                         // OpenFreeMap: OpenStreetMap vector tiles with building heights, no key
  var BASE_RX = /basemaps\.cartocdn\.com|onemap\.gov\.sg|tile\.openstreetmap\.org|\/api\/tomtom\/map\//;   // 2D basemaps (the 3D map draws its own)
  var PANE_Z = {overlayPane:400, shadowPane:500, markerPane:600, tooltipPane:650, popupPane:700};
  var TILTS = [60, 75, 45, 0];
  var mq = function(q){ return !!(window.matchMedia && matchMedia(q).matches); };
  var reduce = mq("(prefers-reduced-motion: reduce)"), coarse = mq("(pointer: coarse)");
  var ls = function(k, v){ if(!k) return null; try{ if(v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); }catch(e){} return null; };
  var CFG = {}, loading = null;

  /* ---------- styles (once) */
  var CSS = [
    ".sg3d,.sg3d.maplibregl-map{position:absolute;inset:0;z-index:var(--sg3d-z,750);background:#081428;visibility:hidden;pointer-events:none}",   /* beats maplibre-gl.css (loaded later) */
    ".is3d>.sg3d,.is3d>.sg3d.maplibregl-map{visibility:visible;pointer-events:auto}",
    ".is3d .leaflet-control-scale,.is3d .leaflet-control-attribution,.is3d .sgbm{visibility:hidden}",
    ".sg3d-btn{position:absolute;z-index:calc(var(--sg3d-z,750) + 2);right:var(--sg3d-br,12px);bottom:var(--sg3d-bb,12px);top:var(--sg3d-bt,auto);width:54px;height:54px;border-radius:50%;display:grid;place-items:center;align-content:center;cursor:pointer;padding:0;",
    "  background:rgba(6,17,32,.93);border:1px solid #1f4468;color:#dff3ff;box-shadow:0 8px 24px rgba(0,10,30,.4);font:800 16px/1 Inter,system-ui,sans-serif;letter-spacing:.02em}",
    ".sg3d-btn small{display:block;font-size:8.5px;font-weight:700;letter-spacing:.1em;color:#8fb3d4;margin-top:3px}",
    ".sg3d-btn[aria-pressed=\"true\"]{border-color:#3cc8ff;background:#0e2c52;box-shadow:0 0 0 2px rgba(60,200,255,.35),0 8px 24px rgba(0,10,30,.45)}",
    ".sg3d-tb[aria-pressed=\"true\"]{border-color:#3cc8ff!important;background:#0e2c52!important;color:#fff!important}",
    ".sg3d-btn:disabled,.sg3d-tb:disabled{opacity:.6;cursor:progress}",
    ".sg3d-opt{position:absolute;z-index:calc(var(--sg3d-z,750) + 2);right:var(--sg3d-or,76px);bottom:var(--sg3d-ob,12px);left:var(--sg3d-ol,auto);top:var(--sg3d-ot,auto);display:none;gap:5px;padding:5px;align-items:center;",
    "  background:rgba(6,17,32,.93);border:1px solid #1f4468;border-radius:12px;box-shadow:0 8px 24px rgba(0,10,30,.4)}",
    ".is3d>.sg3d-opt{display:flex}",
    ".sg3d-opt button{height:32px;padding:0 10px;border-radius:8px;border:1px solid #1f4468;background:rgba(6,17,32,.92);color:#9fb6cf;font:600 12px Inter,system-ui,sans-serif;cursor:pointer;white-space:nowrap}",
    ".sg3d-opt button[aria-pressed=\"true\"]{border-color:#3b8bff;color:#fff;background:#0e2c52}",
    ".sg3d-opt .s{display:none}.sg3d-opt [data-o=\"walls\"] .s{color:#ff6b6b}",
    ".sg3d-opt button:focus-visible,.sg3d-btn:focus-visible{outline:2px solid #3cc8ff;outline-offset:2px}",
    ".sg3d-busy{position:absolute;inset:0;display:none;place-items:center;z-index:5;color:#cfe6ff;font:14px Inter,system-ui,sans-serif;background:rgba(8,20,40,.55)}",
    ".sg3d.busy .sg3d-busy{display:grid}",
    ".sg3d .maplibregl-ctrl-top-right{top:var(--sg3d-ct,10px)}.sg3d .maplibregl-ctrl-top-left{top:var(--sg3d-ct,10px)}.sg3d .maplibregl-ctrl-bottom-left{bottom:var(--sg3d-cb,10px)}",
    ".sg3d .maplibregl-ctrl-group{background:rgba(6,17,32,.93);border:1px solid #1f4468;box-shadow:0 8px 24px rgba(0,10,30,.4)}",
    ".sg3d .maplibregl-ctrl-group button .maplibregl-ctrl-icon{filter:invert(1) brightness(1.4)}",
    ".sg3d .maplibregl-ctrl-attrib,.sg3d .maplibregl-ctrl-attrib.maplibregl-compact{background:rgba(6,17,32,.8);color:#9fb6cf}.sg3d .maplibregl-ctrl-attrib a{color:#9fd8ff}",
    ".sg3d .maplibregl-ctrl-attrib-button{filter:invert(1)}",
    ".sg3dm{cursor:pointer}",
    ".sg3d-toast{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:calc(var(--sg3d-z,750) + 1);pointer-events:none;display:none;max-width:min(92%,460px);padding:9px 14px;border-radius:10px;",
    "  background:rgba(6,17,32,.96);border:1px solid #1f4468;color:#e6f1ff;font:13px/1.4 Inter,system-ui,sans-serif;text-align:center;box-shadow:0 8px 24px rgba(0,10,30,.45)}",
    ".sg3d-toast.on{display:block}",
    ".maplibregl-popup.sg3d-pop .maplibregl-popup-content{background:#fff;color:#0a1a2e;border-radius:12px;box-shadow:0 10px 30px rgba(0,10,30,.4);padding:12px 16px;font-size:14px;line-height:1.45;min-width:190px}",
    ".maplibregl-popup.sg3d-pop .maplibregl-popup-close-button{font-size:18px;color:#5a6f86;right:4px;top:2px}",
    ".maplibregl-popup.sg3d-tip{pointer-events:none}.maplibregl-popup.sg3d-tip .maplibregl-popup-content{background:rgba(6,17,32,.95);color:#e6f1ff;border:1px solid #1f4468;border-radius:8px;padding:6px 9px;font-size:12.5px;line-height:1.35;box-shadow:0 6px 18px rgba(0,10,30,.45)}",
    ".maplibregl-popup.sg3d-tip .maplibregl-popup-tip{display:none}",
    ".maplibregl-popup.sg3d-pop.maplibregl-popup-anchor-bottom .maplibregl-popup-tip{border-top-color:#fff}.maplibregl-popup.sg3d-pop.maplibregl-popup-anchor-top .maplibregl-popup-tip{border-bottom-color:#fff}",
    ".maplibregl-popup.sg3d-pop.maplibregl-popup-anchor-left .maplibregl-popup-tip{border-right-color:#fff}.maplibregl-popup.sg3d-pop.maplibregl-popup-anchor-right .maplibregl-popup-tip{border-left-color:#fff}",
    ".sg3d-lens{position:absolute;z-index:calc(var(--sg3d-z,750) + 4);width:190px;height:190px;border-radius:50%;overflow:hidden;border:3px solid #38d6ff;background:#0a1830;",
    "  box-shadow:0 0 0 2px rgba(0,0,0,.65),0 12px 34px rgba(0,0,0,.65);pointer-events:none;visibility:hidden;transform:translate(-50%,-50%)}",
    ".sg3d-lens.on{visibility:visible}.sg3d-lens-map,.sg3d-lens-map.maplibregl-map{position:absolute;inset:0}",
    ".sg3d-lens-x{position:absolute;left:50%;top:50%;width:26px;height:26px;transform:translate(-50%,-50%);z-index:3}",
    ".sg3d-lens-x:before,.sg3d-lens-x:after{content:\"\";position:absolute;background:#fff;box-shadow:0 0 0 1px rgba(0,0,0,.7)}",
    ".sg3d-lens-x:before{left:12px;top:0;width:2px;height:26px}.sg3d-lens-x:after{top:12px;left:0;height:2px;width:26px}",
    ".sg3d-lens-z{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:3;padding:2px 8px;border-radius:999px;background:rgba(5,15,30,.85);color:#bfefff;font:700 10.5px Inter,system-ui,sans-serif;white-space:nowrap}",
    "@media (max-width:760px){",
    "  .sg3d-lens{width:170px;height:170px}",
    "  .sg3d-btn{width:46px;height:46px;font-size:14px}.sg3d-btn small{font-size:7.5px}",
    "  .sg3d-opt{flex-direction:column;padding:0;gap:6px;background:none;border:0;box-shadow:none}",
    "  .sg3d-opt button{width:46px;height:40px;padding:0;font-size:13px;font-weight:700;background:rgba(6,17,32,.93);box-shadow:0 6px 18px rgba(0,10,30,.4)}",
    "  .sg3d-opt .l{display:none}.sg3d-opt .s{display:inline}.sg3d-opt.row{flex-direction:row}",
    "}"
  ].join("\n");
  function addCss(){ if(document.getElementById("sg3d-css")) return; var s = document.createElement("style"); s.id = "sg3d-css"; s.textContent = CSS; document.head.appendChild(s); }

  /* ---------- the library: loaded once, the first time any 3D map is opened */
  function load(){
    if(window.maplibregl) return Promise.resolve();
    if(loading) return loading;
    loading = new Promise(function(res, rej){
      var l = document.createElement("link"); l.rel = "stylesheet"; l.href = ML_CSS; document.head.appendChild(l);
      var s = document.createElement("script"); s.src = ML_JS; s.async = true;
      s.onload = function(){ window.maplibregl ? res() : rej(new Error("no maplibregl")); };
      s.onerror = function(){ loading = null; rej(new Error("load failed")); };
      document.head.appendChild(s);
    });
    return loading;
  }

  /* ---------- dark navy basemap in the platform colours, buildings at their real height (OpenStreetMap) */
  function style(){
    var name = ["case", ["has", "name:nonlatin"], ["coalesce", ["get", "name:latin"], ["get", "name"]], ["coalesce", ["get", "name_en"], ["get", "name"]]];
    var lines = ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false], polys = ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
    var road = function(id, classes, color, w0, w1, minz){ return {id:id, type:"line", source:"omt", "source-layer":"transportation", minzoom:minz,
      filter:["all", lines, ["match", ["get", "class"], classes, true, false], ["!=", ["get", "brunnel"], "tunnel"]],
      layout:{"line-cap":"round", "line-join":"round"}, paint:{"line-color":color, "line-width":["interpolate", ["exponential", 1.5], ["zoom"], 10, w0, 18, w1]}}; };
    return {version:8, glyphs:TILES + "/fonts/{fontstack}/{range}.pbf",
      sources:{omt:{type:"vector", url:TILES + "/planet"}},
      layers:[
        {id:"bg", type:"background", paint:{"background-color":"#0a1830"}},
        {id:"park", type:"fill", source:"omt", "source-layer":"landuse", filter:["all", polys, ["match", ["get", "class"], ["park", "cemetery", "grass"], true, false]], paint:{"fill-color":"#0d2a33", "fill-opacity":.7}},
        {id:"wood", type:"fill", source:"omt", "source-layer":"landcover", filter:["all", polys, ["match", ["get", "class"], ["wood", "grass", "wetland"], true, false]], paint:{"fill-color":"#0c2a30", "fill-opacity":.6}},
        {id:"water", type:"fill", source:"omt", "source-layer":"water", filter:["all", polys, ["!=", ["get", "brunnel"], "tunnel"]], paint:{"fill-color":"#05213f"}},
        road("road-minor", ["minor", "service", "track"], "#192b4b", .3, 7, 12),
        road("road-major", ["primary", "secondary", "tertiary", "trunk"], "#233a64", .7, 15, 9),
        road("road-motorway", ["motorway"], "#2b4a7d", 1, 19, 8),
        {id:"rail", type:"line", source:"omt", "source-layer":"transportation", minzoom:12, filter:["all", lines, ["match", ["get", "class"], ["rail", "transit"], true, false], ["!=", ["get", "brunnel"], "tunnel"]],
          paint:{"line-color":"#3a4f78", "line-width":1.2, "line-dasharray":[3, 2]}},
        {id:"building-flat", type:"fill", source:"omt", "source-layer":"building", minzoom:13, paint:{"fill-color":"#16284a", "fill-outline-color":"#22396a"}},
        {id:"building-3d", type:"fill-extrusion", source:"omt", "source-layer":"building", minzoom:13, filter:["!", ["has", "hide_3d"]],
          paint:{"fill-extrusion-color":["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 8], 0, "#1b2c52", 30, "#23396b", 80, "#2c4a88", 200, "#3b62a8"],
                 "fill-extrusion-height":["interpolate", ["linear"], ["zoom"], 13, 0, 14, ["coalesce", ["get", "render_height"], 8]],
                 "fill-extrusion-base":["interpolate", ["linear"], ["zoom"], 13, 0, 14, ["coalesce", ["get", "render_min_height"], 0]],
                 "fill-extrusion-opacity":.9, "fill-extrusion-vertical-gradient":true}},
        {id:"road-label", type:"symbol", source:"omt", "source-layer":"transportation_name", minzoom:14, filter:lines,
          layout:{"symbol-placement":"line", "symbol-spacing":350, "text-field":name, "text-font":["Noto Sans Regular"], "text-size":11, "text-max-angle":30},
          paint:{"text-color":"#9fb6d8", "text-halo-color":"#0a1830", "text-halo-width":1.4}},
        {id:"place-label", type:"symbol", source:"omt", "source-layer":"place", maxzoom:16, filter:["match", ["get", "class"], ["suburb", "neighbourhood", "quarter", "town", "city"], true, false],
          layout:{"text-field":name, "text-font":["Noto Sans Regular"], "text-size":["match", ["get", "class"], ["city", "town"], 14, 11.5], "text-transform":"uppercase", "text-letter-spacing":.08},
          paint:{"text-color":"#c3d6f0", "text-halo-color":"#0a1830", "text-halo-width":1.5}}
      ]};
  }

  /* ---------- geometry helpers */
  var ll2 = function(ll){ return [ll.lng, ll.lat]; };
  function ring(lls){ var r = lls.map(ll2); if(r.length && (r[0][0] !== r[r.length - 1][0] || r[0][1] !== r[r.length - 1][1])) r.push(r[0]); return r; }
  function circ(ll, m){ var out = [], kx = 111320 * Math.cos(ll.lat * Math.PI / 180), ky = 110574;
    for(var i = 0; i <= 48; i++){ var a = i / 48 * 2 * Math.PI; out.push([ll.lng + m * Math.cos(a) / kx, ll.lat + m * Math.sin(a) / ky]); } return out; }
  function quad(a, b, hw){                                               // a thin box along a->b (lon/lat), hw metres either side
    var kx = 111320 * Math.cos(a[1] * Math.PI / 180), ky = 110574, dx = (b[0] - a[0]) * kx, dy = (b[1] - a[1]) * ky, d = Math.hypot(dx, dy);
    if(d < .5) return null; var nx = -dy / d * hw / kx, ny = dx / d * hw / ky;
    return [[a[0] + nx, a[1] + ny], [b[0] + nx, b[1] + ny], [b[0] - nx, b[1] - ny], [a[0] - nx, a[1] - ny], [a[0] + nx, a[1] + ny]]; }
  function box(lat, lon, brg, len, wid){                                 // a len x wid rectangle centred on lat/lon, brg radians from north
    var kx = 111320 * Math.cos(lat * Math.PI / 180), ky = 110574, ux = Math.sin(brg), uy = Math.cos(brg), a = len / 2, b = wid / 2;
    var pt = function(f, s){ return [lon + (ux * f + uy * s) / kx, lat + (uy * f - ux * s) / ky]; };
    return [[pt(a, b), pt(a, -b), pt(-a, -b), pt(-a, b), pt(a, b)]]; }
  function lum(c){ var m = /^#?([0-9a-f]{6})$/i.exec(String(c || "")); if(!m) return .5; var n = parseInt(m[1], 16);
    return (.2126 * (n >> 16 & 255) + .7152 * (n >> 8 & 255) + .0722 * (n & 255)) / 255; }
  function clicks(ly){                                                  // a real click handler (a tooltip also listens to "click" in Leaflet)
    var h = ly._events && ly._events.click; if(!h) return !!(ly.listens && ly.listens("click"));
    return h.some(function(x){ return x && x.fn !== ly._openTooltip; }); }
  function tipOf(ly){ var t = ly.getTooltip && ly.getTooltip(); if(!t) return ""; var c = t.getContent(); if(typeof c === "function") c = c(ly);
    return typeof c === "string" ? c : (c && c.outerHTML) || ""; }

  /* ---------- one 3D map for one Leaflet map */
  function attach(map, opts){
    if(map._sg3d) return map._sg3d;
    opts = opts || {}; addCss();
    var host = typeof opts.host === "string" ? document.querySelector(opts.host) : (opts.host || map.getContainer().parentNode);
    if(!host) return null;
    var cs = getComputedStyle(host); if(cs.position === "static") host.style.position = "relative";
    var walls = opts.walls || null, WH = {};
    if(walls) Object.keys(walls.heights || {}).forEach(function(c){ WH[c.toLowerCase()] = walls.heights[c]; });
    var R = {on:false, ml:null, ready:false, mk:{}, ids:{}, rs:{}, pop:null, popL:null, popH:"", popT:null, tip:null, tipId:null, t:null, mute2d:false, mute3d:false, fb:false,
             bld:ls(opts.key && opts.key + ".bld") !== "0", walls:walls ? (ls(opts.key && opts.key + ".walls") == null ? !!walls.on : ls(opts.key + ".walls") === "1") : false, tilt:0, count:null};
    var tst = null, tstT = null;
    var say = function(m, ms){
      if(typeof opts.toast === "function") return opts.toast(m, ms);
      if(typeof window.toast === "function") return window.toast(m, ms);
      if(!tst){ tst = document.createElement("div"); tst.className = "sg3d-toast"; tst.setAttribute("role", "status"); host.appendChild(tst); }
      tst.textContent = m; tst.classList.add("on"); clearTimeout(tstT); tstT = setTimeout(function(){ tst.classList.remove("on"); }, ms || 5000); };
    var small = function(){ return mq("(max-width:" + (opts.small || 760) + "px)"); };
    var setVars = function(){ var v = Object.assign({"--sg3d-z":String(opts.z || 750)}, opts.vars || {}, small() ? (opts.varsSmall || {}) : {});
      var bel = opts.below && host.querySelector(opts.below);                // --sg3d-below: just under a page panel whose height varies (e.g. wrapping chips)
      if(bel){ var hr = host.getBoundingClientRect(), br = bel.getBoundingClientRect(); v["--sg3d-below"] = Math.round((br.height ? br.bottom - hr.top : 0) + 8) + "px"; }
      Object.keys(v).forEach(function(k){ host.style.setProperty(k, v[k]); }); };
    setVars(); window.addEventListener("resize", setVars);
    if(opts.below && window.ResizeObserver){ var be = host.querySelector(opts.below); if(be) new ResizeObserver(setVars).observe(be); }

    /* page furniture */
    var el = document.createElement("div"); el.className = "sg3d"; el.setAttribute("role", "region"); el.setAttribute("aria-label", "3D map");
    el.innerHTML = '<div class="sg3d-busy" role="status">Loading 3D map…</div>';
    var mapEl = map.getContainer(); if(mapEl.parentNode === host) host.insertBefore(el, mapEl.nextSibling); else host.appendChild(el);
    var btn = document.createElement("button"); btn.type = "button"; if(opts.id) btn.id = opts.id;
    btn.setAttribute("aria-pressed", "false");
    var into = opts.button && opts.button.into && (typeof opts.button.into === "string" ? host.querySelector(opts.button.into) || document.querySelector(opts.button.into) : opts.button.into);
    if(into){ btn.className = "sg3d-tb"; if(opts.button.first) into.insertBefore(btn, into.firstChild); else into.appendChild(btn); }
    else { btn.className = "sg3d-btn"; host.appendChild(btn); }
    var opt = document.createElement("div"); opt.className = "sg3d-opt" + (opts.row ? " row" : ""); opt.setAttribute("role", "group"); opt.setAttribute("aria-label", "3D options");
    opt.innerHTML = '<button type="button" data-o="bld" aria-label="Buildings in 3D" title="Show or hide the buildings in 3D"><span class="l">Buildings</span><span class="s" aria-hidden="true">🏢</span></button>'
      + (walls ? '<button type="button" data-o="walls" aria-label="' + (walls.label || "Raise") + '" title="' + (walls.title || "") + '"><span class="l">' + (walls.label || "Raise") + '</span><span class="s" aria-hidden="true">▲</span></button>' : "")
      + '<button type="button" data-o="tilt" aria-label="Tilt" title="Change the tilt (right-drag or Ctrl + drag, or two fingers, also tilt and turn the map)"><span class="l">Tilt 60°</span><span class="s" aria-hidden="true">60°</span></button>';
    host.appendChild(opt);
    if(opts.lift){ var ls_ = document.createElement("style"); ls_.textContent = ".sg3d.close .sg3dm " + opts.lift + "{transform:translateY(-22px) scale(.85);transition:transform .2s}"; document.head.appendChild(ls_); }
    var hide = function(on){ (opts.hide || []).forEach(function(s){ document.querySelectorAll(s).forEach(function(e){ e.style.visibility = on ? "hidden" : ""; }); }); };

    /* overlay layers: the mirror of the 2D map, drawn above the basemap */
    var E = function(){ return {type:"FeatureCollection", features:[]}; };
    function overlays(ml){
      ml = ml || R.ml;
      ["ov-fill-s", "ov-line-s", "ov-circ-s", "ov-wall-s", "ov-body-s"].forEach(function(s){ ml.addSource(s, {type:"geojson", data:E(), tolerance:.2}); });
      ml.addLayer({id:"ov-fill", type:"fill", source:"ov-fill-s", paint:{"fill-color":["get", "fc"], "fill-opacity":["get", "fo"]}}, "building-flat");
      ml.addLayer({id:"ov-fill-edge", type:"line", source:"ov-fill-s", filter:[">", ["get", "w"], 0], paint:{"line-color":["get", "c"], "line-width":["get", "w"], "line-opacity":["get", "o"]}}, "building-flat");
      ml.addLayer({id:"ov-wall", type:"fill-extrusion", source:"ov-wall-s", paint:{"fill-extrusion-color":["get", "c"], "fill-extrusion-height":["get", "h"], "fill-extrusion-base":0, "fill-extrusion-opacity":.78}}, "road-label");
      ml.addLayer({id:"ov-body", type:"fill-extrusion", source:"ov-body-s", minzoom:14.5, paint:{"fill-extrusion-color":["get", "c"], "fill-extrusion-height":["get", "h"], "fill-extrusion-base":.3, "fill-extrusion-opacity":.95}}, "road-label");
      ml.addLayer({id:"ov-glow", type:"line", source:"ov-line-s", filter:["==", ["get", "g"], true], layout:{"line-cap":"round", "line-join":"round", "line-sort-key":["get", "s"]},
        paint:{"line-color":["get", "c"], "line-width":["*", ["get", "w"], 2.8], "line-blur":["*", ["get", "w"], 1.6], "line-opacity":["*", ["get", "o"], .45]}});
      ml.addLayer({id:"ov-line", type:"line", source:"ov-line-s", filter:["==", ["get", "d"], false], layout:{"line-cap":"round", "line-join":"round", "line-sort-key":["get", "s"]},
        paint:{"line-color":["get", "c"], "line-width":["get", "w"], "line-opacity":["get", "o"]}});
      ml.addLayer({id:"ov-dash", type:"line", source:"ov-line-s", filter:["==", ["get", "d"], true], layout:{"line-cap":"butt", "line-join":"round", "line-sort-key":["get", "s"]},
        paint:{"line-color":["get", "c"], "line-width":["get", "w"], "line-opacity":["get", "o"], "line-dasharray":[2, 1.6]}});
      ml.addLayer({id:"ov-circ", type:"circle", source:"ov-circ-s", layout:{"circle-sort-key":["get", "s"]},
        paint:{"circle-radius":["get", "r"], "circle-color":["get", "fc"], "circle-opacity":["get", "fo"], "circle-stroke-color":["get", "c"], "circle-stroke-width":["get", "w"], "circle-stroke-opacity":["get", "o"]}});
    }
    var QL = ["ov-circ", "ov-line", "ov-dash", "ov-fill"];

    /* copy the 2D map */
    var paneZ = function(p){ if(!p) return 400; var e = map.getPane(p); return (e && +e.style.zIndex) || PANE_Z[p] || 400; };
    function props(o, id, s, it, tt){
      var w = o.stroke === false ? 0 : (o.weight == null ? 3 : o.weight), op = o.opacity == null ? 1 : o.opacity, c = o.color || "#3388ff", lu = lum(c);
      return {id:id, s:s, i:it, t:tt, c:c, w:w, o:op, fc:o.fillColor || c, fo:o.fill ? (o.fillOpacity == null ? .2 : o.fillOpacity) : 0, d:!!o.dashArray,
              g:typeof opts.glow === "function" ? !!opts.glow(o) : (w >= 5 && op >= .6 && lu > .12 && lu < .9)}; }
    function collect(){
      var F = {fill:[], line:[], circ:[], wall:[]}, marks = [], rasters = [], ids = {}, n = 0;
      map.eachLayer(function(ly){
        if(ly instanceof L.GridLayer){ if(ly._url && !BASE_RX.test(ly._url)) rasters.push(ly); return; }
        if(ly instanceof L.LayerGroup || ly instanceof L.DivOverlay || (L.Renderer && ly instanceof L.Renderer)) return;
        var o = ly.options || {}, id = L.stamp(ly), s = paneZ(o.pane) * 10000 + (n++), it = o.interactive !== false && clicks(ly), tt = o.interactive !== false && !!(ly.getTooltip && ly.getTooltip());
        try{
          if(ly instanceof L.Circle){ ids[id] = ly; F.fill.push({type:"Feature", geometry:{type:"Polygon", coordinates:[circ(ly.getLatLng(), ly.getRadius())]}, properties:props(o, id, s, it, tt)}); }
          else if(ly instanceof L.CircleMarker){ ids[id] = ly; var p = props(o, id, s, it, tt); p.r = o.radius == null ? 10 : o.radius; if(o.fill === false) p.fo = 0; else if(o.fillOpacity == null) p.fo = .2; p.g = false;
            F.circ.push({type:"Feature", geometry:{type:"Point", coordinates:ll2(ly.getLatLng())}, properties:p}); }
          else if(ly instanceof L.Polygon){ ids[id] = ly; var g = ly.getLatLngs(), polys = !g.length ? [] : (g[0] instanceof L.LatLng ? [[g]] : (g[0][0] instanceof L.LatLng ? [g] : g));
            F.fill.push({type:"Feature", geometry:{type:"MultiPolygon", coordinates:polys.map(function(rs){ return rs.map(ring); })}, properties:props(o, id, s, it, tt)}); }
          else if(ly instanceof L.Polyline){ ids[id] = ly; var q = ly.getLatLngs(), parts = !q.length ? [] : (q[0] instanceof L.LatLng ? [q] : q), pp = props(o, id, s, it, tt);
            if(pp.w > 0) F.line.push({type:"Feature", geometry:{type:"MultiLineString", coordinates:parts.map(function(a){ return a.map(ll2); })}, properties:pp});
            var hw = walls && walls.panes && walls.panes[o.pane], h = hw && WH[String(o.color || "").toLowerCase()];
            if(h) parts.forEach(function(a){ for(var k = 1; k < a.length; k++){ var b = quad(ll2(a[k - 1]), ll2(a[k]), hw);
              if(b) F.wall.push({type:"Feature", geometry:{type:"Polygon", coordinates:[b]}, properties:{c:o.color, h:h}}); } }); }
          else if(ly instanceof L.Marker){ ids[id] = ly; marks.push(ly); }
        }catch(e){ /* one odd layer must never stop the 3D view */ }
      });
      return {F:F, marks:marks, rasters:rasters, ids:ids};
    }
    function markerEl(ly){
      var ic = ly.options.icon, o = (ic && ic.options) || {}, e = document.createElement("div");
      e.className = "sg3dm " + (o.className || "");
      if(ic instanceof L.DivIcon) e.innerHTML = o.html || "";
      else if(o.iconUrl){ var im = document.createElement("img"); im.src = o.iconUrl; im.alt = ""; im.style.width = "100%"; e.appendChild(im); }
      var sz = o.iconSize ? L.point(o.iconSize) : null;
      if(sz){ e.style.width = sz.x + "px"; e.style.height = sz.y + "px"; }
      e.style.opacity = ly.options.opacity == null ? 1 : ly.options.opacity;
      e.style.zIndex = String(10 + Math.round((ly.options.zIndexOffset || 0) / 10));
      e.style.pointerEvents = ly.options.interactive === false ? "none" : "auto";
      if(ly.options.title) e.title = ly.options.title;
      e.addEventListener("click", function(ev){ ev.stopPropagation(); if(Date.now() < (R.noClick || 0)) return; if(clicks(ly)) fire(ly, ly.getLatLng()); else { var t = tipOf(ly); if(t) tipShow(t, ly.getLatLng(), "m" + L.stamp(ly)); } });
      e.addEventListener("mouseenter", function(){ var t = tipOf(ly); if(t) tipShow(t, ly.getLatLng(), "m" + L.stamp(ly)); });
      e.addEventListener("mouseleave", function(){ if(R.tipId === "m" + L.stamp(ly)) tipHide(); });
      return e;
    }
    function dragWire(mm, ly){                                           // V16.26: drag in 3D = drag on the 2D map (e.g. the blockage to the bin)
      var ev = function(){ var p = mm.getLngLat(), ll = L.latLng(p.lat, p.lng); ly.setLatLng(ll); return {latlng:ll, originalEvent:R.ptr ? {clientX:R.ptr[0], clientY:R.ptr[1]} : null}; };
      mm.on("dragstart", function(){ R.dragging = true; tipHide(); ly.fire("dragstart", ev());
        // let go OUTSIDE the 3D canvas (e.g. on the page's bin): MapLibre only ends a marker drag on a mouseup over the map, so end it here
        var up = function(){ window.removeEventListener("mouseup", up, true); setTimeout(function(){ if(R.dragging && R.ml) R.ml.fire("mouseup"); }, 0); };
        window.addEventListener("mouseup", up, true); });
      mm.on("drag", function(){ ly.fire("drag", ev()); later(); });
      mm.on("dragend", function(){ var e = ev(); R.dragging = false; R.noClick = Date.now() + 400; e.distance = 1; ly.fire("dragend", e); later(); });
    }
    function syncMarkers(marks){
      var seen = {};
      marks.forEach(function(ly){
        var id = L.stamp(ly), ic = ly.options.icon, o = (ic && ic.options) || {}, html = o.html || o.iconUrl || "";
        var drag = opts.drag !== false && !!(ly.dragging && ly.dragging.enabled && ly.dragging.enabled());
        var key = (o.className || "") + "|" + html + "|" + ly.options.opacity + "|" + ly.options.zIndexOffset + "|" + drag;
        seen[id] = 1; var cur = R.mk[id];
        if(cur && cur.key !== key){ cur.m.remove(); cur = null; }
        if(!cur){
          var sz = o.iconSize ? L.point(o.iconSize) : L.point(0, 0), an = o.iconAnchor ? L.point(o.iconAnchor) : sz.divideBy(2), rot = /rotate\(/.test(html);   // rotated arrows turn with the map
          cur = R.mk[id] = {key:key, mo:{anchor:"top-left", offset:[-an.x, -an.y], rotationAlignment:rot ? "map" : "viewport", pitchAlignment:rot ? "map" : "viewport"}};
          cur.m = new maplibregl.Marker(Object.assign({element:markerEl(ly), draggable:drag}, cur.mo)).setLngLat(ll2(ly.getLatLng())).addTo(R.ml);
          if(drag) dragWire(cur.m, ly);
        } else cur.m.setLngLat(ll2(ly.getLatLng()));
      });
      Object.keys(R.mk).forEach(function(id){ if(!seen[id]){ R.mk[id].m.remove(); delete R.mk[id]; } });
    }
    var abs = function(u){ return (u.charAt(0) === "/" ? location.origin + u : u).replace("{r}", "").replace("{s}", "a"); };
    function syncRasters(list){
      var ml = R.ml, want = {};
      list.forEach(function(ly){
        var id = "rs-" + L.stamp(ly); want[id] = 1;
        if(ml.getSource(id)) return;
        ml.addSource(id, {type:"raster", tiles:[abs(ly._url)], tileSize:256, minzoom:Math.max(0, (ly.options.minZoom || 0) - 1), maxzoom:Math.max(1, ly.options.maxNativeZoom || ly.options.maxZoom || 18)});
        ml.addLayer({id:id, type:"raster", source:id, paint:{"raster-opacity":ly.options.opacity == null ? 1 : ly.options.opacity, "raster-fade-duration":0}}, "ov-fill");
        R.rs[id] = 1;
      });
      Object.keys(R.rs).forEach(function(id){ if(!want[id]){ if(ml.getLayer(id)) ml.removeLayer(id); if(ml.getSource(id)) ml.removeSource(id); delete R.rs[id]; } });
    }
    function sync(){
      if(!R.on || !R.ready) return;
      var c = collect(), ml = R.ml, FC = function(a){ return {type:"FeatureCollection", features:a}; }, bodies = [];
      R.ids = c.ids;
      ml.getSource("ov-fill-s").setData(FC(c.F.fill)); ml.getSource("ov-line-s").setData(FC(c.F.line));
      ml.getSource("ov-circ-s").setData(FC(c.F.circ)); ml.getSource("ov-wall-s").setData(FC(R.walls ? c.F.wall : []));
      if(typeof opts.bodies === "function"){ try{ bodies = opts.bodies(SG3D) || []; }catch(e){ bodies = []; } }
      ml.getSource("ov-body-s").setData(FC(bodies));
      R.fc = {"ov-fill-s":FC(c.F.fill), "ov-line-s":FC(c.F.line), "ov-circ-s":FC(c.F.circ), "ov-wall-s":FC(R.walls ? c.F.wall : []), "ov-body-s":FC(bodies)};
      syncMarkers(c.marks); syncRasters(c.rasters);
      if(LZ.on) lensData();
      R.count = {lines:c.F.line.length, circles:c.F.circ.length, fills:c.F.fill.length, walls:c.F.wall.length, markers:c.marks.length, rasters:c.rasters.length, bodies:bodies.length};
    }
    function later(){ if(!R.on || !R.ready) return; clearTimeout(R.t); R.t = setTimeout(sync, 120); }
    map.on("layeradd layerremove", later);

    /* clicks, popups and tooltips: the 2D handlers run, what they open is shown in 3D */
    function fire(ly, ll){
      var latlng = L.latLng(ll.lat, ll.lng);
      try{ ly.fire("click", {latlng:latlng, layerPoint:map.latLngToLayerPoint(latlng), containerPoint:map.latLngToContainerPoint(latlng), originalEvent:{}}); }catch(e){ console.error(e); }
    }
    function popHtml(p){ var e = p.getElement && p.getElement(), c = e && e.querySelector(".leaflet-popup-content"); if(c) return c.innerHTML;
      var x = p.getContent(); return typeof x === "string" ? x : (x && x.outerHTML) || ""; }
    function wire(mp){ var e = mp.getElement(); if(e && typeof opts.wire === "function") try{ opts.wire(e); }catch(err){ console.error(err); } }
    function popUpd(){ if(!R.pop || !R.popL) return; var h = popHtml(R.popL); if(h !== R.popH){ R.popH = h; R.pop.setHTML(h); wire(R.pop); } }
    function popHide(){ clearInterval(R.popT); if(R.popL) R.popL.off("contentupdate", popUpd); var mp = R.pop; R.pop = null; R.popL = null; if(mp) mp.remove(); }
    function popShow(p){
      popHide(); tipHide(); var ll = p.getLatLng(); if(!ll || !R.ml) return;
      var cls = ["sg3d-pop"].concat(String((p.options && p.options.className) || "").split(/\s+/)).filter(Boolean).join(" ");
      var html = popHtml(p), mp = new maplibregl.Popup({className:cls, maxWidth:"300px", offset:16}).setLngLat(ll2(ll)).setHTML(html).addTo(R.ml);
      R.pop = mp; R.popL = p; R.popH = html; wire(mp);
      p.on("contentupdate", popUpd);
      mp.on("close", function(){ if(R.pop !== mp) return; var l = R.popL; R.pop = null; R.popL = null; clearInterval(R.popT); if(l){ l.off("contentupdate", popUpd); map.closePopup(l); } });
      R.popT = setInterval(popUpd, 500);                                 // e.g. arrivals arriving after "Loading..." (content changed without an event)
    }
    map.on("popupopen", function(e){ if(R.on && R.ready) popShow(e.popup); });
    map.on("popupclose", function(e){ if(R.popL === e.popup) popHide(); });
    function tipShow(html, ll, key){
      if(!R.ml || R.pop) return;
      if(R.tip && R.tipId === key){ R.tip.setLngLat(ll2(ll)); return; }
      tipHide(); R.tipId = key;
      R.tip = new maplibregl.Popup({className:"sg3d-tip", closeButton:false, closeOnClick:false, offset:14, maxWidth:"280px"}).setLngLat(ll2(ll)).setHTML(html).addTo(R.ml);
    }
    function tipHide(){ if(R.tip){ R.tip.remove(); R.tip = null; } R.tipId = null; }

    /* one view: the 2D map follows the 3D camera; page moves (fit, fly to) move the 3D camera */
    map.on("moveend", function(){
      if(!R.on || !R.ready || R.mute2d) return;
      var ml = R.ml, c = map.getCenter(), mc = ml.getCenter(), p = map.latLngToContainerPoint([mc.lat, mc.lng]), sz = map.getSize();
      if(Math.hypot(p.x - sz.x / 2, p.y - sz.y / 2) < 120 && Math.abs(ml.getZoom() - (map.getZoom() - 1)) < .6) return;   // small moves (a popup panning into view) stay
      R.mute3d = true; ml.easeTo({center:[c.lng, c.lat], zoom:map.getZoom() - 1, duration:reduce ? 0 : 800});
    });
    function to2d(){
      var ml = R.ml, c = ml.getCenter(), z = Math.round(Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), ml.getZoom() + 1)));
      R.mute2d = true; try{ map.setView([c.lat, c.lng], z, {animate:false}); } finally { R.mute2d = false; }
    }

    /* options */
    function applyOpt(){
      opt.querySelector('[data-o="bld"]').setAttribute("aria-pressed", String(R.bld));
      var wb = opt.querySelector('[data-o="walls"]'); if(wb) wb.setAttribute("aria-pressed", String(R.walls));
      opt.querySelector('[data-o="tilt"] .l').textContent = "Tilt " + TILTS[R.tilt] + "°"; opt.querySelector('[data-o="tilt"] .s').textContent = TILTS[R.tilt] + "°";
      if(!R.ready) return;
      R.ml.setLayoutProperty("building-3d", "visibility", R.bld ? "visible" : "none");
      R.ml.setLayoutProperty("building-flat", "visibility", R.bld ? "none" : "visible");
    }
    opt.addEventListener("click", function(e){
      var b = e.target.closest("button[data-o]"); if(!b) return;
      var k = b.dataset.o;
      if(k === "bld"){ R.bld = !R.bld; ls(opts.key && opts.key + ".bld", R.bld ? "1" : "0"); }
      if(k === "walls"){ R.walls = !R.walls; ls(opts.key && opts.key + ".walls", R.walls ? "1" : "0"); sync(); if(R.walls && walls.hint) say(walls.hint, 4000); }
      if(k === "tilt"){ R.tilt = (R.tilt + 1) % TILTS.length; if(R.ready) R.ml.easeTo({pitch:TILTS[R.tilt], duration:reduce ? 0 : 600}); }
      applyOpt();
    });

    /* the 3D map itself */
    function make(){
      var c = map.getCenter(), ml = new maplibregl.Map({container:el, style:style(), center:[c.lng, c.lat], zoom:map.getZoom() - 1, pitch:0, bearing:0,
        minZoom:9.5, maxZoom:19, maxPitch:75, maxBounds:[[103.4, 1.05], [104.3, 1.6]], antialias:!coarse, attributionControl:false, fadeDuration:0, dragRotate:true, touchPitch:true});
      R.ml = ml;
      ml.addControl(new maplibregl.NavigationControl({showZoom:!!opts.zoom, visualizePitch:true}), opts.compass || "top-right");
      ml.addControl(new maplibregl.AttributionControl({compact:true, customAttribution:'<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a>'}), (small() && opts.attribSmall) || opts.attrib || "bottom-right");
      if(coarse || host.clientWidth < 520) ml.once("idle", function(){ var a = el.querySelector(".maplibregl-ctrl-attrib"); if(a) a.classList.remove("maplibregl-compact-show"); });   // small maps: start folded (i)
      ml.once("style.load", function(){                                  // not "load": that waits for every tile source, and never comes if one is blocked
        overlays(ml); R.ready = true; applyOpt(); el.classList.remove("busy");
        sync(); intro();
        var under = function(pt, pad){ return ml.queryRenderedFeatures([[pt.x - pad, pt.y - pad], [pt.x + pad, pt.y + pad]], {layers:QL}); };
        ml.on("click", function(e){
          if(Date.now() < (R.noClick || 0)) return;
          var ll0 = L.latLng(e.lngLat.lat, e.lngLat.lng);
          if(typeof opts.clickThrough === "function" && opts.clickThrough()){ tipHide();
            map.fire("click", {latlng:ll0, layerPoint:map.latLngToLayerPoint(ll0), containerPoint:map.latLngToContainerPoint(ll0), originalEvent:{}}); return; }
          var fs = under(e.point, 6).filter(function(f){ return f.properties.i || f.properties.t; });
          if(!fs.length){ tipHide(); if(opts.mapClicks !== false && !R.pop){ var ll = L.latLng(e.lngLat.lat, e.lngLat.lng);
            map.fire("click", {latlng:ll, layerPoint:map.latLngToLayerPoint(ll), containerPoint:map.latLngToContainerPoint(ll), originalEvent:{}}); } return; }
          fs.sort(function(a, b){ return ((b.layer.id === "ov-circ") - (a.layer.id === "ov-circ")) || ((b.properties.i ? 1 : 0) - (a.properties.i ? 1 : 0)) || (b.properties.s - a.properties.s); });
          var ly = R.ids[fs[0].properties.id]; if(!ly) return;
          if(fs[0].properties.i){ tipHide(); fire(ly, e.lngLat); } else { var t = tipOf(ly); if(t) tipShow(t, e.lngLat, "f" + fs[0].properties.id); }
        });
        var hov = 0;
        ml.on("mousemove", function(e){ var now = Date.now(); if(now - hov < 60) return; hov = now;
          var fs = under(e.point, 5).filter(function(f){ return f.properties.i || f.properties.t; });
          ml.getCanvas().style.cursor = fs.some(function(f){ return f.properties.i; }) ? "pointer" : "";
          var tf = fs.filter(function(f){ return f.properties.t; }).sort(function(a, b){ return b.properties.s - a.properties.s; })[0];
          if(tf){ var ly = R.ids[tf.properties.id], t = ly && tipOf(ly); if(t) tipShow(t, e.lngLat, "f" + tf.properties.id); }
          else if(R.tipId && R.tipId.charAt(0) === "f") tipHide(); });
        ml.getCanvas().addEventListener("mouseleave", tipHide);
      });
      ml.on("moveend", function(){ if(!R.on) return; if(R.mute3d){ R.mute3d = false; } else to2d(); later(); });
      ml.on("zoom", function(){ el.classList.toggle("close", ml.getZoom() >= 15); });
      ml.on("pitchend", function(){ var p = ml.getPitch(), k = 0, bd = 1e9; TILTS.forEach(function(t, i){ if(Math.abs(t - p) < bd){ bd = Math.abs(t - p); k = i; } }); R.tilt = k; applyOpt(); });
      ml.on("error", function(e){                                          // vector tiles unreachable (firewall): the 2D basemap, tilted
        var sid = e && (e.sourceId || (e.source && e.source.id));
        if(sid !== "omt" || R.fb || !R.ready) return;
        R.fb = true; var base = null; map.eachLayer(function(ly){ if(ly instanceof L.TileLayer && ly._url && BASE_RX.test(ly._url)) base = ly; });
        if(base){ ml.addSource("fb", {type:"raster", tiles:[abs(base._url)], tileSize:256}); ml.addLayer({id:"fb", type:"raster", source:"fb"}, "park"); }
        say("3D buildings are not available on this network right now — showing the normal map, tilted.", 7000);
      });
    }
    function intro(){ var ml = R.ml, c = map.getCenter();
      ml.jumpTo({center:[c.lng, c.lat], zoom:map.getZoom() - 1, pitch:0, bearing:0});
      ml.easeTo({pitch:TILTS[R.tilt], bearing:0, duration:reduce ? 0 : 1100}); }
    function setBtn(){ btn.setAttribute("aria-pressed", String(R.on));
      btn.innerHTML = btn.className === "sg3d-tb" ? (R.on ? "2D" : "3D") : (R.on ? "2D" : "3D") + "<small>VIEW</small>";
      btn.title = R.on ? "Back to the flat map" : "3D view: tilt the map and show the buildings in 3D"; btn.setAttribute("aria-label", R.on ? "Back to the flat map" : "3D view"); }
    function set(on){
      if(on === R.on) return Promise.resolve();
      if(!on){
        R.on = false; host.classList.remove("is3d"); popHide(); tipHide(); setBtn(); hide(false); ls(opts.key, "0");
        if(R.ml && R.ready) to2d();
        setTimeout(function(){ map.invalidateSize(); }, 0);
        if(typeof opts.onChange === "function") opts.onChange(false);
        return Promise.resolve();
      }
      btn.disabled = true; el.classList.add("busy"); host.classList.add("is3d"); hide(true);
      return load().then(function(){
        R.on = true; setBtn(); ls(opts.key, "1");
        if(!R.ml){
          try{ make(); }catch(e){ R.on = false; R.ml = null; host.classList.remove("is3d"); el.classList.remove("busy"); hide(false); setBtn();
            say("The 3D view needs WebGL, which this browser or device does not provide.", 7000); return; }
        } else { R.ml.resize(); el.classList.remove("busy"); sync(); intro(); }
        if(typeof opts.onChange === "function") opts.onChange(true);
        if(ls("sg3d.hint") !== "1"){ ls("sg3d.hint", "1"); say(opts.hint || (coarse ? "3D view: two fingers to turn and tilt. Tap the map as usual." : "3D view: right-drag (or Ctrl + drag) to turn and tilt. Click the map as usual."), 7000); }
      }, function(){
        host.classList.remove("is3d"); el.classList.remove("busy"); hide(false);
        say("The 3D view could not be loaded (no connection to the map library). The normal map still works.", 7000);
      }).then(function(){ btn.disabled = false; });
    }
    /* V16.26 press and hold on the 3D map: a round magnifier (a second, small 3D map x4 with the same lines and markers).
       Keep holding and slide to the exact spot; let go to tap it. A quick tap or a drag works as before. */
    var LZ = {t:null, on:false, el:null, ml:null, ready:false, id:null, x0:0, y0:0, touch:false, ll:null, mk:[]};
    document.addEventListener("pointermove", function(e){ R.ptr = [e.clientX, e.clientY]; }, {passive:true, capture:true});
    document.addEventListener("touchmove", function(e){ if(e.touches[0]) R.ptr = [e.touches[0].clientX, e.touches[0].clientY]; }, {passive:true, capture:true});
    function lensBuild(){
      if(LZ.el) return;
      var e = document.createElement("div"); e.className = "sg3d-lens"; e.setAttribute("aria-hidden", "true");
      e.innerHTML = '<div class="sg3d-lens-map"></div><i class="sg3d-lens-x"></i><span class="sg3d-lens-z"></span>';
      host.appendChild(e); LZ.el = e;
      try{
        LZ.ml = new maplibregl.Map({container:e.querySelector(".sg3d-lens-map"), style:style(), interactive:false, attributionControl:false, fadeDuration:0, antialias:false,
          center:R.ml.getCenter(), zoom:R.ml.getZoom(), pitch:R.ml.getPitch(), bearing:R.ml.getBearing(), maxZoom:21, maxPitch:75});
        LZ.ml.once("style.load", function(){ overlays(LZ.ml); LZ.ready = true;
          LZ.ml.setLayoutProperty("building-3d", "visibility", R.bld ? "visible" : "none"); LZ.ml.setLayoutProperty("building-flat", "visibility", R.bld ? "none" : "visible"); lensData(); });
      }catch(err){ LZ.ml = null; }
    }
    function lensData(){
      if(!LZ.ml || !LZ.ready || !R.fc) return;
      Object.keys(R.fc).forEach(function(k){ var s_ = LZ.ml.getSource(k); if(s_) s_.setData(R.fc[k]); });
      LZ.mk.forEach(function(m){ m.remove(); }); LZ.mk = [];
      Object.keys(R.mk).forEach(function(id){ var cur = R.mk[id], e2 = cur.m.getElement().cloneNode(true); e2.style.pointerEvents = "none";
        LZ.mk.push(new maplibregl.Marker(Object.assign({element:e2}, cur.mo)).setLngLat(cur.m.getLngLat()).addTo(LZ.ml)); });
    }
    function lensMove(cx, cy){
      var r = el.getBoundingClientRect(), x = Math.max(0, Math.min(r.width, cx - r.left)), y = Math.max(0, Math.min(r.height, cy - r.top)), lx = x, ly_ = y;
      if(LZ.touch){ ly_ = y - 120 < 95 ? y + 120 : y - 120; lx = Math.max(70, Math.min(r.width - 70, x)); }
      LZ.el.style.left = lx + "px"; LZ.el.style.top = ly_ + "px";
      LZ.ll = R.ml.unproject([x, y]);
      if(LZ.ml) LZ.ml.jumpTo({center:LZ.ll, zoom:Math.min(21, R.ml.getZoom() + 2), pitch:R.ml.getPitch(), bearing:R.ml.getBearing()});
      var can = !opts.lens.can || opts.lens.can();
      LZ.el.querySelector(".sg3d-lens-z").textContent = "\u00d74 \u00b7 " + (typeof opts.lens.label === "function" ? opts.lens.label() : (can ? "let go to pick" : "view only"));
    }
    var HANDLERS = ["dragPan", "dragRotate", "touchZoomRotate", "touchPitch"];
    function lensStart(){
      LZ.t = null; if(!R.on || !R.ready) return;
      LZ.on = true; lensBuild();
      HANDLERS.forEach(function(h){ if(R.ml[h] && R.ml[h].disable) R.ml[h].disable(); });
      LZ.el.classList.add("on"); if(LZ.ml) LZ.ml.resize(); lensData(); lensMove(LZ.x0, LZ.y0); tipHide();
      if(LZ.touch && navigator.vibrate) try{ navigator.vibrate(12); }catch(err){}
    }
    function lensCancel(){ if(LZ.t){ clearTimeout(LZ.t); LZ.t = null; } if(!LZ.on) LZ.id = null; }
    function lensEnd(pick){
      lensCancel(); if(!LZ.on) return;
      LZ.on = false; LZ.id = null; R.noClick = Date.now() + 700;
      HANDLERS.forEach(function(h){ if(R.ml[h] && R.ml[h].enable) R.ml[h].enable(); });
      LZ.el.classList.remove("on");
      if(pick && LZ.ll && (!opts.lens.can || opts.lens.can())){
        var ll = L.latLng(LZ.ll.lat, LZ.ll.lng);
        if(typeof opts.lens.pick === "function") opts.lens.pick(ll);
        else map.fire("click", {latlng:ll, layerPoint:map.latLngToLayerPoint(ll), containerPoint:map.latLngToContainerPoint(ll), originalEvent:{}});
      }
    }
    if(opts.lens){
      el.addEventListener("pointerdown", function(e){
        if(!R.on || !R.ready) return;
        if(LZ.on){ lensEnd(false); return; }                             // a second finger (pinch): stop
        if(LZ.t){ lensCancel(); return; }
        if(!e.isPrimary || (e.pointerType === "mouse" && (e.button !== 0 || e.ctrlKey))) return;
        if(e.target.closest && e.target.closest(".sg3dm, .maplibregl-ctrl, .maplibregl-popup, .maplibregl-marker")) return;   // not the blockage X, handles, buttons
        LZ.id = e.pointerId; LZ.x0 = e.clientX; LZ.y0 = e.clientY; LZ.touch = e.pointerType !== "mouse";
        lensBuild();                                                     // built (and its tiles loading) while the finger is held
        if(LZ.ml) try{ var r0 = el.getBoundingClientRect(); LZ.ml.jumpTo({center:R.ml.unproject([e.clientX - r0.left, e.clientY - r0.top]), zoom:Math.min(21, R.ml.getZoom() + 2), pitch:R.ml.getPitch(), bearing:R.ml.getBearing()}); }catch(err){}
        LZ.t = setTimeout(lensStart, opts.lens.hold || 400);
      }, true);
      window.addEventListener("pointermove", function(e){
        if(e.pointerId !== LZ.id) return;
        if(LZ.on){ e.preventDefault(); lensMove(e.clientX, e.clientY); }
        else if(LZ.t && Math.hypot(e.clientX - LZ.x0, e.clientY - LZ.y0) > 8) lensCancel();   // moving straight away: a normal 3D pan
      }, true);
      window.addEventListener("pointerup", function(e){ if(e.pointerId !== LZ.id) return; if(LZ.on) lensEnd(true); else lensCancel(); }, true);
      window.addEventListener("pointercancel", function(e){ if(e.pointerId !== LZ.id) return; if(LZ.on) lensEnd(false); else lensCancel(); }, true);
      el.addEventListener("contextmenu", function(e){ if(LZ.on || LZ.t) e.preventDefault(); }, true);
      document.addEventListener("keydown", function(e){ if(e.key === "Escape" && LZ.on) lensEnd(false); });
    }
    btn.addEventListener("click", function(){ set(!R.on); });
    setBtn(); applyOpt();
    var ctl = {set:set, state:R, sync:sync, map:map, host:host, button:btn, lens:LZ};
    map._sg3d = ctl; SG3D.list.push(ctl);
    if(ls(opts.key) === "1") setTimeout(function(){ set(true); }, 400);  // the controller's last choice on this map
    return ctl;
  }

  function auto(id, opts){ CFG[id] = opts || {}; }
  L.Map.addInitHook(function(){                                          // maps created later, also inside a page's private code
    var m = this, c = m.getContainer && m.getContainer(), o = c && c.id && CFG[c.id];
    if(o) setTimeout(function(){ if(!m._sg3d) attach(m, o); }, 0);
  });
  var SG3D = window.SG3D = {attach:attach, auto:auto, load:load, box:box, list:[]};
})();
