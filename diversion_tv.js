/* SG Transport Pulse - TV broadcast of live diversions (V16.27). Served at /diversion-tv.js for /diversion/tv.
   Every diversion whose period covers now (saved in the planner's step 7) is shown in turn, one route at a time:
     1. overview of the closure and the diversion (3D),
     2. a bus drives the diversion while the camera follows it and the route ribbon fills road by road,
     3. overview again, then the next one.
   It refreshes itself every 30 s, keeps showing the last data when the network drops, and needs no keyboard or mouse.
   3D: MapLibre GL 4.7.1 + OpenFreeMap vector tiles (no key). A TV without WebGL gets a 2D map (Leaflet + CARTO) with the same
   animation. Link options: ?case=ID preview one saved record, ?lite=1 flat buildings (slow TV), ?2d=1 force 2D,
   ?speed=N run N times faster (demos). Keys: F full screen, -> next, <- previous, space pause. */
(function(){
  "use strict";
  var Q = new URLSearchParams(location.search), PREVIEW = +(Q.get("case") || 0) || 0, LITE = Q.get("lite") === "1", FORCE2D = Q.get("2d") === "1";
  var SPEED = Math.max(.25, Math.min(40, +(Q.get("speed") || 1) || 1));
  var REDUCE = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  var ML = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl", LF = "https://unpkg.com/leaflet@1.9.4/dist/leaflet", TILES = "https://tiles.openfreemap.org";
  var KX = 111320 * Math.cos(1.35 * Math.PI / 180), KY = 110574;
  var $ = function(id){ return document.getElementById(id); };
  function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[c]; }); }
  function rem(){ return parseFloat(getComputedStyle(document.documentElement).fontSize) || 16; }
  function px(n){ return n * rem() / 16; }

  /* ---------------------------------------------------------------- geometry (metres, local flat projection) */
  function dm(a, b){ return Math.hypot((a[1] - b[1]) * KX, (a[0] - b[0]) * KY); }
  function cum(l){ var c = [0]; for(var i = 1; i < l.length; i++) c.push(c[i - 1] + dm(l[i - 1], l[i])); return c; }
  function at(l, c, s){
    var n = c.length - 1; if(s <= 0) return l[0]; if(s >= c[n]) return l[n];
    var lo = 0, hi = n; while(hi - lo > 1){ var m = (lo + hi) >> 1; if(c[m] <= s) lo = m; else hi = m; }
    var f = c[hi] === c[lo] ? 0 : (s - c[lo]) / (c[hi] - c[lo]); return [l[lo][0] + (l[hi][0] - l[lo][0]) * f, l[lo][1] + (l[hi][1] - l[lo][1]) * f];
  }
  function brg(a, b){ return (Math.atan2((b[1] - a[1]) * KX, (b[0] - a[0]) * KY) * 180 / Math.PI + 360) % 360; }
  function adiff(a, b){ return ((a - b + 540) % 360) - 180; }
  function proj(p, l, c, lo, hi){                                        // nearest position on l between lo and hi (m) -> {d, s}
    lo = lo == null ? -1 : lo; hi = hi == null ? 1e12 : hi; var best = {d:1e18, s:0};
    for(var i = 0; i < l.length - 1; i++){
      if(c[i + 1] < lo || c[i] > hi) continue;
      var ax = l[i][1] * KX, ay = l[i][0] * KY, bx = l[i + 1][1] * KX, by = l[i + 1][0] * KY, qx = p[1] * KX, qy = p[0] * KY, dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      var t = L2 < 1e-9 ? 0 : Math.max(0, Math.min(1, ((qx - ax) * dx + (qy - ay) * dy) / L2)), s = c[i] + t * (c[i + 1] - c[i]);
      if(s < lo || s > hi){ s = Math.max(lo, Math.min(hi, s)); t = c[i + 1] > c[i] ? (s - c[i]) / (c[i + 1] - c[i]) : 0; }
      var d = Math.hypot(qx - (ax + t * dx), qy - (ay + t * dy)); if(d < best.d) best = {d:d, s:s};
    }
    return best;
  }
  function cut(l, c, a, b){ var o = [at(l, c, a)]; for(var i = 0; i < l.length; i++) if(c[i] > a && c[i] < b) o.push(l[i]); o.push(at(l, c, b)); return o; }
  function pts(v){ return (v || []).filter(function(p){ return p && p.length >= 2 && isFinite(+p[0]) && isFinite(+p[1]) && (+p[0] || +p[1]); }).map(function(p){ return [+p[0], +p[1]]; }); }
  function offset(p, h, fwd, side){                                      // a point fwd m ahead and side m to the right of p (heading h)
    var r = h * Math.PI / 180, e = Math.sin(r) * fwd + Math.cos(r) * side, n = Math.cos(r) * fwd - Math.sin(r) * side;
    return [p[0] + n / KY, p[1] + e / KX];
  }
  function band(l, half){                                                // a polygon half m either side of a line (the raised closure)
    if(l.length < 2) return null;
    var L_ = [], R_ = [];
    for(var i = 0; i < l.length; i++){ var h = brg(l[Math.max(0, i - 1)], l[Math.min(l.length - 1, i + 1)]); L_.push(offset(l[i], h, 0, -half)); R_.push(offset(l[i], h, 0, half)); }
    var ring = L_.concat(R_.reverse()); ring.push(ring[0]);
    return {type:"Feature", properties:{}, geometry:{type:"Polygon", coordinates:[ring.map(function(p){ return [p[1], p[0]]; })]}};
  }

  /* ---------------------------------------------------------------- road names, as OCC writes them (LTA short forms) */
  var ABR = [[/\bAvenue\b/g, "Ave"], [/\bRoad\b/g, "Rd"], [/\bStreet\b/g, "St"], [/\bDrive\b/g, "Dr"], [/\bCrescent\b/g, "Cres"], [/\bExpressway\b/g, "Expwy"],
    [/\bBoulevard\b/g, "Blvd"], [/\bCentral\b/g, "Ctrl"], [/\bPlace\b/g, "Pl"], [/\bLane\b/g, "Ln"], [/\bTerrace\b/g, "Ter"], [/\bClose\b/g, "Cl"], [/\bSquare\b/g, "Sq"],
    [/\bGardens?\b/g, "Gdns"], [/\bHeights\b/g, "Hts"], [/\bIndustrial\b/g, "Ind"], [/\bJunction\b/g, "Jct"], [/\bInterchange\b/g, "Int"], [/\bStation\b/g, "Stn"],
    [/\bUpper\b/g, "Upp"], [/\bNorth\b/g, "Nth"], [/\bSouth\b/g, "Sth"], [/\bCommonwealth\b/g, "C'wealth"], [/\bBukit\b/g, "Bt"], [/\bJalan\b/g, "Jln"],
    [/\bLorong\b/g, "Lor"], [/\bTanjong\b/g, "Tg"], [/\bKampong\b/g, "Kg"], [/\bMount\b/g, "Mt"]];
  function tc(s){ s = String(s || ""); return s === s.toUpperCase() ? s.toLowerCase().replace(/\b([a-z])/g, function(m){ return m.toUpperCase(); }) : s; }
  function abbr(r){ var t = tc(r).trim(); ABR.forEach(function(a){ t = t.replace(a[0], a[1]); }); return t; }
  function dist(m){ return m < 950 ? Math.max(10, Math.round(m / 10) * 10) + " m" : (m / 1000).toFixed(1) + " km"; }
  function turnWord(t){ t = String(t || "").toUpperCase(); if(!t) return "x"; return /U-?TURN/.test(t) ? "u" : /LEFT/.test(t) ? (/SLIGHT|KEEP|BEAR/.test(t) ? "sl" : "l") : /RIGHT/.test(t) ? (/SLIGHT|KEEP|BEAR/.test(t) ? "sr" : "r") : "s"; }
  var TURN_SVG = {l:'<path d="M15 20v-7a3 3 0 0 0-3-3H6m3-4L5 10l4 4"/>', r:'<path d="M9 20v-7a3 3 0 0 1 3-3h6m-3-4 4 4-4 4"/>', sl:'<path d="M14 20v-6L8 8m0 5V8h5"/>',
    sr:'<path d="M10 20v-6l6-6m0 5V8h-5"/>', s:'<path d="M12 20V5m-4 4 4-4 4 4"/>', u:'<path d="M8 20V10a4 4 0 0 1 8 0v4m-3-3 3 3 3-3"/>',
    x:'<circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>', ok:'<path d="M5 12.5l4.5 4.5L19 7.5"/>'};
  function turnSvg(k){ return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">' + (TURN_SVG[k] || TURN_SVG.s) + '</svg>'; }
  function turnTxt(k){ return {l:"Turn left into", r:"Turn right into", sl:"Keep left into", sr:"Keep right into", s:"Continue into", u:"U-turn into", x:"Then"}[k] || "Then"; }

  /* ---------------------------------------------------------------- a slide = one route of one live diversion */
  function prep(c, r, i, n){
    var line = pts(r.line), lc = cum(line), lineM = lc[lc.length - 1], orig = pts(r.orig), oc = orig.length > 1 ? cum(orig) : null;
    var lead = [], tail = [];
    if(oc){                                                              // a little of the normal route before and after: where it leaves and comes back
      var pl = proj(line[0], orig, oc), pr = proj(line[line.length - 1], orig, oc, pl.s + 1);
      if(pl.d < 60 && pl.s > 20) lead = cut(orig, oc, Math.max(0, pl.s - 380), pl.s);
      if(pr.d < 60 && pr.s < oc[oc.length - 1] - 20) tail = cut(orig, oc, pr.s, Math.min(oc[oc.length - 1], pr.s + 280));
    }
    var leadM = lead.length > 1 ? cum(lead)[lead.length - 1] : 0;
    var path = (lead.length > 1 ? lead.slice(0, -1) : []).concat(line).concat(tail.length > 1 ? tail.slice(1) : []);
    var pc = cum(path), total = pc[pc.length - 1];
    var segs = [], legs = (r.legs || []).filter(function(x){ return x && x.road; });
    function add(kind, name, a, b, turn){
      name = abbr(name); if(/^\(?unnamed/i.test(name)) name = "";
      var last = segs[segs.length - 1];
      if(last && (last.name === name || !name)){ last.b = Math.max(last.b, b); if(kind === "div") last.kind = "div"; return; }
      segs.push({kind:kind, name:name, a:last ? last.b : a, b:Math.max(b, last ? last.b : a), turn:turnWord(turn)});     // no gaps between roads
    }
    var back = r.back_road || "", from = r.from_road || "";
    if(legs.length){
      add("route", from || legs[0].road, 0, leadM, "");
      legs.forEach(function(x, k){
        var a = leadM + Math.max(0, Math.min(lineM, +x.s || 0)), b = k + 1 < legs.length ? leadM + Math.max(0, Math.min(lineM, +legs[k + 1].s || 0)) : leadM + lineM;
        add(k === legs.length - 1 && back && abbr(x.road) === abbr(back) ? "route" : "div", x.road, a, b, x.turn);
      });
      if(back && abbr(back) !== (segs[segs.length - 1] || {}).name) add("route", back, leadM + lineM, total, "");
      else segs[segs.length - 1].b = total;
    } else {                                                             // no road-by-road record (e.g. an older case): the names share the route evenly
      var nm = (r.text && /\s>\s/.test(r.text) ? r.text.replace(/\s*\(Back on route\)\s*$/i, "").split(/\s*>\s*/) : (r.roads || []).map(abbr)).filter(Boolean);
      if(!nm.length) nm = ["Diversion"];
      var first = nm.length > 2 ? nm[0] : null, lastN = nm.length > 2 ? nm[nm.length - 1] : null, mids = nm.length > 2 ? nm.slice(1, -1) : nm;
      if(first) add("route", first, 0, leadM, "");
      mids.forEach(function(x, k){ add("div", x, leadM + lineM * k / mids.length, leadM + lineM * (k + 1) / mids.length, ""); });
      if(lastN) add("route", lastN, leadM + lineM, total, "");
      else segs[segs.length - 1].b = total;
    }
    segs.forEach(function(sg, k){ sg.i = k; if(!sg.name) sg.name = "Unnamed road"; });
    var blocks = (c.blocks || []).map(function(b){ return pts(b.line); }).filter(function(l){ return l.length > 1; });
    var all = path.concat.apply(path, blocks), skip = pts(r.skip_line);
    return {key:c.id + ":" + (r.n || i + 1), c:c, r:r, i:i, n:n, line:line, lineM:lineM, leadM:leadM, path:path, pc:pc, total:Math.max(1, total), segs:segs, blocks:blocks, all:all,
            orig:orig, skip:skip, bear:(brg(path[0], path[path.length - 1]) + 270) % 360, services:uniq(r.services || []),     // overview: the bus travels left to right, like the ribbon
            dIntro:5.5, dDrive:Math.max(14, Math.min(32, 12 + total / 1000 * 6)), dOutro:4.5};
  }
  function uniq(a){ return a.filter(function(x, i){ return a.indexOf(x) === i; }); }
  function slidesOf(cases){
    var out = [];
    (cases || []).forEach(function(c){
      var rs = (c.routes || []).filter(function(r){ return pts(r.line).length >= 2; });
      rs.forEach(function(r, i){ try{ out.push(prep(c, r, i, rs.length)); }catch(e){ if(window.console) console.warn("TV: route skipped", e); } });
    });
    return out;
  }

  /* ---------------------------------------------------------------- 3D map (MapLibre) */
  function load(src, css){
    return new Promise(function(res, rej){
      if(css){ var l = document.createElement("link"); l.rel = "stylesheet"; l.href = css; document.head.appendChild(l); }
      var s = document.createElement("script"); s.src = src; s.async = true; s.onload = function(){ res(); }; s.onerror = function(){ rej(new Error("could not load " + src)); };
      document.head.appendChild(s); setTimeout(function(){ rej(new Error("timeout " + src)); }, 20000);
    });
  }
  function webgl(){ try{ var c = document.createElement("canvas"); return !!(c.getContext("webgl2") || c.getContext("webgl")); }catch(e){ return false; } }
  var EMPTY = {type:"FeatureCollection", features:[]};
  function lineF(l){ return {type:"Feature", properties:{}, geometry:{type:"LineString", coordinates:l.map(function(p){ return [p[1], p[0]]; })}}; }
  function fc(fs){ return {type:"FeatureCollection", features:fs.filter(Boolean)}; }
  function grad(f){
    var A = "#7dffc8", B = "rgba(46,229,157,0.5)";
    if(f <= .001) return ["interpolate", ["linear"], ["line-progress"], 0, B, 1, B];
    if(f >= .999) return ["interpolate", ["linear"], ["line-progress"], 0, A, 1, A];
    return ["interpolate", ["linear"], ["line-progress"], 0, A, f, A, Math.min(.9999, f + .0008), B, 1, B];
  }
  function R3(){
    var ml, hS = null, marks = [], lastF = -1, R = {kind:"3d", ready:false};
    function style(){
      var name = ["coalesce", ["get", "name:latin"], ["get", "name_en"], ["get", "name"]];
      var lines = ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false], polys = ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
      var road = function(id, classes, color, w0, w1, minz){ return {id:id, type:"line", source:"omt", "source-layer":"transportation", minzoom:minz,
        filter:["all", lines, ["match", ["get", "class"], classes, true, false], ["!=", ["get", "brunnel"], "tunnel"]],
        layout:{"line-cap":"round", "line-join":"round"}, paint:{"line-color":color, "line-width":["interpolate", ["exponential", 1.5], ["zoom"], 10, w0, 18, w1]}}; };
      return {version:8, glyphs:TILES + "/fonts/{fontstack}/{range}.pbf", sources:{omt:{type:"vector", url:TILES + "/planet"}},
        layers:[
          {id:"bg", type:"background", paint:{"background-color":"#081428"}},
          {id:"park", type:"fill", source:"omt", "source-layer":"landuse", filter:["all", polys, ["match", ["get", "class"], ["park", "cemetery", "grass"], true, false]], paint:{"fill-color":"#0b2530", "fill-opacity":.7}},
          {id:"wood", type:"fill", source:"omt", "source-layer":"landcover", filter:["all", polys, ["match", ["get", "class"], ["wood", "grass", "wetland"], true, false]], paint:{"fill-color":"#0a2329", "fill-opacity":.6}},
          {id:"water", type:"fill", source:"omt", "source-layer":"water", filter:["all", polys, ["!=", ["get", "brunnel"], "tunnel"]], paint:{"fill-color":"#04193a"}},
          road("road-minor", ["minor", "service", "track"], "#172a49", .3, 8, 12),
          road("road-major", ["primary", "secondary", "tertiary", "trunk"], "#24406b", .8, 17, 9),
          road("road-motorway", ["motorway"], "#2d4f86", 1, 21, 8),
          {id:"building-flat", type:"fill", source:"omt", "source-layer":"building", minzoom:13, layout:{visibility:LITE ? "visible" : "none"}, paint:{"fill-color":"#14264a", "fill-outline-color":"#22396a"}},
          {id:"building-3d", type:"fill-extrusion", source:"omt", "source-layer":"building", minzoom:13, filter:["!", ["has", "hide_3d"]], layout:{visibility:LITE ? "none" : "visible"},
            paint:{"fill-extrusion-color":["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 8], 0, "#182a50", 30, "#203766", 80, "#294680", 200, "#365c9e"],
                   "fill-extrusion-height":["interpolate", ["linear"], ["zoom"], 13, 0, 14, ["coalesce", ["get", "render_height"], 8]],
                   "fill-extrusion-base":["interpolate", ["linear"], ["zoom"], 13, 0, 14, ["coalesce", ["get", "render_min_height"], 0]],
                   "fill-extrusion-opacity":.78, "fill-extrusion-vertical-gradient":true}},
          {id:"road-label", type:"symbol", source:"omt", "source-layer":"transportation_name", minzoom:14, filter:lines,
            layout:{"symbol-placement":"line", "symbol-spacing":420, "text-field":name, "text-font":["Noto Sans Regular"], "text-size":px(15), "text-max-angle":30},
            paint:{"text-color":"#b4c8e4", "text-halo-color":"#081428", "text-halo-width":1.6}}
        ]};
    }
    function set(id, d){ var s = ml.getSource(id); if(s) s.setData(d); }
    function layers(){
      ["orig", "skipl", "block", "wall"].forEach(function(id){ ml.addSource(id, {type:"geojson", data:EMPTY}); });
      ml.addSource("div", {type:"geojson", data:EMPTY, lineMetrics:true});
      var below = "road-label";
      ml.addLayer({id:"orig", type:"line", source:"orig", layout:{"line-cap":"round", "line-join":"round"}, paint:{"line-color":"#38d6ff", "line-width":px(7), "line-opacity":.6}}, below);
      ml.addLayer({id:"skipl", type:"line", source:"skipl", paint:{"line-color":"#ffb547", "line-width":px(7), "line-dasharray":[1.2, 1.1]}}, below);
      ml.addLayer({id:"div-glow", type:"line", source:"div", layout:{"line-cap":"round", "line-join":"round"}, paint:{"line-color":"#2ee59d", "line-width":px(28), "line-blur":px(16), "line-opacity":.42}}, below);
      ml.addLayer({id:"div", type:"line", source:"div", layout:{"line-cap":"round", "line-join":"round"}, paint:{"line-width":px(10), "line-gradient":grad(0)}}, below);
      ml.addLayer({id:"block", type:"line", source:"block", layout:{"line-cap":"round"}, paint:{"line-color":"#ff3b52", "line-width":px(13)}}, below);
      ml.addLayer({id:"wall", type:"fill-extrusion", source:"wall", paint:{"fill-extrusion-color":"#ff3b52", "fill-extrusion-height":18, "fill-extrusion-base":0, "fill-extrusion-opacity":.72}});
    }
    var busM = null;
    function busMarker(){ if(busM) return busM; var el = document.createElement("div"); el.className = "tvbus";
      busM = new maplibregl.Marker({element:el, rotationAlignment:"map", pitchAlignment:"map"}).setLngLat([103.82, 1.35]); return busM; }
    function mark(p, cls, html, anchor){
      var el = document.createElement("div"); el.className = "mk " + cls; el.innerHTML = html;
      marks.push(new maplibregl.Marker({element:el, anchor:anchor || "bottom"}).setLngLat([p[1], p[0]]).addTo(ml));
    }
    R.init = function(){
      return load(ML + ".js", ML + ".css").then(function(){
        return new Promise(function(res, rej){
          ml = R.ml = new maplibregl.Map({container:"map", style:style(), center:[103.82, 1.35], zoom:12.5, pitch:50, bearing:0, interactive:false, maxPitch:70, fadeDuration:0,
            attributionControl:{compact:true}, antialias:!LITE, pixelRatio:Math.min(window.devicePixelRatio || 1, LITE ? 1 : 1.5)});
          ml.on("load", function(){ layers(); R.ready = true; res(); });
          setTimeout(function(){ if(!R.ready) rej(new Error("the 3D map did not start")); }, 25000);
        });
      });
    };
    R.show = function(sl){
      set("orig", sl.orig.length > 1 ? lineF(sl.orig) : EMPTY); set("skipl", sl.skip.length > 1 ? lineF(sl.skip) : EMPTY); set("div", lineF(sl.line));
      set("block", fc(sl.blocks.map(lineF))); set("wall", fc(sl.blocks.map(function(l){ return band(l, 2.6); })));
      busMarker().remove(); lastF = -1; ml.setPaintProperty("div", "line-gradient", grad(0));
      marks.forEach(function(m){ m.remove(); }); marks = [];
      var c = sl.c, r = sl.r;
      sl.blocks.forEach(function(l, k){ var lc = cum(l); if(!k) mark(at(l, lc, lc[lc.length - 1] / 2), "closed", "Road closed" + (c.dir_txt ? "<small>" + esc(c.dir_txt) + "</small>" : "")); });
      mark(sl.line[0], "leave", "Leaves route" + (r.leave && r.leave.code ? "<small>after BS " + esc(r.leave.code) + "</small>" : ""), "right");
      mark(sl.line[sl.line.length - 1], "back", "Back on route" + (r.rejoin && r.rejoin.code ? "<small>at BS " + esc(r.rejoin.code) + "</small>" : ""), "left");
      (r.skipped || []).forEach(function(s){ if(s.lat && s.lon) mark([s.lat, s.lon], "skip", esc(s.code), "center"); });
    };
    R.clear = function(){ ["orig", "skipl", "div", "block", "wall"].forEach(function(id){ set(id, EMPTY); }); marks.forEach(function(m){ m.remove(); }); marks = []; busMarker().remove(); };
    R.overview = function(sl, ms){
      var b = new maplibregl.LngLatBounds(); sl.all.forEach(function(p){ b.extend([p[1], p[0]]); });
      var pad = 5 * rem(), cam = ml.cameraForBounds(b, {padding:{top:pad * 1.3, bottom:pad * 1.4, left:pad, right:pad}, bearing:sl.bear});
      if(!cam) return;
      ml.flyTo({center:cam.center, zoom:Math.min(17.2, cam.zoom - .25), bearing:sl.bear, pitch:REDUCE ? 0 : 50, duration:REDUCE ? 0 : ms, essential:true});
    };
    R.approach = function(p, h, ahead, ms){ hS = h; ml.flyTo({center:[ahead[1], ahead[0]], bearing:h, pitch:57, zoom:16.6, duration:ms, essential:true}); };
    R.follow = function(p, h, ahead, dt){
      if(hS == null) hS = h;
      hS = (hS + adiff(h, hS) * (1 - Math.exp(-dt * 2.4)) + 360) % 360;
      ml.jumpTo({center:[ahead[1], ahead[0]], bearing:hS, pitch:57, zoom:16.6});
    };
    R.bus = function(p, h, f, sl){
      var b = busMarker(); b.setLngLat([p[1], p[0]]).setRotation(h); if(!b._map) b.addTo(ml);
      var q = Math.round(f * 400) / 400; if(q !== lastF){ lastF = q; ml.setPaintProperty("div", "line-gradient", grad(q)); }
    };
    R.idle = function(dt){ ml.jumpTo({center:[103.818, 1.338], zoom:12.8, pitch:58, bearing:(ml.getBearing() + dt * 2.5) % 360}); };
    R.project = function(p){ var q = ml.project([p[1], p[0]]); return {x:q.x, y:q.y}; };
    R.lite = function(){ ml.setLayoutProperty("building-3d", "visibility", "none"); ml.setLayoutProperty("building-flat", "visibility", "visible"); };
    return R;
  }

  /* ---------------------------------------------------------------- 2D map (Leaflet) - for a TV without WebGL */
  function R2(){
    var map, G, bus, done, todo, R = {kind:"2d", ready:false};
    R.init = function(){
      return load(LF + ".js", LF + ".css").then(function(){ return load("/basemap.js").catch(function(){}); }).then(function(){
        map = R.map = L.map("map", {zoomControl:false, dragging:false, scrollWheelZoom:false, doubleClickZoom:false, boxZoom:false, keyboard:false, touchZoom:false, zoomSnap:.25, fadeAnimation:false})
          .setView([1.35, 103.82], 12);
        if(window.SGBasemap) SGBasemap.add(map, {style:"dark", switcher:false});        // the platform's basemap (CARTO with its key, OneMap, OpenStreetMap)
        else L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {subdomains:"abcd", maxZoom:20, attribution:"&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);
        G = L.layerGroup().addTo(map); R.ready = true;
      });
    };
    var TF = {closed:"translate(-50%,-100%)", leave:"translate(-100%,-50%)", back:"translate(0,-50%)"};
    function mk(p, cls, html){ return L.marker(p, {interactive:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="mk ' + cls + '" style="display:inline-block;transform:' + (TF[cls] || "translate(-50%,-50%)") + '">' + html + '</div>'})}).addTo(G); }
    R.show = function(sl){
      G.clearLayers(); var w = px(1);
      if(sl.orig.length > 1) L.polyline(sl.orig, {color:"#38d6ff", weight:7 * w, opacity:.6, interactive:false}).addTo(G);
      if(sl.skip.length > 1) L.polyline(sl.skip, {color:"#ffb547", weight:6 * w, dashArray:"10 9", interactive:false}).addTo(G);
      L.polyline(sl.line, {color:"#2ee59d", weight:22 * w, opacity:.18, interactive:false}).addTo(G);
      todo = L.polyline(sl.line, {color:"#2ee59d", weight:9 * w, opacity:.55, interactive:false}).addTo(G);
      done = L.polyline([sl.line[0], sl.line[0]], {color:"#9dffd3", weight:9 * w, interactive:false}).addTo(G);
      sl.blocks.forEach(function(l){ L.polyline(l, {color:"#ff3b52", weight:12 * w, interactive:false}).addTo(G); });
      if(sl.blocks.length){ var l0 = sl.blocks[0], c0 = cum(l0); mk(at(l0, c0, c0[c0.length - 1] / 2), "closed", "Road closed"); }
      mk(sl.line[0], "leave", "Leaves route"); mk(sl.line[sl.line.length - 1], "back", "Back on route");
      bus = L.marker(sl.path[0], {interactive:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="lf-bus" style="transform:translate(-50%,-50%)"><div class="tvbus"></div></div>'})}).addTo(G);
    };
    R.clear = function(){ G.clearLayers(); };
    R.overview = function(sl, ms){ var pad = 5 * rem(); map.flyToBounds(sl.all, {padding:[pad, pad], duration:REDUCE ? 0 : ms / 1000, animate:!REDUCE}); };
    R.approach = function(p, h, ahead, ms){ map.flyTo(ahead, 17, {duration:ms / 1000}); };
    R.follow = function(p, h, ahead){ map.setView(ahead, 17, {animate:false}); };
    R.bus = function(p, h, f, sl){
      if(bus){ bus.setLatLng(p); var e = bus.getElement() && bus.getElement().querySelector(".tvbus"); if(e) e.style.transform = "rotate(" + Math.round(h) + "deg)"; }
      if(done){ var lc = cum(sl.line); done.setLatLngs(cut(sl.line, lc, 0, Math.max(.5, f * lc[lc.length - 1]))); }
    };
    R.idle = function(){};
    R.project = function(p){ var q = map.latLngToContainerPoint(p); return {x:q.x, y:q.y}; };
    R.lite = function(){};
    return R;
  }

  /* ---------------------------------------------------------------- state, data, clock */
  var T = {slides:[], i:0, sl:null, phase:"none", t:0, slideT:0, last:0, skew:0, live:[], up:[], okAt:0, failN:0, sig:"", paused:false, frames:[], liteDone:false, everOk:false};
  var R = null;
  function now(){ return Date.now() + T.skew; }
  function sgt(ms){ return new Date(ms + 8 * 3600e3); }
  function p2(n){ return (n < 10 ? "0" : "") + n; }
  var MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" "), DAY = "Sun Mon Tue Wed Thu Fri Sat".split(" ");
  function hm(ts){ var d = sgt(ts * 1000); return p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes()); }
  function dayOf(ts){ var d = sgt(ts * 1000); return d.getUTCDate() + " " + MON[d.getUTCMonth()]; }
  function sameDay(ts){ return dayOf(ts) === dayOf(now() / 1000); }
  function whenTxt(ts){ return (sameDay(ts) ? "" : dayOf(ts) + " ") + hm(ts); }
  function left(ms){ var m = Math.max(0, Math.round(ms / 60000)), h = Math.floor(m / 60); return h ? h + " h " + p2(m % 60) + " min left" : m + " min left"; }

  function clock(){
    var d = sgt(now());
    $("clk").textContent = p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes());
    $("date").textContent = DAY[d.getUTCDay()] + " " + d.getUTCDate() + " " + MON[d.getUTCMonth()] + " " + d.getUTCFullYear();
    if(T.sl) paintWhen(T.sl);
  }
  function poll(){
    var url = "/api/dv2/live" + (PREVIEW ? "?case=" + PREVIEW : "");
    return fetch(url, {credentials:"same-origin", cache:"no-store"}).then(function(r){
      if(r.status === 401) throw new Error("login");
      if(!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function(j){
      T.failN = 0; T.okAt = Date.now(); T.everOk = true; if(j.now) T.skew = j.now * 1000 - Date.now();
      $("off").hidden = true; apply(j);
    }).catch(function(e){
      T.failN++;
      var login = String(e && e.message) === "login";
      $("off").hidden = false;
      $("off").textContent = login ? "Sign-in needed: open this page once with the TV key (DIVERSION_TV_KEY)" : T.okAt ? "Connection lost: showing the update from " + hm(T.okAt / 1000 + T.skew / 1000) : "Cannot reach the server yet";
      if(!T.everOk){ $("empty").hidden = false; $("emptyP").textContent = login ? "This TV is not signed in. Open /diversion/tv?key=… once (the key is DIVERSION_TV_KEY in the server settings)." : "Waiting for the server…"; }
    });
  }
  function apply(j){
    T.live = j.live || []; T.up = j.upcoming || []; T.pv = !!j.preview;
    $("pv").hidden = !T.pv; $("title").textContent = T.pv ? "Diversion preview" : "Live diversions";
    var n = T.live.length, cnt = $("cnt");
    cnt.classList.toggle("zero", !n || T.pv); $("cntT").textContent = T.pv ? (T.live[0] ? (T.live[0].state === "live" ? "Live now" : T.live[0].state === "upcoming" ? "Not started" : "Not live") : "Not found") : n ? n + " live" : "None live";
    var slides = slidesOf(T.live), sig = slides.map(function(s){ return s.key; }).join("|");
    var cur = T.sl && slides.filter(function(s){ return s.key === T.sl.key; })[0];
    T.slides = slides;
    if(cur){ cur.slideT = T.sl.slideT; T.sl.c = cur.c; T.i = slides.indexOf(cur); paintSide(T.sl); }
    if(sig !== T.sig){
      T.sig = sig;
      if(!slides.length){ T.sl = null; T.phase = "none"; if(R && R.ready) R.clear(); $("nav").hidden = true; }
      else if(!cur){ start(0); }
    }
    $("empty").hidden = !!slides.length;
    if(!slides.length) $("emptyP").textContent = T.pv ? "This saved record has no route to show." : "No diversion is live right now." + (T.up.length ? " Next one starts " + whenTxt(T.up[0].start_ts) + "." : "");
    pips(); foot();
  }

  /* ---------------------------------------------------------------- the side panel, the ribbon, the footer */
  function paintSide(sl){
    var c = sl.c, r = sl.r;
    $("road").textContent = c.blocked || c.road || "";
    $("dir").textContent = c.dir_txt || "";
    $("leds").innerHTML = sl.services.map(function(s){ return '<span class="led"><b>' + esc(s) + '</b></span>'; }).join("");
    $("routeOf").textContent = sl.n > 1 ? "Route " + (sl.i + 1) + " of " + sl.n + " for this closure" : "";
    var sk = (r.skipped || []);
    $("skipBox").hidden = false;
    $("skips").innerHTML = sk.length ? sk.slice(0, 5).map(function(s){ return '<li><b>' + esc(s.code) + '</b><span>' + esc(s.name || "") + '</span></li>'; }).join("") + (sk.length > 5 ? '<li class="more"><b></b><span>and ' + (sk.length - 5) + ' more</span></li>' : "")
      : '<li class="none">None: every stop is served</li>';
    var f = ['<div><b>' + (r.km ? (+r.km).toFixed(1) + " km" : dist(sl.lineM)) + '</b><span>diversion</span></div>'];
    if(r.added_km > .05) f.push('<div><b>+' + (+r.added_km).toFixed(1) + ' km</b><span>longer</span></div>');
    if(r.added_min >= .5) f.push('<div><b>+' + Math.round(r.added_min) + ' min</b><span>extra time</span></div>');
    $("facts").innerHTML = f.join("");
    var nt = String(c.note || "").trim(); $("note").hidden = !nt; $("note").textContent = nt;
    paintWhen(sl);
  }
  function paintWhen(sl){
    var c = sl.c, t = now() / 1000, h = "";
    if(T.pv && c.state !== "live") h = '<b>' + (c.start_ts ? (c.state === "upcoming" ? "Starts " : "Ran from ") + whenTxt(c.start_ts) : "Saved " + esc(c.date || "")) + '</b><span>' + (c.end_ts ? "until " + whenTxt(c.end_ts) : c.start_ts ? "until further notice" : "Preview") + '</span>';
    else h = '<b>Live since ' + whenTxt(c.start_ts || c.ts) + '</b>' + (c.end_ts ? '<span>Ends ' + whenTxt(c.end_ts) + '</span><span class="left">' + left(c.end_ts * 1000 - t * 1000) + '</span>' : '<span>Until further notice</span>');
    if($("when").innerHTML !== h) $("when").innerHTML = h;
  }
  function ribbon(sl){
    var r = sl.r, segs = sl.segs, tot = segs.reduce(function(a, s){ return a + Math.sqrt(Math.max(30, s.b - s.a)); }, 0), h = [];
    h.push('<div class="cap"><span class="ring"></span><b>Leaves route</b><span>' + (r.leave && r.leave.code ? "after BS " + esc(r.leave.code) : "") + '</span></div>');
    segs.forEach(function(s, k){
      if(k){ var t = segs[k].turn; h.push('<div class="jn" data-k="' + k + '"><span class="t" title="' + esc(turnTxt(t)) + '">' + turnSvg(t) + '</span><span class="u"></span></div>'); }
      var w = Math.sqrt(Math.max(30, s.b - s.a)) / tot;
      h.push('<div class="seg ' + s.kind + '" data-k="' + k + '" style="flex-grow:' + (w * 100).toFixed(2) + '"><div class="nm">' + esc(s.name) + '</div><div class="bar"><i></i></div><div class="km">' + (s.b - s.a >= 40 ? dist(s.b - s.a) : "") + '</div></div>');
    });
    h.push('<div class="cap"><span class="ring"></span><b>Back on route</b><span>' + (r.rejoin && r.rejoin.code ? "at BS " + esc(r.rejoin.code) : "") + '</span></div>');
    $("track").innerHTML = h.join("");
    $("rhead").innerHTML = '<b>' + esc(sl.services.join(", ")) + '</b><span>' + (sl.segs.length) + ' roads, ' + Math.max(0, sl.segs.length - 1) + ' turns. The bus leaves its route, follows the green roads and comes back after the closure.</span>';
    // fit long road names: shrink until nothing is cut off (down to 60%)
    var tr = $("track"), fs = 2.3; tr.style.setProperty("--rf", fs + "rem");
    for(var g = 0; g < 8; g++){
      var cutOff = [].some.call(tr.querySelectorAll(".seg .nm"), function(e){ return e.scrollWidth > e.clientWidth + 1; });
      if(!cutOff) break; fs -= .15; tr.style.setProperty("--rf", fs + "rem");
    }
    T.segEls = [].slice.call(tr.querySelectorAll(".seg")); T.jnEls = [].slice.call(tr.querySelectorAll(".jn"));
  }
  function ribbonAt(sl, d){
    var segs = sl.segs;
    segs.forEach(function(s, k){
      var el = T.segEls[k]; if(!el) return;
      var p = d >= s.b ? 1 : d <= s.a ? 0 : (d - s.a) / Math.max(1, s.b - s.a), on = d > s.a - .01 && d < s.b && d < sl.total;
      el.style.setProperty("--p", p.toFixed(4));
      el.classList.toggle("on", on); el.classList.toggle("done", p >= 1 && !on);
      var j = T.jnEls[k - 1]; if(j) j.classList.toggle("passed", d >= s.a);
    });
  }
  function navAt(sl, d){                                                 // sat-nav card: the next turn, how far, the road the bus is on
    var k = 0; while(k < sl.segs.length - 1 && d >= sl.segs[k].b) k++;
    var s = sl.segs[k], nx = sl.segs[k + 1], r = sl.r, ic, dd, act;
    if(!nx){ ic = "ok"; dd = "Back on route"; act = r.rejoin && r.rejoin.code ? "Next stop<b>BS " + esc(r.rejoin.code) + (r.rejoin.name ? " " + esc(r.rejoin.name) : "") + "</b>" : "Normal route<b>" + esc(s.name) + "</b>"; }
    else {
      ic = nx.turn; dd = dist(Math.max(0, s.b - d));
      act = (s.kind === "route" && nx.kind === "div" ? "Leaves its route: " : s.kind === "div" && nx.kind === "route" ? "Back on its route: " : "") + turnTxt(nx.turn).toLowerCase() + "<b>" + esc(nx.name) + "</b>";
      act = act.charAt(0).toUpperCase() + act.slice(1);
    }
    var key = ic + "|" + act;
    if(T.navKey !== key){ T.navKey = key; $("navI").innerHTML = turnSvg(ic); $("navA").innerHTML = act; }
    $("navD").textContent = dd;
    var now = "Now on <b>" + esc(s.name) + "</b>"; if($("navNow").innerHTML !== now) $("navNow").innerHTML = now;
    $("nav").classList.toggle("route", !nx || (s.kind === "route" && k > 0)); $("nav").hidden = false;
  }
  function pips(){
    var n = T.slides.length, h = "";
    for(var k = 0; k < n && n > 1; k++) h += '<i class="' + (k < T.i ? "done" : "") + '"><b></b></i>';
    if($("pips").innerHTML !== h) $("pips").innerHTML = h;
  }
  function foot(){
    var h = [], nx = T.slides.length > 1 ? T.slides[(T.i + 1) % T.slides.length] : null;
    if(nx) h.push('<span class="it">Next on screen <b>' + esc(nx.c.blocked || nx.c.road || "") + '</b> for ' + esc(nx.services.join(", ")) + '</span>');
    if(T.up.length) h.push('<span class="it">Starting later ' + T.up.slice(0, 3).map(function(c){ return '<em>' + esc(whenTxt(c.start_ts)) + '</em> <b>' + esc(c.blocked || c.road) + '</b> ' + esc(uniq(c.services || []).join(", ")); }).join("&ensp;&ensp;") + '</span>');
    if(!h.length) h.push('<span class="it">' + (T.slides.length ? "Every other service is on its normal route" : "") + '</span>');
    h.push('<span class="sp"></span><span class="it">Updated ' + (T.okAt ? hm(T.okAt / 1000 + T.skew / 1000) : "–") + '</span>');
    var s = h.join(""); if($("foot").innerHTML !== s) $("foot").innerHTML = s;
  }

  /* ---------------------------------------------------------------- the show: overview -> drive -> overview -> next */
  function start(i){
    if(!T.slides.length){ T.sl = null; return; }
    T.i = (i + T.slides.length) % T.slides.length; var sl = T.sl = T.slides[T.i];
    T.t = 0; T.slideT = 0; T.phase = "intro"; $("nav").hidden = true;
    paintSide(sl); ribbon(sl); ribbonAt(sl, -1); pips(); foot();
    ["side", "ribbon"].forEach(function(id){ var e = $(id); e.classList.remove("fade"); void e.offsetWidth; e.classList.add("fade"); });
    if(R && R.ready){ R.show(sl); R.overview(sl, 1800 / SPEED); R.bus(sl.path[0], brg(sl.path[0], at(sl.path, sl.pc, 25)), 0, sl); }
  }
  function next(){ start(T.i + 1); }
  function prev(){ start(T.i - 1); }
  function ease(u){ return u < .08 ? (u * u) / .16 : u > .92 ? 1 - ((1 - u) * (1 - u)) / .16 : u; }   // gentle start and stop, steady between
  function place(sl, d, dt){
    var p = at(sl.path, sl.pc, d), h = brg(at(sl.path, sl.pc, d - 6), at(sl.path, sl.pc, d + 28)), ahead = at(sl.path, sl.pc, d + 70);
    R.bus(p, h, Math.max(0, Math.min(1, (d - sl.leadM) / Math.max(1, sl.lineM))), sl);
    R.follow(p, h, ahead, dt);
    ribbonAt(sl, d); navAt(sl, d);
  }
  function tick(ts){
    requestAnimationFrame(tick);
    var dt = T.last ? Math.min(.25, (ts - T.last) / 1000) : 0; T.last = ts;
    if(!R || !R.ready) return;
    if(!T.sl){ if(!REDUCE && R.idle) R.idle(dt); return; }
    if(T.paused) return;
    var sl = T.sl; T.t += dt * SPEED; T.slideT += dt * SPEED;
    var all = sl.dIntro + 1.8 + sl.dDrive + sl.dOutro;
    if(REDUCE){ if(T.t > 15) next(); return; }                         // reduced motion: overview only, one diversion every 15 s
    if(T.phase === "intro" && T.t >= sl.dIntro){
      T.phase = "approach"; T.t = 0;
      var h0 = brg(sl.path[0], at(sl.path, sl.pc, 28)); R.approach(sl.path[0], h0, at(sl.path, sl.pc, 70), 1800 / SPEED);
    } else if(T.phase === "approach" && T.t >= 1.8){
      T.phase = "drive"; T.t = 0; T.frames = [];
    } else if(T.phase === "drive"){
      var u = Math.min(1, T.t / sl.dDrive); place(sl, ease(u) * sl.total, dt);
      if(!T.liteDone && R.kind === "3d"){ T.frames.push(dt); if(T.frames.length > 90){ var avg = T.frames.slice(-90).reduce(function(a, b){ return a + b; }, 0) / 90; if(avg > .055){ T.liteDone = true; R.lite(); } T.frames = []; } }
      if(u >= 1){ T.phase = "outro"; T.t = 0; $("nav").hidden = true; ribbonAt(sl, sl.total + 1); R.overview(sl, 1800 / SPEED); }
    } else if(T.phase === "outro" && T.t >= sl.dOutro){
      next(); return;
    }
    var pb = $("pips").children[T.i]; if(pb && pb.firstChild) pb.firstChild.style.width = Math.min(100, T.slideT / all * 100).toFixed(1) + "%";
  }

  /* ---------------------------------------------------------------- TV niceties: full screen, hide the cursor, keep the screen on */
  var uiT = 0;
  function ui(){ var tv = $("tv"); tv.classList.add("ui"); tv.classList.remove("nocur"); clearTimeout(uiT); uiT = setTimeout(function(){ tv.classList.remove("ui"); tv.classList.add("nocur"); }, 3000); }
  function fullscreen(){ var d = document, e = d.documentElement; if(d.fullscreenElement){ if(d.exitFullscreen) d.exitFullscreen(); } else if(e.requestFullscreen) e.requestFullscreen().catch(function(){}); }
  document.addEventListener("mousemove", ui); document.addEventListener("pointerdown", ui);
  $("fs").onclick = fullscreen;
  document.addEventListener("keydown", function(e){
    if(e.key === "f" || e.key === "F") fullscreen();
    else if(e.key === "ArrowRight") next(); else if(e.key === "ArrowLeft") prev();
    else if(e.key === " "){ T.paused = !T.paused; e.preventDefault(); }
  });
  var lock = null;
  function wake(){ try{ if(navigator.wakeLock && document.visibilityState === "visible") navigator.wakeLock.request("screen").then(function(l){ lock = l; }).catch(function(){}); }catch(e){} }
  document.addEventListener("visibilitychange", wake); wake();
  if(!PREVIEW) setTimeout(function(){ location.reload(); }, 6 * 3600e3);    // a fresh page every 6 h (long-running TV browsers)

  /* ---------------------------------------------------------------- go */
  function boot(){
    clock(); setInterval(clock, 1000);
    var use3d = !FORCE2D && webgl();
    R = use3d ? R3() : R2();
    R.init().catch(function(e){
      if(R.kind === "3d"){ if(window.console) console.warn("TV: 3D map unavailable, using 2D", e); try{ if(R.ml) R.ml.remove(); }catch(x){} $("map").innerHTML = ""; R = R2(); return R.init(); }
      throw e;
    }).then(function(){ if(T.sl) start(T.i); }).catch(function(e){ if(window.console) console.warn("TV: no map", e); });
    poll(); setInterval(function(){ poll(); }, 30000);
    setInterval(foot, 15000);
    requestAnimationFrame(tick);
    ui();
  }
  window.__tv = {T:T, next:next, prev:prev, go:start, poll:poll, kind:function(){ return R && R.kind; }, ready:function(){ return !!(R && R.ready); }, map:function(){ return R && (R.ml || R.map); }};
  boot();
})();
