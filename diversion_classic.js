/* SG Transport Pulse — Diversion Maps (V16.14): OCC Diversion Decision Engine, client.
   BLOCK -> ANALYSE -> PLAN -> SIMULATE -> CONFIRM -> MONITOR -> RECOVER.
   Every figure shown comes from /api/diversion/* (LTA DataMall, OSRM road routes, OpenStreetMap tags). Estimates are labelled as such.
   Decision support only: nothing is executed or sent outside the platform until the controller confirms. */
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); }, esc = DS.esc;
  var C = {blue:"#2f8cff", red:"#ff5566", green:"#2ee59d", amber:"#ffb547", cyan:"#38d6ff", grey:"#6f8399"};
  var SPEED = {smooth:"#22d36a", moderate:"#ffc21a", slow:"#ff8a1f", congested:"#ff4d55", none:"#2f7dff"};
  var LS = {get:function(k, d){ try{ var v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } },
            set:function(k, v){ try{ localStorage.setItem(k, JSON.stringify(v)); }catch(e){} }};

  var S = {
    blocks:[], bi:0, editing:true, allowSmall:false, net:null, showNet:true, netPending:false, autoSimDone:false,
    closure:{min:30, label:"30 MIN"}, bus:LS.get("dv.bus", "dd"), occ:LS.get("dv.occ", ""),
    an:null, sel:null, opts:null, optSel:null, choices:{}, plan:null, shownRev:null,
    simT:0, simPlay:false, simSeen:false, teams:[], mobile:null, busy:{an:0, opt:0, net:0}, pending:null
  };

  /* ------------------------------------------------------------------ helpers */
  /* scope + data choices ride along with every diversion request (operator / services filter, LIVE or TEST data, traffic) */
  var FLT = LS.get("dv.flt", {op:"", svcs:[], test:false});
  if(!Array.isArray(FLT.svcs)) FLT.svcs = [];
  var TRAFFIC = LS.get("dv.traffic", true) !== false;
  function flags(){ var f = {operator:FLT.op, services_filter:FLT.svcs, test:!!FLT.test, traffic:TRAFFIC}; if(S && S.manual && S.manual.length) f.manual_vias = S.manual; return f; }
  var FLAGGED = /\/api\/diversion\/(analyse|options|plan_all|plans$|plans\/\d+\/(confirm|update))/;
  function post(url, body, timeout){
    if(FLAGGED.test(url)) body = Object.assign({}, body || {}, flags());
    return DS.api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body || {}), timeout:timeout || 30000});
  }
  function toast(t){ var el = $("toast"); el.textContent = t; el.classList.add("on"); clearTimeout(toast._t); toast._t = setTimeout(function(){ el.classList.remove("on"); }, 2600); }
  function f0(x){ return x == null || isNaN(x) ? "\u2013" : String(Math.round(x)); }
  function f1(x){ return x == null || isNaN(x) ? "\u2013" : (Math.round(x * 10) / 10).toFixed(1); }
  function sgn(x, d){ if(x == null || isNaN(x)) return "\u2013"; var v = d ? (Math.round(x * 10) / 10).toFixed(1) : String(Math.round(x)); return (x > 0 ? "+" : x < 0 ? "" : "\u00b1") + v; }
  function clock(minFromNow){ return DS.fmtTime(new Date(Date.now() + minFromNow * 60000)); }
  function tc(s){ s = String(s || ""); return s === s.toUpperCase() ? s.toLowerCase().replace(/\b([a-z])/g, function(m){ return m.toUpperCase(); }) : s; }
  function km(m){ return m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m"; }
  function key(e){ return e.service + "|" + e.direction + "|" + (e.run || 0); }
  function stColor(st){ return st === "red" ? C.red : st === "amber" ? C.amber : st === "green" ? C.green : st === "on_diversion" ? C.green : C.grey; }
  function stTag(st){ return {red:"ACT NOW", amber:"APPROACHING", green:"NOT YET", passed_exit:"PASSED DIV. POINT", passed_block:"PAST BLOCK", inside:"IN SECTION", on_diversion:"ON DIVERSION", unknown:"POSITION ONLY"}[st] || String(st || "").toUpperCase(); }
  function pill(st, txt){ return '<span class="dv-pill" style="--c:' + stColor(st) + '">' + esc(txt || stTag(st)) + '</span>'; }

  /* geometry: flat metres (same approximation as diversion.py) */
  var KX = 111320 * Math.cos(1.35 * Math.PI / 180), KY = 110574;
  function dm(a, b){ return Math.hypot((a[1] - b[1]) * KX, (a[0] - b[0]) * KY); }
  function cumM(line){ var c = [0]; for(var i = 1; i < line.length; i++) c.push(c[i - 1] + dm(line[i - 1], line[i])); return c; }
  function nearestOn(p, line, cum){
    var best = {d:1e18, s:0};
    for(var i = 0; i < line.length - 1; i++){
      var ax = line[i][1] * KX, ay = line[i][0] * KY, bx = line[i + 1][1] * KX, by = line[i + 1][0] * KY, px = p[1] * KX, py = p[0] * KY;
      var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy, t = L2 < 1e-9 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2));
      var d = Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
      if(d < best.d) best = {d:d, s:cum[i] + t * (cum[i + 1] - cum[i])};
    }
    return best;
  }
  function interp(xs, ys, x){
    if(!xs.length) return 0; if(x <= xs[0]) return ys[0]; if(x >= xs[xs.length - 1]) return ys[ys.length - 1];
    var lo = 0, hi = xs.length - 1; while(hi - lo > 1){ var m = (lo + hi) >> 1; if(xs[m] <= x) lo = m; else hi = m; }
    var f = xs[hi] === xs[lo] ? 0 : (x - xs[lo]) / (xs[hi] - xs[lo]); return ys[lo] + (ys[hi] - ys[lo]) * f;
  }
  function pointAt(line, cum, s){
    if(!line.length) return null; if(s <= 0) return line[0]; if(s >= cum[cum.length - 1]) return line[line.length - 1];
    var lo = 0, hi = cum.length - 1; while(hi - lo > 1){ var m = (lo + hi) >> 1; if(cum[m] <= s) lo = m; else hi = m; }
    var f = cum[hi] === cum[lo] ? 0 : (s - cum[lo]) / (cum[hi] - cum[lo]);
    return [line[lo][0] + (line[hi][0] - line[lo][0]) * f, line[lo][1] + (line[hi][1] - line[lo][1]) * f];
  }
  function cutLine(line, cum, a, b){
    var out = [pointAt(line, cum, a)]; for(var i = 0; i < line.length; i++) if(cum[i] > a && cum[i] < b) out.push(line[i]); out.push(pointAt(line, cum, b)); return out;
  }
  /* several blockages: each {corridor, cm, a, b, road, directed, snapSrc}; S.bi = the one being edited */
  function hasBlock(){ return S.blocks.length > 0; }
  function curB(){ return S.blocks[S.bi] || null; }
  function bLine(b){ return cutLine(b.corridor, b.cm, b.a, b.b); }
  function roadsTxt(){ var seen = {}, out = []; S.blocks.forEach(function(b){ if(!seen[b.road]){ seen[b.road] = 1; out.push(b.road); } }); return out.join(" / "); }
  function blocksPayload(){ return S.blocks.map(function(b){ return {line:bLine(b), road:b.road, directed:b.directed}; }); }

  /* ------------------------------------------------------------------ map */
  var map = L.map("dvMap", {zoomControl:true, preferCanvas:false, minZoom:10, maxZoom:19, maxBounds:[[1.10, 103.50], [1.55, 104.20]]}).setView([1.3521, 103.8198], 12);
  SGBasemap.add(map, {style:"dark", minZoom:10, maxZoom:19});
  [["traffic", 330], ["tomtom", 340], ["routes", 410], ["alts", 420], ["divsel", 430], ["block", 440], ["pts", 620], ["bus", 640]].forEach(function(p){ map.createPane(p[0]).style.zIndex = p[1]; });
  map.getPane("traffic").style.pointerEvents = "none";
  var canvasR = L.canvas({padding:.3, pane:"traffic"});
  map.createPane("arrows").style.zIndex = 450;
  /* route drawing: darker, saturated colours with a white outline (readable on the dark basemap) and white direction arrows */
  var RC = {route:"#1257d6", other:"#1a4fa8", sel:"#00955a", alt:"#c96a00", small:"#a8430f", block:"#c81e36", esc:"#0a7ea8", prev:"#0a7ea8"};
  var ARROWS = [];
  function rline(pts, color, o, grp, withArrows){
    o = o || {}; var w = o.weight || 5;
    var cs = L.polyline(pts, {pane:o.pane, color:"#ffffff", weight:w + 4, opacity:o.casing == null ? .9 : o.casing, interactive:false, lineCap:o.lineCap || "round", lineJoin:"round"}).addTo(grp);
    var core = L.polyline(pts, Object.assign({lineJoin:"round"}, o, {color:color, weight:w})).addTo(grp);
    core._casing = cs;
    if(withArrows){ var ag = L.layerGroup().addTo(grp), sp = {pts:pts, color:color, grp:grp, ag:ag}; ARROWS.push(sp); arrowsFor(sp); }
    return core;
  }
  function arrowsFor(sp){
    sp.ag.clearLayers();
    var pts = sp.pts; if(!pts || pts.length < 2) return;
    var cm = cumM(pts), total = cm[cm.length - 1], lat = pts[0][0];
    var mpp = 40075016.686 * Math.cos(lat * Math.PI / 180) / (256 * Math.pow(2, map.getZoom()));
    var step = Math.max(80, 150 * mpp);
    if(total < step * .6) return;
    for(var d = step / 2; d < total; d += step){
      var a = pointAt(pts, cm, Math.max(0, d - 4)), b = pointAt(pts, cm, Math.min(total, d + 4));
      var brg = Math.atan2((b[1] - a[1]) * KX, (b[0] - a[0]) * KY) * 180 / Math.PI;
      L.marker(pointAt(pts, cm, d), {pane:"arrows", interactive:false, keyboard:false,
        icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dv-arr" style="--c:' + sp.color + ';transform:translate(-50%,-50%) rotate(' + brg.toFixed(0) + 'deg)"></div>'})}).addTo(sp.ag);
    }
  }
  function redrawArrows(){ ARROWS = ARROWS.filter(function(sp){ return sp.grp.hasLayer(sp.ag) && (map.hasLayer(sp.grp) || true); }); ARROWS.forEach(arrowsFor); }
  var G = {};
  ["speed", "incidents", "roadworks", "others", "net", "route", "blocked", "alts", "sel", "pts", "buses", "handles", "prev", "draw", "rej"].forEach(function(k){ G[k] = L.layerGroup().addTo(map); });
  var tomLayer = null;
  function divIcon(html, cls){ return L.divIcon({className:"", html:'<div class="' + (cls || "dv-mk") + '">' + html + '</div>', iconSize:[0, 0]}); }

  var lay = {speed:true, tomtom:false, incidents:true, roadworks:true, buses:true, alts:true, others:true, net:true, legend:true};
  document.querySelectorAll("#layers input[data-l]").forEach(function(c){
    c.onchange = function(){ lay[c.dataset.l] = c.checked; applyLayers(); if(c.dataset.l === "speed" && c.checked) loadSpeed(); };
  });
  function applyLayers(){
    [["speed", G.speed], ["incidents", G.incidents], ["roadworks", G.roadworks], ["buses", G.buses], ["alts", G.alts], ["others", G.others], ["net", G.net]].forEach(function(x){
      if(lay[x[0]] && !map.hasLayer(x[1])) x[1].addTo(map); if(!lay[x[0]] && map.hasLayer(x[1])) map.removeLayer(x[1]); });
    if(tomLayer){ if(lay.tomtom && !map.hasLayer(tomLayer)) tomLayer.addTo(map); if(!lay.tomtom && map.hasLayer(tomLayer)) map.removeLayer(tomLayer); }
    $("legend").classList.toggle("hid", !lay.legend);
  }
  DS.api("/api/health").then(function(h){
    if(h && h.tomtom){ $("ttRow").hidden = false; tomLayer = L.tileLayer("/api/tomtom/tile/{z}/{x}/{y}.png", {pane:"tomtom", minZoom:10, maxZoom:19, maxNativeZoom:18, opacity:.9, attribution:"Traffic \u00a9 TomTom"}); applyLayers(); }
  });

  /* live traffic context (existing Route Traffic feeds) */
  var speedT = null;
  function loadSpeed(){
    clearTimeout(speedT);
    speedT = setTimeout(function(){
      G.speed.clearLayers(); if(!lay.speed || map.getZoom() < 13) return;
      var b = map.getBounds();
      DS.api("/api/speedbands?bbox=" + [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map(function(v){ return v.toFixed(4); }).join(","), {timeout:30000}).then(function(j){
        G.speed.clearLayers(); if(j.error && !(j.segments || []).length) return;
        (j.segments || []).forEach(function(s){ L.polyline([[s[0], s[1]], [s[2], s[3]]], {renderer:canvasR, color:SPEED[s[5]] || SPEED.none, weight:s[5] === "congested" ? 3 : 2, opacity:.45, interactive:false}).addTo(G.speed); });
      });
    }, 350);
  }
  map.on("moveend", loadSpeed);
  map.on("zoomend", redrawArrows);
  function loadIncidents(){
    DS.api("/api/incidents").then(function(j){
      G.incidents.clearLayers();
      (j.incidents || []).forEach(function(x){ L.marker([x.lat, x.lon], {icon:divIcon("!", "dv-inc"), pane:"pts"}).bindPopup("<b>" + esc(x.type) + "</b><br>" + esc(x.message) + '<br><small class="dv-muted">LTA Traffic Incidents</small>').addTo(G.incidents); });
    });
    DS.api("/api/roadworks", {timeout:30000}).then(function(j){
      G.roadworks.clearLayers();
      (j.roadworks || []).forEach(function(x){ L.marker([x.lat, x.lon], {icon:divIcon("W", "dv-inc"), pane:"pts", opacity:.8}).bindPopup("<b>Road works \u00b7 " + esc(x.road) + "</b><br>" + esc(x.start || "") + " \u2192 " + esc(x.end || "") + (x.other ? "<br>" + esc(x.other) : "") + '<br><small class="dv-muted">LTA Road Works</small>').addTo(G.roadworks); });
    });
  }

  /* ------------------------------------------------------------------ the draggable road block */
  var chips = [];
  function makeChip(home, compact){
    var btn = document.createElement("button"); btn.type = "button"; btn.className = "dv-blockchip";
    btn.innerHTML = '<span class="dv-x" aria-hidden="true">&#x2715;</span><span>' + (compact ? "ADD BLOCKAGE" : "ROAD BLOCK") + (compact ? "" : '<small>Drag onto a road, or tap then tap the map</small>') + '</span>';
    btn.setAttribute("aria-label", "Road block: drag onto the blocked road, or press and then tap the map");
    home.innerHTML = ""; home.appendChild(btn); chips.push(btn); wireChip(btn); return btn;
  }
  var armed = false;
  function arm(on){
    armed = on; chips.forEach(function(c){ c.classList.toggle("arm", on); });
    $("dvHint").classList.toggle("on", on); map.getContainer().style.cursor = on ? "crosshair" : "";
  }
  function wireChip(btn){
    var ghost = null, moved = false, sx = 0, sy = 0;
    btn.addEventListener("pointerdown", function(e){
      if(e.button > 0 || lockedEdit()) return;
      sx = e.clientX; sy = e.clientY; moved = false;
      try{ btn.setPointerCapture(e.pointerId); }catch(x){}
    });
    btn.addEventListener("pointermove", function(e){
      if(!btn.hasPointerCapture || !btn.hasPointerCapture(e.pointerId)) return;
      if(!moved && Math.hypot(e.clientX - sx, e.clientY - sy) < 8) return;
      if(!ghost){ ghost = document.createElement("div"); ghost.className = "dv-ghost"; ghost.innerHTML = '<div class="dv-mk-x">&#x2715;</div>'; document.body.appendChild(ghost); binShow("Drop here to cancel"); }
      moved = true; ghost.style.left = e.clientX + "px"; ghost.style.top = e.clientY + "px"; binHot(e.clientX, e.clientY);
    });
    btn.addEventListener("pointerup", function(e){
      try{ btn.releasePointerCapture(e.pointerId); }catch(x){}
      if(ghost){ ghost.remove(); ghost = null; }
      var inBin = moved && binHot(e.clientX, e.clientY); binHide();
      if(!moved) return;                              // a plain click is handled below
      btn._skipClick = true; setTimeout(function(){ btn._skipClick = false; }, 60);
      if(inBin){ toast("Cancelled \u2014 no blockage added."); return; }
      var r = map.getContainer().getBoundingClientRect();
      if(e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom){
        drop(map.containerPointToLatLng(L.point(e.clientX - r.left, e.clientY - r.top)));
      }
      btn._skipClick = true; setTimeout(function(){ btn._skipClick = false; }, 60);
    });
    btn.addEventListener("click", function(){ if(btn._skipClick || lockedEdit()) return; arm(!armed); if(armed && S.mobile) sheetTo("peek"); });
  }
  map.on("click", function(e){ if(armed){ arm(false); drop(e.latlng); } });

  /* the bin: shown while anything is being dragged; dropping on it cancels a new blockage or deletes one */
  function binShow(t){ $("dvBinT").textContent = t; $("dvBin").classList.add("on"); $("dvBin").setAttribute("aria-hidden", "false"); }
  function binHide(){ $("dvBin").classList.remove("on", "hot"); $("dvBin").setAttribute("aria-hidden", "true"); }
  function binHot(x, y){
    var el = $("dvBin"); if(!el.classList.contains("on")) return false;
    var r = el.getBoundingClientRect(), pad = 18, hot = x >= r.left - pad && x <= r.right + pad && y >= r.top - pad && y <= r.bottom + pad;
    el.classList.toggle("hot", hot); return hot;
  }
  function evXY(e){ var o = e && e.originalEvent; if(!o) return null; var t = o.touches && o.touches[0] || o.changedTouches && o.changedTouches[0] || o; return t && t.clientX != null ? [t.clientX, t.clientY] : null; }
  var lastXY = null;
  document.addEventListener("pointermove", function(e){ lastXY = [e.clientX, e.clientY]; }, {passive:true});
  document.addEventListener("touchmove", function(e){ if(e.touches[0]) lastXY = [e.touches[0].clientX, e.touches[0].clientY]; }, {passive:true});
  function dragBin(m, label, onDrop){
    m.on("dragstart", function(){ binShow(label); });
    m.on("drag", function(e){ var xy = evXY(e) || lastXY; if(xy) binHot(xy[0], xy[1]); });
    m.on("dragend", function(e){ var xy = evXY(e) || lastXY, hot = xy ? binHot(xy[0], xy[1]) : false; binHide(); if(hot){ onDrop(); return true; } });
  }
  document.addEventListener("keydown", function(e){ if(e.key === "Escape" && armed) arm(false); });
  function lockedPlanQuiet(){ return !!(S.plan && ["active", "monitoring", "recovering"].indexOf(S.plan.status) >= 0 && !S.updating); }
  function lockedEdit(){
    if(S.plan && ["active", "monitoring", "recovering"].indexOf(S.plan.status) >= 0 && !S.updating){ toast("This diversion is " + S.plan.status.toUpperCase() + ". Press UPDATE to change the blockage."); return true; }
    return false;
  }

  function drop(ll){
    $("blockSub").textContent = "Finding the road\u2026";
    DS.api("/api/diversion/snap?lat=" + ll.lat.toFixed(6) + "&lon=" + ll.lng.toFixed(6)).then(function(j){
      if(!j.ok){ renderBlockCard(); toast(j.error || "No road found there."); return; }
      S.manual = null; G.draw.clearLayers();
      if(S.plan && S.plan.status === "ended"){ detachPlan(); S.blocks = []; S.choices = {}; }
      var nb = {corridor:j.corridor, cm:j.corridor_m, a:j.start_m, b:j.end_m, road:tc(j.road), snapSrc:j.source, directed:false};
      var mid = pointAt(nb.corridor, nb.cm, (nb.a + nb.b) / 2);
      var hit = S.blocks.map(function(b, i){ var l = bLine(b); return {i:i, d:nearestOn(mid, l, cumM(l)).d}; }).filter(function(x){ return x.d < 60; })[0];
      if(hit){ S.bi = hit.i; S.editing = true; drawBlocks(hit.i); toast("That section is already blocked \u2014 drag its START / END handles to extend it."); return; }
      S.blocks.push(nb); S.bi = S.blocks.length - 1; S.editing = true;
      resetAnalysis(); drawBlocks(S.bi); analyse();
      if(S.blocks.length > 1) toast("Blockage " + S.blocks.length + " added \u2014 all blockages are analysed together.");
    });
  }
  function snapSaved(bl){
    var line = bl.line || [], n = line.length; if(n < 2) return Promise.resolve();
    var mid = line[Math.floor(n / 2)];
    return DS.api("/api/diversion/snap?lat=" + mid[0] + "&lon=" + mid[1]).then(function(j){
      var b = {road:tc(bl.road || (j && j.road) || "Road"), directed:!!bl.directed};
      if(!j || !j.ok){ b.corridor = line; b.cm = cumM(line); b.a = 0; b.b = b.cm[b.cm.length - 1]; b.snapSrc = "saved block"; }
      else{
        b.corridor = j.corridor; b.cm = j.corridor_m; b.snapSrc = j.source;
        var a = nearestOn(line[0], b.corridor, b.cm).s, e = nearestOn(line[n - 1], b.corridor, b.cm).s;
        b.a = Math.min(a, e); b.b = Math.max(a, e); if(b.b - b.a < 30){ b.a = j.start_m; b.b = j.end_m; }
      }
      S.blocks.push(b);
    });
  }
  function loadBlocks(list){
    S.blocks = []; S.bi = 0;
    return (list || []).reduce(function(p, bl){ return p.then(function(){ return snapSaved(bl); }); }, Promise.resolve()).then(function(){ drawBlocks(-1); });
  }
  function savedBlocks(block){ return block && block.blocks && block.blocks.length ? block.blocks : (block && block.line ? [block] : []); }

  var hA = null, hB = null;
  function drawBlocks(fit){
    G.blocked.clearLayers(); G.handles.clearLayers(); hA = hB = null;
    if(!hasBlock()){ renderBlockCard(); setLife(); return; }
    var all = null, many = S.blocks.length > 1;
    S.blocks.forEach(function(b, i){
      var bl = bLine(b), sel = i === S.bi && S.editing;
      if(sel) L.polyline(b.corridor, {pane:"block", color:C.red, weight:2, opacity:.35, dashArray:"3 6", interactive:false}).addTo(G.blocked);
      b._line = rline(bl, RC.block, {pane:"block", weight:7, opacity:1, className:"dv-blockline", lineCap:"butt"}, G.blocked, false)
        .bindTooltip((many ? "Blockage " + (i + 1) + " \u00b7 " : "") + b.road + " \u00b7 " + km(b.b - b.a), {sticky:true});
      b._x = L.marker(pointAt(b.corridor, b.cm, (b.a + b.b) / 2), {draggable:!lockedPlanQuiet(), autoPan:false, icon:divIcon('<div class="dv-mk-x" style="position:relative">&#x2715;' + (many ? '<sup>' + (i + 1) + '</sup>' : '') + '</div>'), pane:"bus", keyboard:false})
        .addTo(G.blocked).bindTooltip(esc(b.road) + " \u2014 blocked" + (many ? " (" + (i + 1) + ")" : ""), {direction:"top"})
        .on("click", function(){ if(S.bi !== i){ S.bi = i; drawBlocks(); } });
      (function(bb, mk){ var home = mk.getLatLng(), del = false;
        dragBin(mk, "Drop here to delete this blockage", function(){ del = true; });
        mk.on("dragend", function(){ if(del){ del = false; var k = S.blocks.indexOf(bb); removeBlock(k < 0 ? S.bi : k); } else { mk.setLatLng(home); toast("Drag the \u2715 to the bin to delete it; drag the START / END handles to resize."); } });
      })(b, b._x);
      all = all ? all.extend(bl) : L.latLngBounds(bl);
    });
    var cb = curB();
    if(S.editing && cb){
      hA = handle(cb, cb.a, "BLOCK START", function(s){ cb.a = Math.min(s, cb.b - 30); });
      hB = handle(cb, cb.b, "BLOCK END", function(s){ cb.b = Math.max(s, cb.a + 30); });
    }
    if(fit === -1 && all) map.fitBounds(all.pad(many ? .5 : 1.6), {maxZoom:17});
    else if(typeof fit === "number" && S.blocks[fit]) map.fitBounds(L.latLngBounds(bLine(S.blocks[fit])).pad(1.6), {maxZoom:17});
    renderBlockCard(); setLife();
  }
  function handle(b, s, label, set){
    var st = label === "BLOCK START";
    var m = L.marker(pointAt(b.corridor, b.cm, s), {draggable:true, pane:"bus", icon:divIcon('<div class="dv-handle' + (st ? " st" : "") + '" role="slider" aria-label="' + label + ' (drag along the road)"><span>' + (st ? "START" : "END") + '</span></div>', "dv-mk"), autoPan:true}).addTo(G.handles);
    m.on("drag", function(e){
      var p = e.target.getLatLng(), n = nearestOn([p.lat, p.lng], b.corridor, b.cm); set(n.s);
      if(b._line){ b._line.setLatLngs(bLine(b)); if(b._line._casing) b._line._casing.setLatLngs(bLine(b)); } if(b._x) b._x.setLatLng(pointAt(b.corridor, b.cm, (b.a + b.b) / 2)); renderBlockCard();
    });
    var binned = false;
    dragBin(m, "Drop here to delete this blockage", function(){ binned = true; });
    m.on("dragend", function(){ if(binned){ binned = false; var i = S.blocks.indexOf(b); removeBlock(i < 0 ? S.bi : i); return; } drawBlocks(); resetAnalysis(); analyse(); });
    return m;
  }
  function renderBlockCard(){
    var el = $("blockCard");
    if(!hasBlock()){ el.innerHTML = '<p class="dv-muted" style="margin:10px 0 0">Drag the road block onto the map where the road is obstructed. It snaps to the nearest road and marks 300 m as blocked; drag the two handles to set the exact section. Drag it again to add more blockages \u2014 they are analysed together.</p>'; $("blockSub").textContent = ""; return; }
    var tot = S.blocks.reduce(function(a, b){ return a + (b.b - b.a); }, 0), many = S.blocks.length > 1;
    $("blockSub").textContent = (many ? S.blocks.length + " blockages \u00b7 " : "") + km(tot);
    var h = "";
    S.blocks.forEach(function(b, i){
      if(i !== S.bi){ h += '<button type="button" class="dv-bl" data-sel="' + i + '" aria-label="Select blockage ' + (i + 1) + '"><span class="n">&#x2715; ' + (i + 1) + '</span><b>' + esc(b.road) + '</b><span>' + km(b.b - b.a) + (b.directed ? " \u00b7 one way" : "") + '</span></button>'; return; }
      var sp = pointAt(b.corridor, b.cm, b.a), ep = pointAt(b.corridor, b.cm, b.b);
      h += '<div class="dv-block-card"><div class="k">' + (many ? "BLOCKAGE " + (i + 1) + " OF " + S.blocks.length : "ROAD BLOCKAGE") + '</div><b>' + esc(b.road) + '</b><dl>'
        + '<dt>Affected section</dt><dd><b style="display:inline;font:inherit;color:#fff">' + km(b.b - b.a) + '</b></dd>'
        + '<dt>Block start</dt><dd>' + sp[0].toFixed(5) + ', ' + sp[1].toFixed(5) + '</dd><dt>Block end</dt><dd>' + ep[0].toFixed(5) + ', ' + ep[1].toFixed(5) + '</dd>'
        + '<dt>Road data</dt><dd>' + esc(b.snapSrc) + '</dd></dl>'
        + '<div class="ds-segm" role="group" aria-label="Blocked direction" style="margin-bottom:8px"><button type="button" data-dir="0" aria-pressed="' + !b.directed + '">BOTH DIRECTIONS</button><button type="button" data-dir="1" aria-pressed="' + b.directed + '">DRAWN DIRECTION ONLY</button></div>'
        + '<div class="dv-row"><button type="button" class="ds-btn sm" id="bEdit">' + (S.editing ? "LOCK BLOCKAGE" : "EDIT BLOCKAGE") + '</button><button type="button" class="ds-btn sm ghost" id="bRemove">REMOVE</button><button type="button" class="ds-btn sm ghost" id="bZoom">ZOOM</button>'
        + (many ? '<button type="button" class="ds-btn sm ghost" id="bAll">SHOW ALL</button>' : '') + '</div></div>';
    });
    h += '<p class="dv-muted" style="margin:8px 0 0">Drag the \u2715 ROAD BLOCK again to add another blockage. Blockages close together on one route are bypassed by one diversion.</p>';
    el.innerHTML = h;
    var cb = curB();
    el.querySelectorAll("[data-sel]").forEach(function(x){ x.onclick = function(){ S.bi = +x.dataset.sel; drawBlocks(S.bi); }; });
    el.querySelectorAll("[data-dir]").forEach(function(x){ x.onclick = function(){ if(lockedEdit()) return; cb.directed = x.dataset.dir === "1"; renderBlockCard(); resetAnalysis(); analyse(); }; });
    $("bEdit").onclick = function(){ if(lockedEdit()) return; S.editing = !S.editing; drawBlocks(); };
    $("bRemove").onclick = function(){ removeBlock(S.bi); };
    $("bZoom").onclick = function(){ map.fitBounds(L.latLngBounds(bLine(cb)).pad(1.6), {maxZoom:17}); };
    if($("bAll")) $("bAll").onclick = function(){ drawBlocks(-1); };
  }
  function removeBlock(i){
    if(lockedEdit()) return;
    S.manual = null; G.draw.clearLayers();
    if(S.blocks.length > 1){ S.blocks.splice(i, 1); S.bi = Math.max(0, Math.min(S.bi, S.blocks.length - 1)); resetAnalysis(); drawBlocks(); analyse(); return; }
    if(S.plan && ["detected", "planned"].indexOf(S.plan.status) >= 0){
      if(!confirm("Discard the saved plan for " + S.plan.road + "? It will be closed without being activated.")) return;
      post("/api/diversion/plans/" + S.plan.id + "/status", {status:"ended"}).then(loadPlans);
    }
    detachPlan(); S.blocks = []; S.choices = {}; resetAnalysis(); drawBlocks(); renderAll();
  }

  /* ------------------------------------------------------------------ ANALYSE: affected services + approaching buses */
  function resetAnalysis(){
    S.an = null; S.sel = null; S.opts = null; S.optSel = null; S.simSeen = false; S.net = null; netSeq++;
    ["others", "route", "alts", "sel", "pts", "buses", "prev", "net", "rej"].forEach(function(k){ G[k].clearLayers(); });
    stopSim(); renderAll();
  }
  var anSeq = 0;
  function analyse(){
    if(!hasBlock()) return;
    var my = ++anSeq; S.busy.an++; setLife();
    var ld = DS.loading("svcList", ["Matching every bus route to the blocked road\u2026", "Finding stops that become inaccessible\u2026", "Locating approaching buses (LTA Bus Arrival)\u2026", "Checking previous diversions\u2026"], "ANALYSING NETWORK IMPACT");
    post("/api/diversion/analyse", {blocks:blocksPayload(), closure_min:S.closure.min}, 60000).then(function(j){
      ld.done(); S.busy.an--; if(my !== anSeq) return;
      if(j.error || !j.ok){ $("svcList").innerHTML = '<p class="dv-err">' + esc(j.error || "Analysis failed.") + '</p>'; setLife(); return; }
      S.an = j; S.netPending = true; renderServices(); drawOtherRoutes(); renderPlaybook(); setLife();
      var pick = S.pending && j.entries.filter(function(e){ return e.service === S.pending.service && e.direction === S.pending.direction; })[0];
      if(!pick){                                                      // most urgent first: the service with a bus closest to the block
        var best = null, bm = 1e9;
        j.entries.forEach(function(e){ (e.buses || []).forEach(function(b){ if(b.min_to_block != null && b.min_to_block < bm){ bm = b.min_to_block; best = e; } }); });
        pick = best || j.entries[0];
      }
      if(pick) selectService(key(pick)); else runNet();
      renderAll();
    });
  }
  function renderServices(){
    var j = S.an, el = $("svcList");
    if(!j){ el.innerHTML = hasBlock() ? "" : DS.empty({icon:"route", title:"NO BLOCKAGE PLACED", text:"Affected services, directions and approaching buses appear here once a road block is on the map."}); $("svcSub").textContent = ""; return; }
    $("svcSub").textContent = j.test ? "TEST buses (synthetic)" : (j.polled ? "live buses: LTA Bus Arrival" : "");
    var fi = j.filter || {}, filt = (fi.operator || (fi.services || []).length) ? '<p class="dv-muted" style="margin:0 0 8px">Showing ' + fi.shown + ' of ' + fi.total + ' affected service-directions'
      + (fi.operator ? ' \u00b7 operator ' + esc(fi.operator) : '') + ((fi.services || []).length ? ' \u00b7 services ' + esc(fi.services.join(", ")) : '') + ' <button type="button" class="ds-btn sm ghost" id="fClear">Show all</button></p>' : '';
    if(!j.entries.length && fi.total){ el.innerHTML = filt + DS.empty({icon:"route", title:"NO AFFECTED SERVICE MATCHES THE FILTER", text:fi.total + " service-direction(s) run along the blocked section, but none match the operator / services filter."}); var fc0 = $("fClear"); if(fc0) fc0.onclick = clearFlt; return; }
    if(!j.entries.length){ el.innerHTML = DS.empty({icon:"route", title:"NO SERVICE RUNS ALONG THIS SECTION", text:"No bus route runs along the blocked section (routes that only cross it are not counted). Matching basis: " + j.basis + "."}); return; }
    var far = (j.far_apart || []).map(function(x){ return '<div class="dv-far">\u26a0 Blockages ' + x.km + ' km apart (' + esc(tc(x.a)) + ' and ' + esc(tc(x.b)) + '). They are planned together as one incident \u2014 if one is left over from earlier, drag its \u2715 to the bin.</div>'; }).join("");
    var h = filt + far + '<div class="dv-tot"><b>' + j.services + '</b>services affected<span style="margin-left:auto"><b style="font-size:20px">' + j.buses + '</b> buses</span></div>';
    j.entries.forEach(function(e){
      var k = key(e), n = (e.buses || []).filter(function(b){ return b.status !== "passed_block"; });
      var dots = n.slice(0, 8).map(function(b){ return '<i class="st-' + b.status + '" title="' + esc(b.label + " \u00b7 " + b.status_text) + '"></i>'; }).join("");
      var imp = e.stops_inaccessible.filter(function(s){ return s.important.length; }).length;
      h += '<button type="button" class="dv-svc" data-k="' + esc(k) + '" aria-pressed="' + (S.sel === k) + '"><b>' + esc(e.service) + '</b><span class="d">D' + e.direction + (e.run ? " \u00b7 pass " + (e.run + 1) : "") + '<span class="dv-dots">' + dots + '</span></span>'
        + '<span class="n">' + (e.buses_polled ? n.length + " bus" + (n.length === 1 ? "" : "es") : "not polled") + '</span>'
        + '<span class="m">' + (S.blocks.length > 1 ? '\u2715 ' + e.blocks.map(function(x){ return x + 1; }).join(" + ") + ' \u00b7 ' : '') + e.stops_inaccessible.length + ' stop(s) inaccessible' + (imp ? ' \u00b7 \u26a0 ' + imp + ' important' : '') + ' \u00b7 to ' + esc(e.last) + '</span></button>';
    });
    if(j.not_polled) h += '<p class="dv-muted">' + j.not_polled + ' service-direction(s) not polled for live buses (limit ' + j.poll_cap + ' per analysis, protects the LTA quota). Select one to load its buses.</p>';
    if(j.arrival_error) h += '<p class="dv-err">Bus Arrival unavailable: ' + esc(j.arrival_error) + '</p>';
    if((j.incidents || []).length) h += '<div class="dv-lbl">NEAR THE BLOCK (LTA)</div>' + j.incidents.map(function(x){ return '<div class="dv-muted">\u26a0 ' + esc(x.type) + ' \u2014 ' + esc(x.message) + '</div>'; }).join("");
    h += '<p class="dv-muted" style="margin-top:8px">Matched on ' + esc(j.basis) + '. A service counts when it runs along the section, not when it only crosses it.</p>';
    el.innerHTML = h;
    var fc = $("fClear"); if(fc) fc.onclick = clearFlt;
    el.querySelectorAll(".dv-svc").forEach(function(b){ b.onclick = function(){ selectService(b.dataset.k); if(S.mobile) sheetTab("opt"); }; });
  }
  function entryBy(k){ return S.an ? S.an.entries.filter(function(e){ return key(e) === k; })[0] : null; }
  var routeCache = {};
  function drawOtherRoutes(){
    G.others.clearLayers(); if(!S.an) return;
    S.an.entries.forEach(function(e){
      var k = e.service + "|" + e.direction;
      var draw = function(line){ if(!line || S.sel && S.sel.indexOf(k + "|") === 0) return; rline(line, RC.other, {pane:"routes", weight:3, opacity:.7, casing:.35}, G.others, false).bindTooltip("Service " + e.service + " D" + e.direction, {sticky:true}).on("click", function(){ selectService(key(e)); }).addTo(G.others); };
      if(routeCache[k]) return draw(routeCache[k]);
      var fp = hasBlock() ? bLine(S.blocks[0])[0] : null;
      DS.api("/api/diversion/route?service=" + encodeURIComponent(e.service) + "&direction=" + e.direction + (fp ? "&lat=" + fp[0].toFixed(5) + "&lon=" + fp[1].toFixed(5) : ""), {timeout:60000}).then(function(r){ if(r.line && r.line.length){ routeCache[k] = r.line; draw(r.line); } });
    });
  }
  function renderPlaybook(){
    var el = $("playbook"), items = (S.an && S.an.playbook) || [];
    if(!items.length || (S.plan && items[0].id === S.plan.id)){ el.innerHTML = ""; return; }
    var p = items[0];
    el.innerHTML = '<div class="dv-pb"><div class="k">PREVIOUS DIVERSION AVAILABLE</div><b>' + esc(p.road) + '</b><small>Last used ' + esc(p.last_used) + ' \u00b7 services ' + esc(p.services.join(" / ")) + '</small>'
      + '<div class="dv-row" style="margin-top:8px"><button type="button" class="ds-btn sm" id="pbView">VIEW PREVIOUS PLAN</button><button type="button" class="ds-btn sm" id="pbLoad">LOAD AS STARTING POINT</button></div>'
      + '<small style="margin-top:6px">A suggestion only: current road and traffic conditions are re-checked.</small></div>';
    $("pbView").onclick = function(){ viewPrevious(p); };
    $("pbLoad").onclick = function(){ loadAsStart(p); };
  }
  function viewPrevious(p){
    G.prev.clearLayers();
    savedBlocks(p.block).forEach(function(b){ rline(b.line, RC.prev, {pane:"block", weight:5, dashArray:"4 6", opacity:1}, G.prev, false).bindTooltip("Previous blockage (" + p.last_used + ")"); });
    modal('<h2 id="modalT">Previous diversion \u2014 ' + esc(p.road) + '</h2><p class="dv-muted">Used ' + esc(p.last_used) + (p.closure_label ? ' \u00b7 closure ' + esc(p.closure_label) : '') + '. Shown in cyan on the map.</p>'
      + p.detail.map(function(d){ return '<div class="dv-plan-row"><b>' + esc(d.service) + '</b><span>D' + d.direction + ' \u2014 ' + esc(d.roads.length ? d.roads.join(" \u2192 ") + " \u2192 rejoin" : (d.action || "wait / regulate")) + '</span></div>'; }).join("")
      + '<div class="foot"><button type="button" class="ds-btn" data-close>Close</button><button type="button" class="ds-btn pri" id="mLoadPrev">Load as starting point</button></div>');
    $("mLoadPrev").onclick = function(){ closeModal(); loadAsStart(p); };
  }
  function loadAsStart(p){
    S.prevChoices = {}; p.detail.forEach(function(d){ S.prevChoices[d.service + "|" + d.direction] = d.signature || "wait"; });
    if(p.closure_label) setClosureLabel(p.closure_label);
    if(savedBlocks(p.block).length){ loadBlocks(savedBlocks(p.block)).then(function(){ resetAnalysis(); analyse(); }); }
    toast("Previous plan loaded as a starting point \u2014 check current conditions.");
  }

  /* ------------------------------------------------------------------ PLAN: options for one service */
  var optSeq = 0;
  function selectService(k){
    var e = entryBy(k); if(!e) return;
    S.sel = k; S.opts = null; S.optSel = null; stopSim();
    document.querySelectorAll(".dv-svc").forEach(function(b){ b.setAttribute("aria-pressed", String(b.dataset.k === k)); });
    drawOtherRoutes(); ["route", "alts", "sel", "pts", "buses"].forEach(function(x){ G[x].clearLayers(); });
    var my = ++optSeq; S.busy.opt++; setLife();
    DS.loading("optBody", ["Finding road routes around the block (OSRM)\u2026", "Locating where each route leaves and rejoins service " + e.service + "\u2026", "Timing each route on live LTA speed bands\u2026", "Checking restrictions, works and incidents\u2026", "Simulating buses and headway after rejoining\u2026"], "PLANNING DIVERSION \u00b7 " + e.service + " D" + e.direction);
    $("cmpBody").innerHTML = ""; renderDock();
    var others = S.an.entries.filter(function(x){ return key(x) !== k; }).map(function(x){ return {service:x.service, direction:x.direction}; });
    post("/api/diversion/options", {blocks:blocksPayload(), service:e.service, direction:e.direction, run:e.run, closure_min:S.closure.min, bus_type:S.bus, allow_small:S.allowSmall, others:others}, 150000).then(function(j){
      S.busy.opt--; if(my !== optSeq) return;
      if(j.error || !j.ok){ $("optBody").innerHTML = '<p class="dv-err">' + esc(j.error || "Options failed.") + '</p>'; setLife(); return; }
      S.opts = j;
      var prev = S.choices[k] || (S.prevChoices && S.prevChoices[e.service + "|" + e.direction] ? {sig:S.prevChoices[e.service + "|" + e.direction]} : null);
      if(S.pending && S.pending.service === e.service && S.pending.direction === e.direction){ prev = {sig:S.pending.signature || "wait"}; S.pending = null; }
      var match = prev && prev.sig && j.options.filter(function(o){ return o.signature === prev.sig; })[0];
      var rec = j.recommendation || {};
      if(j.wait_max_min != null) S.waitMax = j.wait_max_min;
      if(match && match.permitted === false) match = null;
      S.optSel = match ? match.n : (prev && prev.sig === "wait" && j.wait_allowed ? 0 : (rec.option || rec.route_if_extended || (j.options.length ? j.options[0].n : 0)));
      if(S.choices[k] && !j.wait_allowed && !S.choices[k].option && !(S.choices[k].roads || []).length) delete S.choices[k];
      S.prevMissing = !!(prev && prev.sig && prev.sig !== "wait" && !match);
      if(S.choices[k] && match) S.choices[k] = choiceFrom(e, j, match);
      drawOptions(true); renderAll();
      if(!S.mobile && !S.autoSimDone && S.optSel){ S.autoSimDone = true; openSim(); }   // the recommended route is simulated straight away
      if(S.netPending){ S.netPending = false; runNet(); }
    });
  }
  function optByN(n){ return S.opts ? S.opts.options.filter(function(o){ return o.n === n; })[0] : null; }
  function colFor(n){ return S.opts ? S.opts.compare.filter(function(c){ return c.key === (n ? "o" + n : "none"); })[0] || null : null; }

  /* ---- V16.15 stop-to-stop engine output */
  var CONF_C = {HIGH:C.green, MEDIUM:C.amber, LOW:C.grey};
  function confBadge(o){ return o.confidence ? '<span class="dv-conf" style="--c:' + (CONF_C[o.confidence] || C.grey) + '">' + esc(o.confidence) + (o.score != null ? ' \u00b7 ' + f0(o.score) : '') + '</span> ' : ''; }
  function routeSearch(j){
    var sr = j.search || {}, at = sr.attempts || [];
    if(sr.mode !== "stops") return "";
    var h = '<section class="dv-rsearch" aria-label="Route search"><div class="k">ROUTE SEARCH</div>'
      + (sr.last_reachable ? '<div class="dv-muted" style="margin-bottom:6px">Last reachable stop <b style="color:#fff">' + esc(sr.last_reachable.code) + ' ' + esc(sr.last_reachable.name) + '</b>' + (sr.first_after ? ' \u00b7 first stop after the blockage <b style="color:#fff">' + esc(sr.first_after.code) + ' ' + esc(sr.first_after.name) + '</b>' : '') + ' \u00b7 ' + f0(sr.buffer_m) + ' m exclusion zone</div>' : '')
      + '<ol>' + at.map(function(a){ var ok = a.result === "valid";
          return '<li class="' + (ok ? "ok" : "no") + '"><span class="ic" aria-hidden="true">' + (ok ? "\u2713" : "\u2715") + '</span><span class="pr">' + (a.earlier ? '<em>earlier</em> ' : '') + esc(a.from_code) + ' \u2192 ' + esc(a.to_code) + '<small>' + esc(a.from_name) + ' \u2192 ' + esc(a.to_name) + ' \u00b7 skips ' + a.skips + '</small></span><span class="rs">' + (ok ? "Valid \u2014 " : "") + esc(a.reason) + '</span></li>'; }).join("") + '</ol>';
    var sel = at.filter(function(a){ return a.result === "valid"; }).pop();
    h += sel ? '<div class="dv-muted">Selected: <b style="color:#fff">' + esc(sel.from_code) + ' \u2192 ' + esc(sel.to_code) + '</b>' + (sel.earlier ? ' \u2014 <b style="color:#ffc56b">earlier diversion point required</b> (no valid road exit after the last reachable stop)' : '') + ' \u00b7 ' + f0(sr.routes_tested) + ' road routes checked</div>'
      : '<div class="dv-muted">' + esc(sr.summary || "") + '</div>';
    return h + '</section>';
  }
  function optSummary(o){
    var lr = o.last_reachable || {}, rj = o.rejoin_target || {}, nx = o.next_leg || {}, ev = o.evidence || {};
    var row = function(k, v){ return '<dt>' + k + '</dt><dd>' + v + '</dd>'; };
    return (o.earlier ? '<div class="dv-warn" style="margin:0 0 8px">Earlier diversion point required \u2014 no valid road exit after the last reachable stop, so the bus diverts from ' + esc(lr.code || "") + ' ' + esc(lr.name || "") + '.</div>' : '')
      + '<dl class="dv-out">'
      + row("Diversion start", '<b>' + esc(lr.code || "\u2013") + '</b> ' + esc(lr.name || ""))
      + row("Rejoin stop", '<b>' + esc(rj.code || "\u2013") + '</b> ' + esc(rj.name || ""))
      + row("Next verified stop", nx.ok ? '<b>' + esc(nx.next_code || "\u2013") + '</b> ' + esc(nx.next_name || "") : '<span style="color:#ffc3ca">' + esc(nx.detail || "not verified") + '</span>')
      + row("Skipped stops", o.skipped.length ? o.skipped.map(function(x){ return esc(x.code); }).join(", ") + ' (' + o.skipped_n + ')' : 'none')
      + row("Diversion distance", f1(o.div_km) + ' km')
      + row("Normal section", f1(o.normal_km) + ' km')
      + row("Additional distance", sgn(o.added_km, 1) + ' km')
      + row("Est. additional time", sgn(o.added_min) + ' min')
      + row("Bus-road confidence", '<b style="color:' + (CONF_C[o.confidence] || C.grey) + '">' + esc(o.confidence || "") + '</b> \u00b7 ' + esc(ev.text || ""))
      + '</dl><ul class="dv-checks">' + (o.checks || []).map(function(c){ return '<li class="' + (c[1] ? "ok" : "no") + '">' + (c[1] ? "\u2713" : "\u2715") + ' ' + esc(c[0]) + '</li>'; }).join("") + '</ul>'
      + (o.score_parts ? '<details class="dv-why" style="margin:4px 0 8px"><summary>Score ' + f0(o.score) + '/100 \u2014 how it is made</summary><div class="st">Bus-road confidence ' + f1(o.score_parts.busroad) + '/30 \u00b7 road class ' + f1(o.score_parts.roadclass) + '/20 \u00b7 added time ' + f1(o.score_parts.time) + '/15 \u00b7 added distance ' + f1(o.score_parts.dist) + '/15 \u00b7 skipped stops ' + f1(o.score_parts.skip) + '/10 \u00b7 simplicity ' + f1(o.score_parts.simple) + '/10. Scored only after every hard check passed; a score never overrides a failed check.</div></details>' : '');
  }
  function rcBar(rc){
    if(!rc) return "";
    var w = function(x){ return Math.round((x || 0) * 100) + "%"; }, cls = rc.label.split(" ")[0];
    return '<div class="dv-rc"><div class="top"><span class="lbl ' + cls + '">' + esc(rc.label) + '</span></div><div class="bar" role="img" aria-label="' + esc(rc.text) + '"><i class="ma" style="width:' + w(rc.major) + '"></i><i class="me" style="width:' + w(rc.medium) + '"></i><i class="sm" style="width:' + w(rc.small) + '"></i><i class="un" style="width:' + w(rc.unknown) + '"></i></div><small>' + esc(rc.text) + '</small></div>';
  }
  function recBox(j){
    var rec = j.recommendation; if(!rec) return "";
    var li = function(a, c){ return a.map(function(x){ return '<li' + (c ? ' class="c"' : '') + '>' + esc(x) + '</li>'; }).join(""); };
    return '<div class="dv-recwrap">' + DS.ai({title:"AI DIVERSION RECOMMENDATION \u00b7 " + j.service + " D" + j.direction,
      badge:rec.action === "divert" ? DS.sev("ok", "DIVERT") : rec.action === "wait" ? DS.sev("warn", "WAIT / REGULATE") : rec.action === "review" ? DS.sev("warn", "CONTROLLER REVIEW") : DS.sev("crit", "NO VALID DIVERSION"),
      detected:esc(j.road || roadsTxt()) + " blocked \u2014 " + (j.blocked_stops.length ? j.blocked_stops.length + " stop(s) inaccessible" : "section with no stop"),
      recommendation:'<b style="color:#fff">' + esc(rec.headline) + '</b><ul>' + li(rec.reasons) + li(rec.cautions, true) + '</ul>',
      effect:j.wait_or_divert ? esc(j.wait_or_divert.text) : "",
      basis:"LTA diversion rules: 1 safety \u2014 main roads only \u2192 2 fewest bus stops skipped \u2192 3 no U-turn. Then added time. No score. Controller confirms."})
      + '<div class="dv-row" style="margin:-4px 0 10px">' + (rec.option || rec.route_if_extended ? '<button type="button" class="ds-btn sm pri" id="recSim">\u25b6 SIMULATE RECOMMENDED ROUTE</button>' : '')
      + '<button type="button" class="ds-btn sm" id="recAdopt">ADD RECOMMENDATION TO PLAN</button></div></div>';
  }
  function setMainOnly(on){ $("mainOnly").checked = on; S.allowSmall = !on; S.netPending = true; rerunOptions(); }
  function renderOptions(){
    var el = $("optBody"), j = S.opts;
    if(!hasBlock()){ el.innerHTML = DS.empty({icon:"route", title:"NO BLOCKAGE", text:"Place a road block to generate diversion options."}); $("optSub").textContent = ""; return; }
    if(!j){ if(!S.busy.opt) el.innerHTML = S.an && !S.an.entries.length ? '<p class="dv-muted">No service to divert.</p>' : '<p class="dv-muted">Select an affected service.</p>'; return; }
    $("optSub").textContent = j.service + " D" + j.direction + " \u00b7 " + j.options.length + " feasible";
    var rec = j.recommendation || {}, h = recBox(j);
    if(j.small_hidden) h += '<div class="dv-note">\u26d4 ' + j.small_hidden + ' route(s) held back by LTA rule 1 \u2014 not a bus road, or no double-deck service there (' + esc(j.small_hidden_roads.map(tc).join("; ")) + ').'
      + '<br><button type="button" class="ds-btn sm ghost" id="showSmall">Show them for reference (not permitted)</button></div>';
    if(!j.options.length && j.small_hidden) h += '<p class="dv-muted">If a road is safe for double-deckers but no double-deck service is recorded there, check the services in <b>Bus types</b> (Layers panel) against landtransportguru.net.</p>';
    else if(S.allowSmall) h += '<div class="dv-note">Routes that fail LTA rule 1 (not a bus road, or no double-deck service there) are shown for reference only and cannot be selected. <br><button type="button" class="ds-btn sm ghost" id="hideSmall">Main roads only</button></div>';
    h += routeSearch(j);
    if(j.last_point){ var lb = (j.buses || []).filter(function(b){ return b.m_to_exit != null; }).sort(function(a, b){ return a.m_to_exit - b.m_to_exit; })[0];
      h += '<div class="dv-last"><span class="ic" aria-hidden="true">&#x26a0;</span><div><b>LAST DIVERSION POINT</b><span>' + (lb ? km(lb.m_to_exit) + ' ahead of ' + esc(lb.label) + (lb.min_to_exit != null ? ' \u00b7 ' + (lb.min_to_exit < 1 ? 'under 1 min' : 'approx. ' + f0(lb.min_to_exit) + ' min') : '') : km(j.last_point.before_block_m) + ' before the block') + '</span>'
        + '<small>Turn into ' + esc(j.last_point.road || "the diversion") + ' (option ' + j.last_point.option + '). A bus past this junction can no longer use the proposed diversion.</small></div></div>'; }
    if(j.wait_or_divert) h += '<div class="dv-wod' + (j.wait_or_divert.kind === "divert_only" ? " only" : "") + '"><b>' + (j.wait_or_divert.kind === "wait" ? "WAIT / REGULATE vs DIVERT" : j.wait_or_divert.kind === "divert_only" ? "ALWAYS DIVERT \u2014 BUSES ARE NEVER HELD AT A BLOCKAGE" : "DIVERT vs WAIT") + '</b>' + esc(j.wait_or_divert.text) + '</div>';
    if((j.trapped || []).length) h += '<div class="dv-trap"><div class="k">\u26a0 PAST THE LAST DIVERSION POINT (' + j.trapped.length + ')</div>'
      + j.trapped.map(function(t){ return '<div class="it"><b>' + esc(t.label) + '</b>' + (t.escape ? '<span class="ok">Route out, no U-turn: ' + esc(t.escape.roads.map(tc).join(" \u2192 ")) + ' \u2192 rejoin \u00b7 ' + f1(t.escape.km) + ' km, ~' + f0(t.escape.min) + ' min (' + esc(t.escape.time_src) + ')</span>' : '<span class="no">' + esc(t.note || "Unable to move") + '</span>') + '</div>'; }).join("")
      + '<small class="dv-muted">' + (j.trapped.some(function(t){ return t.escape; }) ? 'Routes out are shown dashed cyan on the map. ' : '') + 'Driver instruction by OCC; operational verification required.</small></div>';
    if(S.prevMissing) h += '<div class="dv-warn">The previous plan\u2019s road sequence was not found among today\u2019s feasible routes. Check current conditions.</div>';
    if(!j.options.length){
      h += '<div class="dv-warn"><b>NO VALID DIVERSION FOUND.</b> ' + esc((j.search || {}).summary || "") + ' A correct \u201cno valid diversion\u201d is better than a wrong one: escalate to the Duty Operations Manager / depot. ROUTE SEARCH shows why each stop pair failed; DRAW ROUTE lets you propose roads for the engine to verify.</div>';
    }
    j.options.forEach(function(o){
      var sel = S.optSel === o.n, isRec = rec.option === o.n, isExt = rec.route_if_extended === o.n, np = o.permitted === false;
      var rules = optSummary(o);
      h += '<article class="dv-opt ' + (np ? "na" : sel ? "sel" : "alt") + (isRec ? " rec" : "") + '" aria-label="' + o.name + (isRec ? ", recommended" : "") + '"><h3>' + o.name + '<span class="sp"></span>'
        + (o.controller_route ? '<span class="dv-badge" title="The route you drew, checked against the LTA rules">CONTROLLER ROUTE</span> ' : '') + ((o.shared_with || []).length && !np && !o.controller_route ? '<span class="dv-badge alt" title="The same diversion corridor another affected service uses">SAME AS ' + esc(o.shared_with.join(", ")) + '</span> ' : '') + confBadge(o) + (np ? DS.sev("crit", "NOT PERMITTED") : isRec ? '<span class="dv-badge">\u2605 RECOMMENDED</span>' : o.confidence === "LOW" ? '<span class="dv-badge alt">CONTROLLER REVIEW REQUIRED</span>' : '') + (sel && !np ? ' ' + DS.sev("ok", "SELECTED") : "") + '</h3>' + rules + rcBar(o.road_class) + '<ul class="dv-chain">'
        + o.roads.map(function(r){ return '<li>' + esc(tc(r)) + '</li>'; }).join("") + '<li class="rj">\u21aa REJOIN NORMAL ROUTE' + (o.rejoin_target ? ' \u00b7 ' + esc(o.rejoin_target.name) : (o.rejoin_stop ? ' \u00b7 ' + esc(o.rejoin_stop.name) : '')) + '</li></ul>'
        + '<div class="dv-lbl" style="margin-top:0">SKIPPED STOPS: ' + o.skipped_n + '</div><div class="dv-skip">' + o.skipped.map(function(s){ return '<span class="' + (s.important.length ? "imp" : "") + '" title="' + esc(s.name + (s.important.length ? " \u2014 " + s.important.join(", ") : "")) + '">' + (s.important.length ? "\u26a0" : "\u2715") + ' ' + esc(s.code) + '</span>'; }).join("") + (o.skipped_n ? "" : '<span style="background:none;border-color:var(--ds-line);color:var(--ds-mut)">none</span>') + '</div>'
        + (o.important_n ? '<div class="dv-warn">\u26a0 ' + o.skipped.filter(function(s){ return s.important.length; }).map(function(s){ return esc(s.name) + " (" + esc(s.important.join(", ")) + ")"; }).join("; ") + '</div>' : '')
        + '<dl class="dv-kv"><dt>Added distance</dt><dd><b>' + sgn(o.added_km, 1) + ' km</b></dd><dt>Est. additional running time</dt><dd><b>' + sgn(o.added_min) + ' min</b></dd>'
        + '<dt>Affected buses (can divert)</dt><dd><b>' + o.affected_buses + '</b></dd><dt>Important stops skipped</dt><dd><b style="color:' + (o.important_n ? C.amber : "#fff") + '">' + o.important_n + '</b></dd>'
        + '<dt>Traffic condition</dt><dd><b style="color:' + (o.traffic === "HEAVY" ? C.red : o.traffic === "MODERATE" ? C.amber : o.traffic === "NORMAL" ? C.green : C.grey) + '">' + o.traffic + '</b></dd>'
        + '<dt>Turns \u00b7 sharp turns</dt><dd><b>' + o.n_turns + ' \u00b7 ' + o.n_sharp + '</b></dd>'
        + '<dt>Operational feasibility</dt><dd><b style="color:' + (o.feasibility === "HIGH" ? C.green : o.feasibility === "NOT SUITABLE" ? C.red : C.amber) + '">' + o.feasibility + '</b></dd></dl>'
        + '<p class="dv-feas">' + esc(o.feasibility_text) + (o.findings.length ? ' ' + o.findings.length + ' map finding(s): ' + esc(o.findings.slice(0, 2).map(function(f){ return f.text; }).join(" \u00b7 ")) : '') + '<br>Road route: ' + esc(o.router || "OSRM") + ' \u00b7 time: ' + esc(o.time_src) + ' \u00b7 traffic: ' + esc(o.traffic_basis) + (o.also ? '<br>Same diversion also fits: ' + esc(o.also.join(", ")) : '') + '</p>'
        + '<div class="dv-acts"><button type="button" class="ds-btn sm ghost" data-v="' + o.n + '">VIEW ON MAP</button><button type="button" class="ds-btn sm ghost" data-s="' + o.n + '">SIMULATE</button>' + (np ? '<span class="dv-muted" style="align-self:center">Reference only \u2014 LTA rule 1</span>' : '<button type="button" class="ds-btn sm ' + (sel ? "pri" : "") + '" data-p="' + o.n + '">' + (sel ? "SELECTED" : "SELECT") + '</button>') + '</div></article>';
    });
    if(j.wait_allowed) h += '<article class="dv-opt ' + (S.optSel === 0 ? "sel" : "") + '"><h3>NO DIVERSION \u2014 WAIT / REGULATE<span class="sp"></span>' + (S.optSel === 0 ? DS.sev("ok", "SELECTED") : "") + '</h3>'
      + '<p class="dv-feas">Short closure (' + esc(j.closure_desc) + '): buses hold upstream or wait at the block until it reopens. No stops skipped.</p>'
      + '<div class="dv-acts"><button type="button" class="ds-btn sm ghost" data-s="0">SIMULATE</button><button type="button" class="ds-btn sm ' + (S.optSel === 0 ? "pri" : "") + '" data-p="0">' + (S.optSel === 0 ? "SELECTED" : "SELECT") + '</button></div></article>';

    h += whyPanel(j);
    h += '<p class="dv-muted">Ordered by the LTA rules: safety first (bus roads only; double-deck only where DD services run), then fewest stops skipped, then important stops, then added running time \u2014 no combined score. ' + j.candidates_tested + ' road routes tested; ' + j.rejected.uses_block + ' rejected for using the blocked road' + (j.rejected.uturn ? ', ' + j.rejected.uturn + ' for needing a U-turn' : '') + '. Buses are never routed through a U-turn (including turning back at a roundabout or round a block).</p>';
    el.innerHTML = h;
    var sb = $("showSmall"), hb = $("hideSmall");
    if(sb) sb.onclick = function(){ setMainOnly(false); }; if(hb) hb.onclick = function(){ setMainOnly(true); };
    var rs = $("recSim"), ra = $("recAdopt");
    if(rs) rs.onclick = function(){ S.optSel = rec.option || rec.route_if_extended || 0; drawOptions(false); renderAll(); openSim(); };
    if(ra) ra.onclick = function(){ if(rec.action === "manual"){ toast("No diversion to add \u2014 escalate to the Duty Operations Manager."); return; } S.optSel = rec.option || 0; choose(); drawOptions(false); renderAll(); };
    el.querySelectorAll("[data-v]").forEach(function(b){ b.onclick = function(){ S.optSel = +b.dataset.v; drawOptions(true); renderAll(); if(S.mobile) sheetTo("peek"); }; });
    el.querySelectorAll("[data-s]").forEach(function(b){ b.onclick = function(){ S.optSel = +b.dataset.s; drawOptions(false); renderAll(); openSim(); }; });
    el.querySelectorAll("[data-p]").forEach(function(b){ b.onclick = function(){ S.optSel = +b.dataset.p; choose(); drawOptions(false); renderAll(); }; });
  }

  function choiceFrom(e, j, o){
    var col = colFor(o ? o.n : 0);
    return {service:e.service, direction:e.direction, run:e.run, option:o ? o.n : 0, sig:o ? o.signature : "wait", action:o ? o.name + " \u2014 divert" : "Wait / regulate (no diversion)",
            roads:o ? o.roads.map(tc) : [], signature:o ? o.signature : "", leave_pt:o ? o.leave_pt : null, rejoin_pt:o ? o.rejoin_pt : null, leave_s:o ? o.leave_s : null, rejoin_s:o ? o.rejoin_s : null,
            block_a:j.block_a, block_b:j.block_b, line:o ? o.line : [], rejoin_name:o && o.rejoin_stop ? o.rejoin_stop.name : "",
            skipped:o ? o.skipped.map(function(s){ return {code:s.code, name:s.name, important:!!s.important.length}; }) : [],
            buses:o ? o.affected_buses : (col ? col.affected : 0), holds:col ? col.reg.holds.filter(function(h){ return h.hold >= .5; }).map(function(h){ return {label:h.label, action:h.action}; }) : [],
            added_min:o ? o.added_min : null, road_class:o && o.road_class ? o.road_class.label : "",
            permitted:o ? o.permitted !== false : true, bus_road:o && o.bus_road ? o.bus_road.text : "", dd:o && o.bus_road ? o.bus_road.dd : ""};
  }
  function choose(){
    var e = entryBy(S.sel); if(!e || !S.opts) return;
    var so = optByN(S.optSel);
    if(so && so.permitted === false){ toast("LTA rule 1 (safety): " + so.name + " \u2014 " + ((so.bus_road || {}).text || "not a bus road") + ". It cannot be selected."); return; }
    if(!optByN(S.optSel) && !S.opts.wait_allowed){ toast("Closure " + S.closure.label + ": buses cannot wait. Choose a diversion" + (S.opts.options.length ? "." : " \u2014 none found, escalate.")); return; }
    S.choices[S.sel] = choiceFrom(e, S.opts, optByN(S.optSel));
    toast(e.service + " D" + e.direction + ": " + S.choices[S.sel].action + " added to the plan");
  }

  /* ------------------------------------------------------------------ map: route, block stops, options, buses */
  function drawOptions(fit){
    ["route", "alts", "sel", "pts", "buses"].forEach(function(x){ G[x].clearLayers(); });
    var j = S.opts; if(!j) return;
    rline(j.service_line, RC.route, {pane:"routes", weight:6, opacity:1}, G.route, true).bindTooltip("Service " + j.service + " D" + j.direction + " \u2014 normal route", {sticky:true}).addTo(G.route);
    var so0 = optByN(S.optSel);
    if(so0 && so0.leave_s != null && so0.rejoin_s != null && j.service_line.length > 1){     // the original section the diversion skips
      var scm = cumM(j.service_line), skipLn = cutLine(j.service_line, scm, so0.leave_s, so0.rejoin_s);
      L.polyline(skipLn, {pane:"routes", color:"#ffffff", weight:9, opacity:.7, interactive:false}).addTo(G.route);
      L.polyline(skipLn, {pane:"routes", color:"#7a1626", weight:5, opacity:1, dashArray:"2 7"}).bindTooltip("Skipped original section (" + so0.skipped_n + " stop" + (so0.skipped_n === 1 ? "" : "s") + ")", {sticky:true}).addTo(G.route);
    }
    j.blocked_stops.forEach(function(s){ L.marker([s.lat, s.lon], {pane:"pts", icon:divIcon("\u2715", "dv-stopx")}).bindTooltip("Inaccessible: " + s.code + " " + s.name).addTo(G.pts); });
    var bounds = L.latLngBounds(j.block_pts);
    j.options.forEach(function(o){
      var sel = S.optSel === o.n;
      var small = o.road_class && o.road_class.label === "SMALL ROADS";
      var bad = small || o.permitted === false;      // fails LTA rule 1: never drawn like an approved diversion, even when viewed
      var pl = rline(o.line, bad ? RC.small : sel ? RC.sel : RC.alt, {pane:sel ? "divsel" : "alts", weight:sel ? 6 : bad ? 4 : 5, opacity:sel ? 1 : .9, casing:sel ? .95 : .75,
          dashArray:bad ? "2 8" : sel ? "14 8" : "8 8", className:sel && !bad ? "dv-sel" : ""}, sel ? G.sel : G.alts, !bad || sel)
        .bindTooltip(o.name + ": " + o.roads.map(tc).join(" \u2192 ") + " (" + sgn(o.added_min) + " min)", {sticky:true}).on("click", function(){ S.optSel = o.n; drawOptions(false); renderAll(); });
      pl.addTo(sel ? G.sel : G.alts); bounds.extend(o.line);
      if(sel){
        o.skipped.forEach(function(s){ if(s.in_block) return; L.marker([s.lat, s.lon], {pane:"pts", icon:divIcon(s.important.length ? "!" : "\u2715", "dv-stopx" + (s.important.length ? " imp" : ""))}).bindTooltip("Skipped: " + s.code + " " + s.name + (s.important.length ? " \u2014 " + s.important.join(", ") : "")).addTo(G.pts); });
        L.marker(o.rejoin_pt, {pane:"bus", icon:divIcon('<i>\u21aa</i>REJOIN', "dv-rejoin")}).bindTooltip("Rejoin normal route" + (o.rejoin_stop ? " before " + o.rejoin_stop.name : "")).addTo(G.pts);
        L.marker(o.leave_pt, {pane:"bus", icon:divIcon('<i>\u21b1</i>LEAVE ROUTE', "dv-lastpt")}).addTo(G.pts);
      }
    });
    if(j.last_point && (S.optSel === 0 || !optByN(S.optSel) || optByN(S.optSel).n !== j.last_point.option))
      L.marker([j.last_point.lat, j.last_point.lon], {pane:"bus", icon:divIcon('<i>\u26a0</i>LAST DIVERSION POINT', "dv-lastpt")}).bindTooltip(km(j.last_point.before_block_m) + " before the block").addTo(G.pts);
    else if(j.last_point){ G.pts.eachLayer(function(m){ if(m.options.icon && m.options.icon.options.html.indexOf("LEAVE ROUTE") > 0) m.setIcon(divIcon('<i>\u26a0</i>LAST DIVERSION POINT', "dv-lastpt")); }); }
    j.buses.concat(j.buses_on_diversion || []).forEach(function(b){
      var tr = (j.trapped || []).filter(function(x){ return x.label === b.label; })[0];
      var t = tr ? (tr.escape ? "ROUTE OUT" : "\u26d4 UNABLE TO MOVE") : b.min_to_exit != null ? f0(b.min_to_exit) + " MIN" : stTag(b.status);
      L.marker([b.lat, b.lon], {pane:"bus", icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dv-bus' + (b.test ? ' test' : '') + '" style="--c:' + (tr ? (tr.escape ? C.cyan : C.red) : stColor(b.status)) + '"><b>' + esc(j.service) + '</b>' + esc(b.label.slice(j.service.length)) + (b.test ? ' <small>TEST</small>' : '') + ' <em>' + esc(t) + '</em></div>'})})
        .bindPopup('<b>Bus ' + esc(b.label) + '</b> \u00b7 D' + j.direction + '<br>' + esc(b.status_text) + (b.min_to_exit != null ? '<br>' + f1(b.min_to_exit) + ' min to the diversion point (' + esc(b.eta_basis) + ')' : '') + (b.gap_ahead_min != null ? '<br>Gap to bus ahead ' + f1(b.gap_ahead_min) + ' min' + (j.H ? ' (scheduled ' + f0(j.H) + ')' : '') : '') + (b.load ? '<br>Load ' + esc(b.load) : '') + '<br><small class="dv-muted">IDs are positional: LTA Bus Arrival gives no registration.</small>')
        .addTo(G.buses);
    });
    var so_ = optByN(S.optSel);
    if(so_ && so_.bus_road){
      (so_.bus_road.gaps || []).forEach(function(g){ L.marker([g.lat, g.lon], {pane:"bus", icon:divIcon('<i>\u2715</i>NOT A BUS ROAD \u00b7 ' + esc(tc(g.road)), "dv-lastpt")}).bindTooltip(g.m + " m with no bus service in this direction").addTo(G.pts); });
      (so_.bus_road.sd_only || []).forEach(function(g){ L.marker([g.lat, g.lon], {pane:"bus", icon:divIcon('<i>\u2715</i>SINGLE-DECK ONLY \u00b7 ' + esc(tc(g.road)), "dv-lastpt")}).bindTooltip(g.m + " m where only single-deck services run").addTo(G.pts); });
      if(S.bus === "dd") (so_.bus_road.unknown || []).forEach(function(g){ L.marker([g.lat, g.lon], {pane:"bus", icon:divIcon('<i>?</i>DD TO VERIFY \u00b7 ' + esc(tc(g.road)), "dv-lastpt")}).bindTooltip(g.m + " m: bus road, double-deck operation not yet confirmed").addTo(G.pts); });
    }
    (j.trapped || []).forEach(function(t){
      if(t.escape){ rline(t.escape.line, RC.esc, {pane:"divsel", weight:5, dashArray:"5 7", opacity:1}, G.sel, true).bindTooltip(t.label + " route out (no U-turn): " + t.escape.roads.map(tc).join(" \u2192 "), {sticky:true}).addTo(G.sel); bounds.extend(t.escape.line); }
    });
    if(fit) map.fitBounds(bounds.pad(.15), {maxZoom:17});
  }

  /* ------------------------------------------------------------------ ALL SERVICES: network plan */
  var NETC = ["#00955a", "#0a7ea8", "#7b3fd4", "#b58a00", "#c2185b", "#00838f", "#5b8f00", "#c25a00"];
  var netSeq = 0;
  function netColor(sig){ var g = S.net ? S.net.network.groups : []; for(var i = 0; i < g.length; i++) if(g[i].signature === sig) return NETC[i % NETC.length]; return C.grey; }
  function runNet(){
    if(!S.an || !S.an.entries.length){ S.net = null; renderNet(); return; }
    var my = ++netSeq; S.busy.net = 1; setLife();
    var ld = DS.loading("netBody", ["Planning a diversion for every affected service\u2026", "Keeping each route on expressways and arterial roads\u2026", "Finding services that can share one diversion\u2026", "Checking how many buses each diverted road takes\u2026"], "NETWORK PLAN \u00b7 " + S.an.entries.length + " SERVICE-DIRECTION" + (S.an.entries.length === 1 ? "" : "S"));
    post("/api/diversion/plan_all", {blocks:blocksPayload(), closure_min:S.closure.min, bus_type:S.bus, allow_small:S.allowSmall,
      entries:S.an.entries.map(function(e){ return {service:e.service, direction:e.direction, run:e.run}; })}, 300000).then(function(j){
      ld.stop(); if(my !== netSeq) return; S.busy.net = 0;
      if(!j.ok){ $("netBody").innerHTML = '<p class="dv-err">' + esc(j.error || "Network plan failed.") + '</p>'; setLife(); return; }
      S.net = j; renderNet(); drawNet(); setLife();
    });
  }
  function renderNet(){
    var el = $("netBody"), j = S.net;
    if(!hasBlock()){ el.innerHTML = DS.empty({icon:"route", title:"NO NETWORK PLAN YET", text:"Once a blockage is placed, every affected service gets a recommended main-road diversion here."}); $("netSub").textContent = ""; return; }
    if(!j){ if(!S.busy.net) el.innerHTML = '<p class="dv-muted">' + (S.an && !S.an.entries.length ? "No service runs along the blocked section." : "Planning starts when the analysis finishes.") + '</p>'; $("netSub").textContent = ""; return; }
    var t = j.totals, ACT = {divert:"DIVERT", wait:"WAIT / REGULATE", manual:"NO VALID DIVERSION", review:"CONTROLLER REVIEW"};
    $("netSub").textContent = t.service_dirs + " service-direction" + (t.service_dirs === 1 ? "" : "s");
    var shared = j.network.groups.filter(function(g){ return g.services.length > 1; });
    var h = DS.ai({title:"AI NETWORK PLAN", badge:j.network.warnings.length ? DS.sev("warn", j.network.warnings.length + " WARNING" + (j.network.warnings.length > 1 ? "S" : "")) : DS.sev("ok", "READY TO REVIEW"),
      detected:esc(j.road) + (j.blocks_n > 1 ? " \u2014 " + j.blocks_n + " blockages" : "") + ": " + t.service_dirs + " service-direction(s) affected",
      recommendation:t.divert + " divert" + (t.wait ? ", " + t.wait + " wait / regulate" : "") + (t.manual ? ", <b style=\"color:#ffc3ca\">" + t.manual + " need escalation (no route meets the LTA rules)</b>" : "") + (shared.length ? "; " + shared.length + " route(s) shared by several services" : ""),
      effect:t.bus_min_none == null ? "Closure " + esc(S.closure.label.toLowerCase()) + ": with no action, every bus that reaches a block is unable to move until it reopens. This plan: " + f0(t.bus_min) + " extra bus-minutes (estimate)."
        : "Extra bus-minutes over the " + esc(S.closure.label.toLowerCase()) + " closure: " + f0(t.bus_min) + " with this plan vs " + f0(t.bus_min_none) + " with no action (estimate).",
      basis:"LTA rules: 1 bus roads only (double-deck only where DD services run), 2 fewest stops skipped, 3 no U-turn" + (j.allow_small ? " (other routes shown for reference, never permitted)" : "") + ". Buses are never held. Controller confirms."});
    h += '<div class="dv-net-tot"><span><b>' + t.divert + '</b>DIVERT</span>' + (t.wait ? '<span><b>' + t.wait + '</b>WAIT</span>' : '') + '<span><b>' + t.manual + '</b>NO VALID ROUTE</span>' + (t.review ? '<span><b>' + t.review + '</b>REVIEW</span>' : '') + '<span hidden></span><span><b>' + t.skipped + '</b>STOPS SKIPPED' + (t.important ? ' \u00b7 ' + t.important + ' IMP.' : '') + '</span></div>';
    j.network.warnings.forEach(function(w){ h += '<div class="dv-warn">\u26a0 ' + esc(w) + '</div>'; });
    if(shared.length) h += '<div class="dv-lbl">SHARED DIVERSIONS</div>' + shared.map(function(g){ return '<div class="dv-grp"><i style="background:' + netColor(g.signature) + '"></i><span><b style="color:#fff">' + esc(g.services.join(", ")) + '</b> \u2014 ' + esc(g.roads.map(tc).join(" \u2192 ")) + '</span></div>'; }).join("");
    h += '<div class="dv-lbl">EVERY AFFECTED SERVICE</div>';
    j.rows.forEach(function(r){
      var k = r.service + "|" + r.direction + "|" + r.run, o = r.option, col = r.action === "divert" && o ? netColor(o.signature) : r.action === "wait" ? C.amber : C.red;
      var route = !r.ok ? (r.error || "Could not plan") : r.action === "divert" && o ? o.roads.map(tc).join(" \u2192 ") + " \u2192 rejoin" + (o.rejoin_stop ? " (" + o.rejoin_stop.name + ")" : "")
        : r.action === "wait" ? (o ? "Hold / regulate; ready if extended: " + o.roads.map(tc).join(" \u2192 ") : "Hold / regulate upstream") : r.action === "review" ? "Only a low-confidence route was found \u2014 controller review required" : "No valid diversion found \u2014 no stop pair could be connected by a verified road route; escalate";
      var meta = r.action === "divert" && o ? [(o.added_min >= 0 ? "+" : "") + f0(o.added_min) + " min", (o.added_km >= 0 ? "+" : "") + f1(o.added_km) + " km", o.skipped_n + " stops skipped" + (o.important_n ? " (" + o.important_n + " important)" : ""), o.road_class ? o.road_class.label : "", "traffic " + o.traffic] : [];
      if(r.ok) meta.push(r.polled ? r.buses + " bus" + (r.buses === 1 ? "" : "es") + " approaching" : "buses not polled");
      if(r.small_hidden) meta.push(r.small_hidden + " route(s) held back by LTA rule 1");
      if(r.action === "divert" && o && (o.shared_with || []).length) meta.unshift("same corridor as " + o.shared_with.join(", "));
      if(o && o.confidence) meta.unshift(o.confidence + " confidence" + (o.last_reachable && o.rejoin_target ? " \u00b7 " + o.last_reachable.code + " \u2192 " + o.rejoin_target.code : "") + (o.earlier ? " (earlier diversion point)" : ""));
      if(r.action === "review") meta.unshift("controller review required \u2014 low confidence only");
      if(r.action === "manual" && (r.donors_checked || []).length) meta.push("checked the corridor" + (r.donors_checked.length > 1 ? "s" : "") + " of " + r.donors_checked.join(", ") + " \u2014 not valid for this service / direction");
      h += '<button type="button" class="dv-net-row" data-k="' + esc(k) + '" aria-pressed="' + (S.sel === k) + '"><i class="sw" style="background:' + col + '"></i><b>' + esc(r.service) + '</b><span class="d">D' + r.direction + (r.run ? " \u00b7 pass " + (r.run + 1) : "") + '</span><span class="act ' + r.action + '">' + (ACT[r.action] || "") + '</span>'
        + '<span class="r">' + esc(route) + '</span><span class="m">' + esc(meta.filter(Boolean).join(" \u00b7 ")) + '</span></button>';
    });
    h += '<div class="dv-row" style="margin-top:10px"><button type="button" class="ds-btn sm pri" id="netAdopt"' + (lockedPlan() ? " disabled" : "") + '>ADOPT ALL INTO PLAN</button>'
      + '<button type="button" class="ds-btn sm" id="netMap" aria-pressed="' + S.showNet + '">' + (S.showNet ? "HIDE ROUTES ON MAP" : "SHOW ALL ROUTES ON MAP") + '</button>'
      + (j.ai ? '<button type="button" class="ds-btn sm" id="netAi">\u2728 AI REVIEW</button>' : '') + '<button type="button" class="ds-btn sm ghost" id="netRe">RE-PLAN</button></div><div id="aiBox"></div>'
      + '<p class="dv-muted" style="margin-top:8px">' + esc(j.basis) + (j.rows.some(function(r){ return r.ok && !r.polled; }) ? ' Live buses are polled for the first ' + j.poll_cap + ' service-directions (LTA quota).' : '') + '</p>';
    el.innerHTML = h;
    el.querySelectorAll(".dv-net-row").forEach(function(b){ b.onclick = function(){ selectService(b.dataset.k); if(S.mobile) sheetTab("opt"); }; });
    $("netAdopt").onclick = adoptAll;
    $("netMap").onclick = function(){ S.showNet = !S.showNet; lay.net = S.showNet; var c = document.querySelector('#layers input[data-l="net"]'); if(c) c.checked = S.showNet; applyLayers(); renderNet(); };
    $("netRe").onclick = runNet;
    if($("netAi")) $("netAi").onclick = aiReview;
  }
  function drawNet(){
    G.net.clearLayers(); if(!S.net) return;
    var labelled = {};
    S.net.rows.forEach(function(r){
      var o = r.option; if(!r.ok || !o || r.action !== "divert" || !o.line || o.line.length < 2) return;
      var c = netColor(o.signature);
      rline(o.line, c, {pane:"alts", weight:5, opacity:1, dashArray:"10 6"}, G.net, false).bindTooltip(r.service + " D" + r.direction + ": " + o.roads.map(tc).join(" \u2192 "), {sticky:true})
        .on("click", function(){ selectService(r.service + "|" + r.direction + "|" + r.run); }).addTo(G.net);
      if(!labelled[o.signature]){
        labelled[o.signature] = 1;
        var g = S.net.network.groups.filter(function(x){ return x.signature === o.signature; })[0], mid = o.line[Math.floor(o.line.length / 2)];
        L.marker(mid, {pane:"pts", interactive:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dv-netlbl" style="border-color:' + c + '">' + esc((g ? g.services : [r.service + " D" + r.direction]).join(" \u00b7 ")) + '</div>'})}).addTo(G.net);
      }
    });
  }
  function adoptAll(){
    if(!S.net || lockedPlan()) return;
    var n = 0, manual = 0;
    S.net.rows.forEach(function(r){
      if(!r.ok || r.action === "manual" || r.action === "review" || !r.choice || (r.action === "wait" && !waitOK())){ manual++; return; }
      S.choices[r.service + "|" + r.direction + "|" + r.run] = r.choice; n++;
    });
    toast(n + " service-direction(s) added to the plan" + (manual ? " \u00b7 " + manual + " need manual planning" : ""));
    if(S.sel && S.opts){ var c = S.choices[S.sel]; if(c){ var o = S.opts.options.filter(function(x){ return x.signature === c.signature; })[0]; S.optSel = o ? o.n : 0; drawOptions(false); } }
    renderAll(); if(S.mobile) sheetTab("plan");
  }
  function aiReview(){
    var box = $("aiBox"); if(!box || !S.net) return;
    DS.loading(box, ["Sending the computed plan (no raw feeds)\u2026", "Reviewing conflicts between services\u2026", "Listing what to verify\u2026"], "AI REVIEW");
    post("/api/diversion/ai_review", {plan:S.net}, 90000).then(function(j){
      box.innerHTML = j.ok ? '<div class="dv-ai-rev"><div class="k">\u2728 AI REVIEW \u00b7 ' + esc(j.model) + '</div><pre>' + esc(j.text) + '</pre><small>' + esc(j.note) + '</small></div>'
        : '<p class="dv-err">' + esc(j.error || "AI review unavailable.") + '</p>';
    });
  }

  /* ------------------------------------------------------------------ OPERATIONAL IMPACT comparison (no scores) */
  function hwChain(hw){ return hw && hw.gaps.length ? hw.gaps.slice(0, 6).map(function(g){ return f0(g.gap); }).join(" \u2014 ") + (hw.gaps.length > 6 ? " \u2026" : "") : "\u2013"; }
  function renderCompare(){
    var el = $("cmpBody"), j = S.opts;
    if(!j){ el.innerHTML = hasBlock() ? '<p class="dv-muted">Select a service to compare its options.</p>' : ""; $("impSub").textContent = ""; return; }
    $("impSub").textContent = j.service + " D" + j.direction + (j.H ? " \u00b7 scheduled " + f0(j.H) + " min" : " \u00b7 no scheduled headway");
    var cols = j.compare, H = j.H;
    function cls(v, bad, warn){ return v == null ? "" : v >= bad ? "bad" : v >= warn ? "warn" : "ok"; }
    var rows = [
      ["Skipped stops", function(c){ return f0(c.skipped_n); }],
      ["Added distance", function(c){ return c.key === "none" ? "0" : sgn(c.added_km, 1) + " km"; }],
      ["Additional running time", function(c){ return c.key === "none" ? (c.max_wait != null ? "wait \u2264 " + f0(c.max_wait) + " min" : "until reopened") : sgn(c.added_min) + " min"; }],
      ["Affected buses", function(c){ return f0(c.affected); }],
      ["Important stops", function(c){ return '<span class="' + (c.important_n ? "warn" : "") + '">' + f0(c.important_n) + '</span>'; }],
      ["Maximum gap", function(c){ return '<span class="' + (H ? cls(c.hw.max, H * 1.5, H * 1.2) : "") + '">' + (c.hw.max == null ? "\u2013" : f0(c.hw.max) + " min") + '</span>'; }],
      ["Headway after rejoin", function(c){ return hwChain(c.hw); }],
      ["Bunching risk", function(c){ return '<span class="' + ({high:"bad", moderate:"warn", low:"ok"}[c.hw.risk] || "") + '">' + String(c.hw.risk || "\u2013").toUpperCase() + '</span>'; }],
      ["Extra bus-minutes", function(c){ return c.bus_min == null ? "\u2013" : f0(c.bus_min); }],
      ["Estimated recovery", function(c){ return c.recovery_min == null ? '<span class="warn">not by holding</span>' : "+" + f0(c.recovery_min) + " min"; }]
    ];
    var selKey = S.optSel ? "o" + S.optSel : "none";
    el.innerHTML = '<div class="dv-scroll"><table class="dv-cmp"><thead><tr><th></th>' + cols.map(function(c){ return '<th class="' + (c.key === selKey ? "on" : "") + '">' + esc(c.name) + (c.key === "none" && c.viable === false ? '<br><small style="color:#ffc3ca;letter-spacing:.06em">NOT VIABLE</small>' : '') + '</th>'; }).join("") + '</tr></thead><tbody>'
      + rows.map(function(r){ return '<tr><td>' + r[0] + '</td>' + cols.map(function(c){ return '<td class="' + (c.key === selKey ? "on" : "") + '">' + r[1](c) + '</td>'; }).join("") + '</tr>'; }).join("")
      + '</tbody></table></div><p class="dv-muted">Headways at the rejoin point, predicted from live bus positions and LTA speed bands (' + esc(j.time_basis) + '). Closure: ' + esc(S.closure.label) + '. Consequences only \u2014 the controller decides.</p>';
  }

  /* ------------------------------------------------------------------ SIMULATE: before / after */
  var SIM = {maps:null, raf:0, last:0};
  function openSim(){ S.simSeen = true; if(S.mobile) sheetTab("sim"); else { dockTab("sim"); openDock(true); } setLife(); }
  function stopSim(){ S.simPlay = false; cancelAnimationFrame(SIM.raf); var b = $("simPlay"); if(b) b.textContent = "\u25b6 PLAY"; }
  function simMaps(){
    if(SIM.maps) return SIM.maps;
    var mk = function(id){ var m = L.map(id, {zoomControl:false, attributionControl:false, dragging:true, scrollWheelZoom:false}); SGBasemap.add(m, {style:"dark", switcher:false, minZoom:10, maxZoom:18}); return {map:m, g:L.layerGroup().addTo(m), b:L.layerGroup().addTo(m)}; };
    SIM.maps = {none:mk("simNone"), opt:mk("simOpt")};
    return SIM.maps;
  }
  function renderSim(){
    var el = $("paneSim"), j = S.opts;
    if(!j){ if(SIM.maps){ SIM.maps.none.map.remove(); SIM.maps.opt.map.remove(); SIM.maps = null; } el.innerHTML = DS.empty({icon:"engine", title:"NOTHING TO SIMULATE YET", text:"Place a road block and select a service; each option can then be simulated against doing nothing."}); return; }
    if(!el.offsetWidth || !el.offsetHeight){ SIM.dirty = true; return; }   // build only when visible, so the minimaps fit a real size
    SIM.dirty = false;
    var o = optByN(S.optSel), oc = colFor(o ? o.n : 0);
    if(!$("simNone")){
      if(SIM.maps){ SIM.maps.none.map.remove(); SIM.maps.opt.map.remove(); SIM.maps = null; }
      el.innerHTML = '<div class="dv-simctl"><button type="button" class="ds-btn sm pri" id="simPlay">\u25b6 PLAY</button>'
        + '<div class="ds-segm" id="simJump" role="group" aria-label="Jump to time"><button type="button" data-t="0" aria-pressed="true">NOW</button><button type="button" data-t="5">+5 MIN</button><button type="button" data-t="10">+10 MIN</button><button type="button" data-t="15">+15 MIN</button><button type="button" data-t="30">+30 MIN</button></div>'
        + '<input type="range" id="simRange" min="0" max="30" step="0.1" value="0" aria-label="Simulation time, minutes from now"><span class="dv-simclock" id="simClock"></span>'
        + '<div class="ds-segm dv-simtoggle" id="simWhich"><button type="button" data-w="none">NO DIVERSION</button><button type="button" data-w="opt" aria-pressed="true">DIVERSION</button></div></div>'
        + '<div class="dv-sim one" id="simGrid"><div class="dv-simcell" id="cellNone"><div class="map" id="simNone"></div><span class="tag r">NO DIVERSION</span><div class="stat" id="statNone"></div></div>'
        + '<div class="dv-simcell show" id="cellOpt"><div class="map" id="simOpt"></div><span class="tag g" id="tagOpt"></span><div class="stat" id="statOpt"></div></div></div>'
        + '<p class="dv-muted" id="simNote"></p>';
      $("simPlay").onclick = function(){ if(S.simPlay){ stopSim(); return; } if(S.simT >= 30) S.simT = 0; S.simPlay = true; this.textContent = "\u275a\u275a PAUSE"; SIM.last = performance.now(); SIM.raf = requestAnimationFrame(tick); };
      $("simRange").oninput = function(){ stopSim(); S.simT = +this.value; frame(); };
      $("simJump").querySelectorAll("button").forEach(function(b){ b.onclick = function(){ stopSim(); S.simT = +b.dataset.t; frame(); }; });
      $("simWhich").querySelectorAll("button").forEach(function(b){ b.onclick = function(){ $("simWhich").querySelectorAll("button").forEach(function(x){ x.setAttribute("aria-pressed", String(x === b)); });
        $("cellNone").classList.toggle("show", b.dataset.w === "none"); $("cellOpt").classList.toggle("show", b.dataset.w === "opt"); SIM.maps[b.dataset.w].map.invalidateSize(); }; });
    }
    $("tagOpt").textContent = o ? (o.permitted === false ? "REFERENCE ONLY \u00b7 NOT PERMITTED (LTA RULE 1)" : "PROPOSED \u00b7 " + o.name) : "NO DIVERSION FOUND";
    var M = simMaps();
    [M.none, M.opt].forEach(function(x, i){
      x.g.clearLayers(); x.b.clearLayers();
      L.polyline(j.window.pts, {color:"#fff", weight:8, opacity:.85}).addTo(x.g); L.polyline(j.window.pts, {color:RC.route, weight:5}).addTo(x.g);
      (j.block_runs || [j.block_pts]).forEach(function(r){ L.polyline(r, {color:"#fff", weight:11, opacity:.9}).addTo(x.g); L.polyline(r, {color:RC.block, weight:7}).addTo(x.g); });
      if(i === 1 && o){ var okp = o.permitted !== false; L.polyline(o.anim.pts, {color:"#fff", weight:9, opacity:.9}).addTo(x.g); L.polyline(o.anim.pts, {color:okp ? RC.sel : RC.small, weight:6, dashArray:okp ? "12 7" : "2 8", className:okp ? "dv-sel" : ""}).addTo(x.g); }
      x.markers = {};
      var col = i === 0 ? colFor(0) : oc;
      (col ? col.sim : []).forEach(function(r){
        x.markers[r.label] = L.circleMarker(j.window.pts[0], {radius:7, weight:2, color:"#fff", fillOpacity:1, fillColor:C.cyan}).bindTooltip(r.label, {permanent:true, direction:"right", offset:[8, 0], className:""}).addTo(x.b);
      });
      x.map.invalidateSize(); x.map.fitBounds(L.latLngBounds(j.window.pts.concat(o && i === 1 ? o.anim.pts : [])).pad(.08));
    });
    $("simNote").textContent = "Bus movement from live positions and LTA speed bands (" + j.time_basis + "). " + (j.wait_allowed ? "Queued buses leave the block 30 s apart once it reopens." : "NO DIVERSION is shown for comparison only \u2014 buses are never held. " + (S.closure.min == null || S.closure.min > 120 ? "With a " + S.closure.label.toLowerCase() + " closure a bus that reaches the block cannot move for the rest of the simulation." : "Buses reaching the block would be stuck until it reopens.")) + " Estimate only.";
    frame();
  }
  function tick(t){
    var dt = (t - SIM.last) / 1000; SIM.last = t;
    S.simT = Math.min(30, S.simT + dt * 2);                     // 2 simulated minutes per second
    frame(); if(S.simT < 30 && S.simPlay) SIM.raf = requestAnimationFrame(tick); else stopSim();
  }
  var W = function(){ return S.opts.window; };
  function s2t(s){ return interp(W().s, W().t, s); }
  function t2s(t){ return interp(W().t, W().s, t); }
  function sPt(s){ var w = W(); return [interp(w.s, w.pts.map(function(p){ return p[0]; }), s), interp(w.s, w.pts.map(function(p){ return p[1]; }), s)]; }
  function animPt(o, dt){ var a = o.anim; return [interp(a.t, a.pts.map(function(p){ return p[0]; }), dt), interp(a.t, a.pts.map(function(p){ return p[1]; }), dt)]; }
  function busAt(r, T, o){
    var j = S.opts, b = j.buses.filter(function(x){ return x.label === r.label; })[0]; if(!b) return null;
    var t0 = s2t(b.s), A = j.block_a;
    if(r.mode === "diverted" && o){
      var te = r.t_exit == null ? 0 : r.t_exit;
      if(T < te) return {p:sPt(Math.min(t2s(t0 + T), o.leave_s)), st:"approach"};
      if(T < te + o.div_min) return {p:animPt(o, T - te), st:"div"};
      return {p:sPt(t2s(s2t(o.rejoin_s) + T - te - o.div_min)), st:"rejoined"};
    }
    if(r.mode === "queued" || r.mode === "stuck"){
      var q = S.opts.__q || {}, qi = q[r.label] || 0, tb = r.t_block || 0, rel = r.mode === "stuck" ? Infinity : tb + (r.wait || 0);
      if(T < rel) return {p:sPt(Math.min(t2s(t0 + T), A - 4 - qi * 16)), st:T >= tb ? "queue" : "approach"};
      return {p:sPt(t2s(s2t(A) + T - rel)), st:"released"};
    }
    return {p:sPt(t2s(t0 + T)), st:"normal"};
  }
  var ST_COL = {approach:C.cyan, div:C.green, rejoined:C.green, queue:C.red, released:C.amber, normal:C.grey};
  function frame(){
    if(!SIM.maps || !S.opts) return;
    var T = S.simT, o = optByN(S.optSel);
    $("simRange").value = T; $("simClock").textContent = clock(T) + " \u00b7 +" + f0(T) + " min";
    $("simJump").querySelectorAll("button").forEach(function(b){ b.setAttribute("aria-pressed", String(Math.abs(+b.dataset.t - T) < .05)); });
    [["none", colFor(0), null], ["opt", colFor(o ? o.n : 0), o]].forEach(function(x){
      var M = SIM.maps[x[0]], col = x[1]; if(!col) return;
      S.opts.__q = {}; col.sim.filter(function(r){ return r.mode === "queued" || r.mode === "stuck"; }).sort(function(a, b){ return (a.t_block || 0) - (b.t_block || 0); }).forEach(function(r, i){ S.opts.__q[r.label] = i; });
      var n = {queue:0, div:0, rejoined:0}, maxw = 0;
      col.sim.forEach(function(r){
        var m = M.markers[r.label], pos = busAt(r, T, x[2]); if(!m || !pos) return;
        m.setLatLng(pos.p); m.setStyle({fillColor:ST_COL[pos.st] || C.cyan});
        if(n[pos.st] != null) n[pos.st]++;
        if(pos.st === "queue") maxw = Math.max(maxw, T - (r.t_block || 0));
      });
      $(x[0] === "none" ? "statNone" : "statOpt").innerHTML = x[0] === "none" || !o
        ? (n.queue ? '<b style="color:#ffc3ca">' + n.queue + ' bus(es) ' + (waitOK() ? 'waiting at the block' : 'unable to move at the block') + '</b> \u00b7 ' + (waitOK() ? 'longest wait ' : 'stuck for ') + f0(maxw) + ' min so far' : 'No bus held at the block at this time')
        : '<b style="color:#c6f9e3">' + n.div + ' on diversion \u00b7 ' + n.rejoined + ' rejoined</b>' + (n.queue ? ' \u00b7 ' + n.queue + (waitOK() ? ' waiting' : ' unable to move') + ' (passed the diversion point)' : '');
    });
  }

  /* ------------------------------------------------------------------ WHAT HAPPENS NEXT */
  var TL_IC = {block:"\ud83d\udd34", exit:"\ud83d\ude8c", rejoin:"\ud83d\ude8c", queue:"\u23f8", bunch:"\u26a0", regulate:"\u21c4", stable:"\u2713", unstable:"\u26a0", reopen:"\ud83d\udea7"};
  function renderNext(){
    var el = $("paneNext"), col = S.opts ? colFor(S.optSel || 0) : null;
    if(!col){ el.innerHTML = DS.empty({icon:"clock", title:"NO TIMELINE YET", text:"The operational timeline follows the selected option once a service is analysed."}); return; }
    el.innerHTML = '<div class="ds-ph" style="margin-bottom:8px">' + esc(col.name) + ' \u00b7 SERVICE ' + esc(S.opts.service) + ' D' + S.opts.direction + '<span class="sp"></span><small>updates with the selected option</small></div><ol class="dv-tl">'
      + col.timeline.map(function(e){ return '<li class="k-' + e.kind + '"><span class="t">' + (e.t < .05 ? "NOW" : "+" + f0(e.t) + " MIN") + '</span><span class="dot"><i></i></span><span class="x">' + (TL_IC[e.kind] || "") + ' ' + esc(e.text) + '<small>' + clock(e.t) + '</small></span></li>'; }).join("") + '</ol>';
  }

  /* ------------------------------------------------------------------ POST-DIVERSION HEADWAY */
  function hwBar(sim, hw, H){
    var ts = sim.filter(function(r){ return r.t_ref != null; }), h = '<div class="dv-hwbar">';
    ts.forEach(function(r, i){
      h += '<span class="b ' + (r.mode === "diverted" ? "div" : r.mode === "queued" ? "q" : "") + '"><i></i>' + esc(r.label) + '</span>';
      var g = hw.gaps[i]; if(g && i < ts.length - 1){ var c = !H ? "" : g.gap < H * .5 ? "bad" : g.gap > H * 1.5 ? "bad" : g.gap < H * .75 || g.gap > H * 1.25 ? "warn" : "ok"; h += '<span class="g ' + c + '">' + f0(g.gap) + '</span>'; }
    });
    return h + '</div>';
  }
  function renderHeadway(){
    var el = $("paneHw"), j = S.opts, col = j ? colFor(S.optSel || 0) : null;
    $("hwBadge").hidden = !(col && col.hw.risk === "high");
    if(!col){ el.innerHTML = DS.empty({icon:"headway", title:"NO HEADWAY ANALYSIS YET", text:"A diversion does not end when buses rejoin. Once a service is analysed, the headway after the rejoin point appears here."}); return; }
    var H = j.H, hw = col.hw, reg = col.reg, rs = col.sim.filter(function(r){ return r.t_ref != null; });
    var cls = hw.risk === "high" ? "bad" : hw.risk === "moderate" ? "" : "ok";
    var head = hw.risk === "high" ? "\u26a0 POST-DIVERSION BUNCHING PREDICTED" : hw.risk === "moderate" ? "\u26a0 UNEVEN HEADWAY PREDICTED AFTER REJOINING" : hw.risk === "low" ? "\u2713 HEADWAY WITHIN TOLERANCE AFTER REJOINING" : "HEADWAY UNKNOWN (no scheduled headway)";
    var links = '/control?svc=' + encodeURIComponent(j.service) + '&dir=' + j.direction, bl = '/bunching?svc=' + encodeURIComponent(j.service) + '&dir=' + j.direction, hl = '/halfway?svc=' + encodeURIComponent(j.service) + '&dir=' + j.direction;
    el.innerHTML = '<div class="dv-hw"><div><div class="dv-alert ' + cls + '">' + head + '</div>'
      + '<table class="dv-tbl"><thead><tr><th>BUS</th><th>HOW</th><th>AT REJOIN POINT</th></tr></thead><tbody>'
      + rs.map(function(r){ return '<tr><td><b>' + esc(r.label) + '</b></td><td>' + esc({diverted:"via diversion", queued:"waits at block", normal:"past the block", ahead:"already past"}[r.mode] || r.mode) + '</td><td>' + (r.t_ref < 0 ? "passed " + f0(-r.t_ref) + " min ago" : clock(r.t_ref)) + '</td></tr>'; }).join("")
      + (col.sim.some(function(r){ return r.mode === "stuck"; }) ? '<tr><td colspan="3" class="dv-err">' + col.sim.filter(function(r){ return r.mode === "stuck"; }).length + ' bus(es) cannot pass until the road reopens (closure until further notice).</td></tr>' : '')
      + '</tbody></table>'
      + '<div class="dv-lbl">PREDICTED HEADWAY (MIN)</div><div style="font:600 17px var(--ds-display);color:#fff">' + hwChain(hw) + '</div>'
      + (H ? '<div class="dv-lbl">TARGET</div><div style="font:600 17px var(--ds-display);color:var(--ds-mut)">' + hw.gaps.slice(0, 6).map(function(){ return f0(H); }).join(" \u2014 ") + '</div>' : '')
      + hwBar(col.sim, hw, H) + '<p class="dv-muted">Scheduled headway: ' + esc(j.H_src || "unknown") + '.</p></div>'
      + '<div><div class="dv-lbl" style="margin-top:0">POSSIBLE RECOVERY ACTIONS' + (reg.target ? ' \u00b7 spacing ' + f0(reg.target) + ' min' : '') + '</div><div class="dv-hold">'
      + reg.holds.map(function(h){ return '<b>' + esc(h.label) + '</b><span style="color:' + (h.hold >= .5 ? C.amber : "var(--ds-text-2)") + '">' + esc(h.action) + (h.hold >= .5 && j.options.length && S.optSel ? ' at ' + esc((optByN(S.optSel).rejoin_stop || {}).name || "the first stop after rejoining") : '') + '</span>'; }).join("") + '</div>'
      + '<div class="dv-lbl">AFTER REGULATION</div><div style="font:600 15px var(--ds-display);color:#fff">' + hwChain(reg.after) + '</div>'
      + '<p class="dv-muted">' + (col.recovery_min == null ? "Holding alone does not restore the headway: consider an additional bus from the Halfway Planner." : "Service expected to stabilise about " + clock(col.recovery_min) + " (+" + f0(col.recovery_min) + " min).") + ' Spreads the buses to the average spacing available (or the scheduled headway if smaller); holds capped at 6 min. Suggestions only \u2014 nothing is executed.</p>'
      + '<div class="dv-row"><a class="ds-btn sm pri" href="' + links + '">Open Headway Control</a><a class="ds-btn sm" href="' + bl + '">Bunching & Gap</a>' + (col.recovery_min == null ? '<a class="ds-btn sm" href="' + hl + '">Halfway Planner</a>' : '') + '</div></div></div>';
  }

  /* ------------------------------------------------------------------ AFFECTED BUSES */
  function renderBuses(){
    var el = $("paneBuses"), j = S.opts;
    if(!j){ var e = S.an && S.sel ? entryBy(S.sel) : null;
      el.innerHTML = e && e.buses.length ? busTable(e.buses, e.service, false) : DS.empty({icon:"bunch", title:"NO BUSES LISTED", text:"Buses approaching the blockage appear once a service is analysed (LTA Bus Arrival)."}); return; }
    el.innerHTML = busTable(j.buses.concat(j.buses_on_diversion || []), j.service, true) + (j.arrival_error ? '<p class="dv-err">Bus Arrival: ' + esc(j.arrival_error) + '</p>' : '')
      + '<p class="dv-muted">' + j.stops_polled + ' stops polled. Bus IDs are positional (nearest the block = A); LTA Bus Arrival does not give registrations. Scheduled headway ' + (j.H ? f0(j.H) + ' min' : 'unknown') + '.</p>';
  }
  function busTable(list, svc, withExit){
    return '<div class="dv-scroll"><table class="dv-tbl"><thead><tr><th>BUS</th><th>STATUS</th><th>' + (withExit ? "TO DIVERSION POINT" : "TO BLOCKAGE") + '</th><th>BASIS</th><th>GAP AHEAD</th><th>LOAD</th></tr></thead><tbody>'
      + list.map(function(b){ var m = withExit ? b.min_to_exit : b.min_to_block;
        return '<tr><td><b>' + esc(b.label) + '</b></td><td>' + pill(b.status) + '</td><td>' + (m != null ? f1(m) + ' min' + (b.m_to_exit != null ? ' \u00b7 ' + km(b.m_to_exit) : '') : '\u2013') + '</td><td class="dv-muted">' + esc(b.eta_basis || "") + '</td><td>' + (b.gap_ahead_min != null ? f1(b.gap_ahead_min) + ' min' : '\u2013') + '</td><td>' + esc(b.load || "") + '</td></tr>'; }).join("")
      + '</tbody></table></div>';
  }

  /* ------------------------------------------------------------------ OPERATIONAL PLAN + lifecycle */
  function choicesList(){ return Object.keys(S.choices).map(function(k){ return S.choices[k]; }); }
  function summary(){
    var a = S.an; return {services:a ? a.services : choicesList().length, buses:a ? a.buses : null,
      chips:a ? a.entries.map(function(e){ return e.service + " D" + e.direction; }) : choicesList().map(function(c){ return c.service + " D" + c.direction; })};
  }
  function attachPlan(p){
    S.plan = p; S.shownRev = p.revision;
    try{ history.replaceState(null, "", "/diversion?id=" + p.id); }catch(e){}
  }
  function detachPlan(){ S.plan = null; S.updating = false; S.shownRev = null; try{ history.replaceState(null, "", "/diversion"); }catch(e){} }
  function needOcc(){ if(S.occ) return false; toast("Choose your OCC first."); var el = $("myOcc"); if(S.mobile) sheetTab("svc"); el.focus(); return true; }
  function savePlan(status){
    if(!hasBlock()) return Promise.resolve(null);
    return post("/api/diversion/plans", {blocks:blocksPayload(), status:status || "planned", occ:S.occ, closure_min:S.closure.min, closure_label:S.closure.label, services:choicesList(), summary:summary()}).then(function(j){
      if(j.error){ toast(j.error); return null; } attachPlan(j.plan); loadPlans(); renderAll();
      toast("Saved \u2014 OCC Live alert raised" + (j.saved ? "" : " (database unavailable: not persisted)")); return j.plan;
    });
  }
  function durTxt(p){ if(!p || !p.started) return ""; var m = Math.round(((p.ended || Date.now() / 1000) - p.started) / 60); return "Started " + DS.fmtTime(new Date(p.started * 1000)) + " \u00b7 " + m + " min"; }

  function renderPlan(){
    var el = $("planBody"), p = S.plan, ch = choicesList();
    if(!hasBlock() && !p){ el.innerHTML = DS.empty({icon:"engine", title:"NO PLAN", text:"Place a road block, choose an option per affected service, then confirm the diversion."}); $("planSub").textContent = ""; return; }
    var h = "";
    if(p && S.shownRev != null && p.revision > S.shownRev) h += '<div class="dv-rev">Updated to revision ' + p.revision + ' \u2014 ' + esc((p.log[p.log.length - 1] || {}).text || "") + ' <button type="button" class="ds-btn sm" id="pReload">Load latest</button></div>';
    if(p && ["active", "monitoring", "recovering", "ended"].indexOf(p.status) >= 0){
      h += '<div class="dv-active-h"><span class="dv-life" data-s="' + p.status + '"><i></i>' + p.status.toUpperCase() + '</span><b>' + esc(p.road) + ' diversion</b></div>'
        + '<div class="dv-meta">' + esc(durTxt(p)) + ' \u00b7 revision ' + p.revision + ' \u00b7 created by ' + esc(p.created_occ || p.created_by || "OCC") + (p.ticket_id ? ' \u00b7 <a href="/occ-live" style="color:var(--ds-cyan)">OCC ticket</a>' : '') + '</div>'
        + '<div class="dv-lbl" style="margin-top:0">ACKNOWLEDGED (REVISION ' + p.revision + ')</div><div class="dv-occ">'
        + p.acks.map(function(a){ return '<div class="' + (a.acked ? "ok " : "") + (a.involved ? "inv " : "") + (a.occ === S.occ ? "me" : "") + '" title="' + esc(a.acked ? "Acknowledged by " + a.who : a.involved ? "Shared \u2014 awaiting acknowledgement" : "Not shared") + '">' + (a.acked ? "\u2713" : "\u25cb") + ' ' + esc(a.occ) + (a.creator ? " \u2605" : "") + '</div>'; }).join("") + '</div>';
    }else if(p){
      h += '<div class="dv-meta">Saved as <b style="color:#fff">' + p.status.toUpperCase() + '</b> \u00b7 OCC Live alert ' + (p.alert_acked ? "acknowledged" : "raised") + '</div>';
    }
    if(S.updating) h += '<div class="dv-warn">Editing revision ' + ((p && p.revision) || 1) + ': change the blockage or options, then publish. Every OCC must acknowledge the new revision.</div>';
    h += '<div class="dv-lbl"' + (p ? '' : ' style="margin-top:0"') + '>SERVICES IN THE PLAN</div>';
    var badWait = ch.filter(function(c){ return !c.roads.length && !c.option && !waitOK(); });
    h += ch.length ? ch.map(function(c){ var bad = badWait.indexOf(c) >= 0; return '<div class="dv-plan-row"><b>' + esc(c.service) + '</b><span' + (bad ? ' style="color:#ffc3ca"' : '') + '>D' + c.direction + ' \u2014 ' + esc(c.roads.length ? c.roads.join(" \u2192 ") + " \u2192 rejoin" : c.action) + (bad ? ' \u2014 NOT POSSIBLE: closure ' + esc(S.closure.label) + ', buses cannot wait' : '') + (c.skipped.length ? ' \u00b7 ' + c.skipped.length + ' stops skipped' : '') + '</span>'
      + (lockedPlan() ? '' : '<button type="button" class="ds-btn sm ghost" data-rm="' + esc(c.service + "|" + c.direction + "|" + (c.run || 0)) + '" aria-label="Remove ' + esc(c.service) + ' from the plan">\u2715</button>') + '</div>'; }).join("")
      : '<p class="dv-muted">No option chosen yet. Press SELECT on an option' + (waitOK() ? ' (or on WAIT / REGULATE)' : '') + ' for each affected service.</p>';
    if(badWait.length) h += '<div class="dv-warn">Closure ' + esc(S.closure.label) + ': buses cannot wait. Choose a diversion for ' + badWait.map(function(c){ return esc(c.service + " D" + c.direction); }).join(", ") + ' before confirming.</div>';
    if(S.an){ var miss = S.an.entries.filter(function(e){ return !S.choices[key(e)]; }); if(miss.length && ch.length) h += '<p class="dv-muted">Not decided: ' + miss.map(function(e){ return esc(e.service + " D" + e.direction); }).join(", ") + '</p>'; }
    h += '<div class="dv-row" style="margin-top:10px">';
    if(FLT.test && (!p || ["detected", "planned"].indexOf(p.status) >= 0)){
      h += '<div class="dv-testbar" style="margin:0 0 6px"><b>TEST DATA</b> Plans built on synthetic buses cannot be saved, raised to OCC Live or confirmed. Switch Data to LIVE to act on this plan.</div>';
    }else if(!p || ["detected", "planned"].indexOf(p.status) >= 0){
      if(!p) h += '<button type="button" class="ds-btn sm" id="pSave"' + (hasBlock() ? "" : " disabled") + '>SAVE & RAISE OCC ALERT</button>';
      h += '<button type="button" class="ds-btn sm pri" id="pConfirm"' + (ch.length && !badWait.length ? "" : " disabled") + '>CONFIRM DIVERSION\u2026</button>';
    }else if(p.status === "active" || p.status === "monitoring"){
      if(S.updating) h += '<button type="button" class="ds-btn sm pri" id="pPublish"' + (badWait.length ? " disabled" : "") + '>PUBLISH REVISION</button><button type="button" class="ds-btn sm ghost" id="pCancelUp">CANCEL</button>';
      else h += '<button type="button" class="ds-btn sm pri" id="pAck">ACKNOWLEDGE' + (S.occ ? " (" + esc(S.occ) + ")" : "") + '</button><button type="button" class="ds-btn sm" id="pShare">SHARE</button><button type="button" class="ds-btn sm" id="pUpdate">UPDATE</button>'
        + '<button type="button" class="ds-btn sm" id="pNotice">NOTICE</button><button type="button" class="ds-btn sm" id="pMon">' + (p.status === "active" ? "SET MONITORING" : "SET ACTIVE") + '</button><button type="button" class="ds-btn sm" id="pEnd" style="border-color:rgba(255,85,102,.6)">END DIVERSION</button>';
    }else if(p.status === "recovering"){
      h += '<button type="button" class="ds-btn sm pri" id="pRecTab">RETURN-TO-NORMAL PLAN</button>';
    }else{
      h += '<button type="button" class="ds-btn sm" id="pNew">START A NEW BLOCKAGE</button><button type="button" class="ds-btn sm ghost" id="pNotice">NOTICE</button>';
    }
    if(hasBlock()) h += '<button type="button" class="ds-btn sm ghost" id="pRefresh">REFRESH LIVE DATA</button>';
    h += '</div><p class="dv-muted" style="margin-top:8px">Decision support: nothing is executed and nothing is sent outside the platform. The controller confirms the operational diversion.</p>';
    el.innerHTML = h; $("planSub").textContent = p ? "#" + p.id : (ch.length ? "not saved" : "");
    var on = function(id, f){ var b = $(id); if(b) b.onclick = f; };
    el.querySelectorAll("[data-rm]").forEach(function(b){ b.onclick = function(){ delete S.choices[b.dataset.rm]; renderAll(); }; });
    on("pSave", function(){ savePlan("planned"); });
    on("pConfirm", openConfirm);
    on("pAck", function(){ if(needOcc()) return; post("/api/diversion/plans/" + p.id + "/ack", {occ:S.occ}).then(function(j){ if(j.error) return toast(j.error); attachPlan(j.plan); renderAll(); toast("Acknowledged revision " + j.plan.revision + " for " + S.occ); }); });
    on("pShare", openShare);
    on("pUpdate", function(){ S.updating = true; S.editing = true; drawBlocks(); renderAll(); toast("Edit the blockage or options, then PUBLISH REVISION."); });
    on("pCancelUp", function(){ S.updating = false; openPlan(p.id); });
    on("pPublish", publishRevision);
    on("pNotice", function(){ noticeModal(p.notice || ""); });
    on("pMon", function(){ post("/api/diversion/plans/" + p.id + "/status", {status:p.status === "active" ? "monitoring" : "active"}).then(function(j){ if(j.error) return toast(j.error); attachPlan(j.plan); renderAll(); }); });
    on("pEnd", endDiversion);
    on("pRecTab", function(){ dockTab("rec"); openDock(true); if(S.mobile) sheetTab("plan"); });
    on("pNew", function(){ detachPlan(); S.blocks = []; S.choices = {}; resetAnalysis(); drawBlocks(); renderAll(); });
    on("pReload", function(){ openPlan(p.id); });
    on("pRefresh", function(){ var k = S.sel; S.pending = k ? {service:entryBy(k).service, direction:entryBy(k).direction, signature:(S.choices[k] || {}).sig} : null; analyse(); });
  }
  function lockedPlan(){ return S.plan && ["active", "monitoring", "recovering", "ended"].indexOf(S.plan.status) >= 0 && !S.updating; }

  function modal(html){ $("modalBox").innerHTML = html; $("modal").classList.add("on"); $("modalBox").querySelectorAll("[data-close]").forEach(function(b){ b.onclick = closeModal; }); var f = $("modalBox").querySelector("textarea,button.pri,button"); if(f) f.focus(); }
  function closeModal(){ $("modal").classList.remove("on"); }
  $("modal").addEventListener("click", function(e){ if(e.target === $("modal")) closeModal(); });
  document.addEventListener("keydown", function(e){ if(e.key === "Escape") closeModal(); });

  function occChecks(except, checked){
    return '<div class="dv-occ">' + S.teams.filter(function(t){ return t !== except; }).map(function(t){ return '<label style="display:flex;gap:6px;align-items:center;font-size:12.5px;color:var(--ds-text-2)"><input type="checkbox" value="' + esc(t) + '"' + (checked && checked.indexOf(t) >= 0 ? " checked disabled" : "") + '> ' + esc(t) + '</label>'; }).join("") + '</div>';
  }
  function openConfirm(){
    if(needOcc()) return;
    var ch = choicesList(); if(!ch.length) return toast("Choose an option for at least one service.");
    post("/api/diversion/notice", {road:roadsTxt(), services:ch, closure_label:S.closure.label, status:"active", by:S.occ}).then(function(j){
      modal('<h2 id="modalT">Confirm diversion \u2014 ' + esc(roadsTxt()) + '</h2><p class="dv-muted">Check and edit the operational message. Confirming records the diversion as ACTIVE, opens an OCC ticket (category Diversion) and shows it to the OCCs you share with. Nothing is sent outside the platform.</p>'
        + '<textarea id="mNotice" aria-label="Diversion notice">' + esc(j.text || "") + '</textarea><div class="dv-lbl">SHARE WITH OCCs</div>' + occChecks(S.occ)
        + '<div class="foot"><button type="button" class="ds-btn" data-close>Cancel</button><button type="button" class="ds-btn pri" id="mConfirm">Confirm &amp; activate</button></div>');
      $("mConfirm").onclick = function(){
        var share = Array.prototype.map.call($("modalBox").querySelectorAll("input[type=checkbox]:checked"), function(c){ return c.value; }), text = $("mNotice").value;
        var go = function(pl){ if(!pl) return;
          post("/api/diversion/plans/" + pl.id + "/confirm", {occ:S.occ, services:ch, share:share, notice:text, closure_label:S.closure.label, closure_min:S.closure.min, summary:summary(), blocks:blocksPayload()}).then(function(r){
            if(r.error) return toast(r.error); attachPlan(r.plan); S.editing = false; drawBlocks(); loadPlans(); renderAll(); noticeModal(r.plan.notice, true); }); };
        if(S.plan) go(S.plan); else savePlan("planned").then(go);
      };
    });
  }
  function noticeModal(text, justConfirmed){
    var p = S.plan;
    modal('<h2 id="modalT">' + (justConfirmed ? "Diversion ACTIVE \u2014 notice" : "Diversion notice") + '</h2><p class="dv-muted">' + (p ? "Revision " + p.revision + " \u00b7 " + esc(durTxt(p)) : "") + '</p><textarea id="mNotice" readonly aria-label="Diversion notice">' + esc(text) + '</textarea>'
      + '<div class="foot"><button type="button" class="ds-btn" id="mCopy">COPY</button><button type="button" class="ds-btn" id="mShare">SHARE</button><button type="button" class="ds-btn" id="mNotify">NOTIFY OCC</button>'
      + (p && (p.status === "active" || p.status === "monitoring") ? '<button type="button" class="ds-btn" id="mUpdate">UPDATE</button>' : '') + '<button type="button" class="ds-btn pri" data-close>Close</button></div>'
      + '<p class="dv-muted" style="margin-top:8px">COPY puts the text on your clipboard for any other channel you choose. SHARE and NOTIFY OCC stay inside the platform (OCC Live / OCC Notes & Actions).</p>');
    $("mCopy").onclick = function(){ var t = $("mNotice"); (navigator.clipboard ? navigator.clipboard.writeText(t.value) : Promise.reject()).then(function(){ toast("Notice copied"); }, function(){ t.select(); document.execCommand("copy"); toast("Notice copied"); }); };
    $("mShare").onclick = function(){ closeModal(); openShare(); };
    $("mNotify").onclick = function(){ if(!p) return; post("/api/diversion/plans/" + p.id + "/notify", {}).then(function(j){ toast(j.error || "Notice posted to the OCC ticket (OCC Live)"); }); };
    var u = $("mUpdate"); if(u) u.onclick = function(){ closeModal(); S.updating = true; S.editing = true; drawBlocks(); renderAll(); };
  }
  function openShare(){
    var p = S.plan; if(!p) return;
    modal('<h2 id="modalT">Share diversion</h2><p class="dv-muted">Selected OCCs see it in OCC Live and on this page, and are asked to acknowledge each revision.</p>' + occChecks(p.created_occ, p.shared)
      + '<div class="foot"><button type="button" class="ds-btn" data-close>Cancel</button><button type="button" class="ds-btn pri" id="mDoShare">Share</button></div>');
    $("mDoShare").onclick = function(){
      var occs = Array.prototype.map.call($("modalBox").querySelectorAll("input[type=checkbox]:checked:not(:disabled)"), function(c){ return c.value; });
      if(!occs.length) return toast("Choose at least one OCC.");
      post("/api/diversion/plans/" + p.id + "/share", {occs:occs}).then(function(j){ if(j.error) return toast(j.error); attachPlan(j.plan); closeModal(); renderAll(); toast("Shared with " + occs.join(", ")); });
    };
  }
  function publishRevision(){
    var p = S.plan, note = prompt("What changed? (shown to every OCC)", "") ; if(note === null) return;
    var ch = choicesList();
    post("/api/diversion/notice", {road:roadsTxt(), services:ch, closure_label:S.closure.label, status:p.status, revision:p.revision + 1, by:S.occ, effective:p.started ? DS.fmtTime(new Date(p.started * 1000)) : ""}).then(function(n){
      post("/api/diversion/plans/" + p.id + "/update", {services:ch, note:note, occ:S.occ, closure_label:S.closure.label, closure_min:S.closure.min, notice:n.text, blocks:blocksPayload()}).then(function(j){
        if(j.error) return toast(j.error); S.updating = false; S.editing = false; attachPlan(j.plan); drawBlocks(); renderAll(); noticeModal(j.plan.notice);
      });
    });
  }
  function endDiversion(){
    var p = S.plan; if(!confirm("Road reopened? This runs the RETURN-TO-NORMAL analysis. The diversion stays in force until you confirm the recovery plan.")) return;
    DS.loading("paneRec", ["Locating every bus of the diverted services\u2026", "Checking who is on the diversion\u2026", "Estimating when headway normalises\u2026"], "RETURN-TO-NORMAL ANALYSIS");
    dockTab("rec"); openDock(true); if(S.mobile) sheetTab("plan");
    post("/api/diversion/plans/" + p.id + "/end", {}, 90000).then(function(j){ if(j.error) return toast(j.error); attachPlan(j.plan); renderAll(); loadPlans(); });
  }
  function renderRecovery(){
    var el = $("paneRec"), p = S.plan;
    if(!p || !p.recovery){
      el.innerHTML = p && (p.status === "active" || p.status === "monitoring") ? '<p class="dv-muted">When the road reopens, press END DIVERSION. The diversion is not simply removed: every bus is checked first.</p><button type="button" class="ds-btn" id="rEnd">END DIVERSION</button>'
        : DS.empty({icon:"engine", title:"NO RECOVERY YET", text:"The return-to-normal plan appears here when an active diversion is ended."});
      var b = $("rEnd"); if(b) b.onclick = endDiversion; return;
    }
    var r = p.recovery, tot = r.services.reduce(function(a, s){ return a + (s.still_affected || 0); }, 0);
    el.innerHTML = '<div class="dv-alert ok">ROAD REOPENED \u00b7 ' + tot + ' BUS' + (tot === 1 ? "" : "ES") + ' STILL AFFECTED</div>'
      + r.services.map(function(s){ return '<div class="dv-lbl">SERVICE ' + esc(s.service) + ' D' + s.direction + '</div>' + (s.rows.length ? '<table class="dv-tbl"><tbody>' + s.rows.map(function(x){ return '<tr><td><b>' + esc(x.label) + '</b></td><td>' + esc(x.state) + '</td><td>\u2192 <b style="color:' + (x.action === "Complete diversion" ? C.green : x.action === "No change" ? "var(--ds-mut)" : C.cyan) + '">' + esc(x.action) + '</b>' + (x.eta != null ? ' (rejoins in ~' + f0(x.eta) + ' min)' : '') + '</td></tr>'; }).join("") + '</tbody></table>' : '<p class="dv-muted">' + esc(s.note || "No bus found near the section now.") + '</p>')
        + (s.normalise_min != null ? '<p class="dv-muted">Predicted normalisation: ' + f0(s.normalise_min) + ' min (' + esc(s.basis || "") + ')</p>' : ''); }).join("")
      + '<div class="dv-lbl">PREDICTED NORMALISATION</div><div style="font:600 22px var(--ds-display);color:#fff">' + f0(r.normalise_min) + ' min</div>'
      + (p.status === "recovering" ? '<div class="dv-row" style="margin-top:10px"><button type="button" class="ds-btn pri" id="rConfirm">CONFIRM RECOVERY PLAN</button><a class="ds-btn" href="/control">Headway Control</a></div>' : '<p class="dv-muted">Diversion ended ' + (p.ended ? DS.fmtTime(new Date(p.ended * 1000)) : "") + '.</p>');
    var c = $("rConfirm"); if(c) c.onclick = function(){ post("/api/diversion/plans/" + p.id + "/recovery", {close:true}).then(function(j){ if(j.error) return toast(j.error); attachPlan(j.plan); S.editing = false; renderAll(); loadPlans(); toast("Diversion ended \u2014 incident closed"); }); };
  }

  /* open plans (shared between controllers / OCCs) */
  function loadPlans(){
    return DS.api("/api/diversion/plans").then(function(j){
      if(j.teams && !S.teams.length){ S.teams = j.teams; fillOcc(); }
      var el = $("planList"), list = j.plans || [];
      $("plansSub").textContent = list.length ? list.length + " open" : "";
      el.innerHTML = list.length ? list.map(function(p){ var mine = p.acks.filter(function(a){ return a.occ === S.occ; })[0];
        return '<a href="/diversion?id=' + p.id + '" data-id="' + p.id + '" class="' + (S.plan && S.plan.id === p.id ? "cur" : "") + '"><span class="dv-life" data-s="' + p.status + '" style="height:22px;padding:0 8px;font-size:10px"><i></i>' + p.status.toUpperCase() + '</span><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(p.road) + '</span>'
          + (mine && mine.involved && !mine.acked ? '<span class="ds-sev warn">ACK</span>' : '') + '</a>'; }).join("") : '<p class="dv-muted">No open diversions.</p>';
      el.querySelectorAll("a[data-id]").forEach(function(a){ a.onclick = function(e){ e.preventDefault(); openPlan(+a.dataset.id); }; });
      if(S.plan){ var cur = list.filter(function(p){ return p.id === S.plan.id; })[0]; if(cur && (cur.revision !== S.plan.revision || JSON.stringify(cur.acks) !== JSON.stringify(S.plan.acks) || cur.status !== S.plan.status)){ var keep = S.shownRev; S.plan = cur; S.shownRev = keep; renderPlan(); setLife(); } }
    });
  }
  function openPlan(id){
    return DS.api("/api/diversion/plans/" + id).then(function(j){
      if(j.error) return toast(j.error);
      var p = j.plan; attachPlan(p); S.updating = false; S.editing = false; S.choices = {};
      (p.services || []).forEach(function(s){ S.choices[s.service + "|" + s.direction + "|" + (s.run || 0)] = Object.assign({sig:s.signature || "wait"}, s); });
      if(p.closure_label) setClosureLabel(p.closure_label);
      var first = (p.services || [])[0]; S.pending = first ? {service:first.service, direction:first.direction, signature:first.signature || "wait"} : null;
      if(savedBlocks(p.block).length) loadBlocks(savedBlocks(p.block)).then(function(){ resetAnalysis(); analyse(); });
      renderAll(); loadPlans();
    });
  }

  /* ------------------------------------------------------------------ status strip */
  var LIFE_TXT = {none:"NO BLOCKAGE", detected:"DETECTED", analysing:"ANALYSING", planned:"PLANNED", active:"ACTIVE", monitoring:"MONITORING", recovering:"RECOVERING", ended:"ENDED"};
  function lifeState(){
    if(S.plan && !S.updating) return S.plan.status;
    if(S.busy.an || S.busy.opt || S.busy.net) return "analysing";
    if(S.plan) return S.plan.status;
    if(choicesList().length) return "planned";
    return hasBlock() ? "detected" : "none";
  }
  function setLife(){
    var s = lifeState(), el = $("dvLife"); el.dataset.s = s; $("dvLifeT").textContent = LIFE_TXT[s] || s.toUpperCase();
    $("dvLifeS").textContent = S.plan && S.plan.started ? durTxt(S.plan) : (hasBlock() ? roadsTxt() : "");
    var st = S.plan ? S.plan.status : "", conf = ["active", "monitoring", "recovering", "ended"].indexOf(st) >= 0;
    var done = {block:!!hasBlock() || conf, analyse:!!S.an || conf, plan:choicesList().length > 0 || conf, simulate:S.simSeen || conf, confirm:conf,
      monitor:["recovering", "ended"].indexOf(st) >= 0, recover:st === "ended"};
    var order = ["block", "analyse", "plan", "simulate", "confirm", "monitor", "recover"], now = order.filter(function(k){ return !done[k]; })[0];
    if(st === "active" || st === "monitoring") now = "monitor"; if(st === "recovering") now = "recover";
    document.querySelectorAll("#dvFlow li").forEach(function(li){ var k = li.dataset.k; li.className = done[k] && k !== now ? "done" : k === now ? "now" : ""; });
    var sum = $("sheetSum"); if(sum) sum.innerHTML = hasBlock() ? '<b>' + esc(roadsTxt()) + '</b><span>' + (S.an ? S.an.services + " services \u00b7 " + S.an.buses + " buses" : "analysing\u2026") + '</span>' + (S.net ? '<span>\u00b7 ' + S.net.totals.divert + ' to divert</span>' : S.opts ? '<span>\u00b7 ' + esc(S.opts.service) + ' D' + S.opts.direction + '</span>' : '') : '<b>Add a road blockage</b><span>tap \u2715 then the road</span>';
  }

  /* ------------------------------------------------------------------ bottom dock (desktop / tablet) */
  var DOCK = {sim:"paneSim", next:"paneNext", hw:"paneHw", buses:"paneBuses", rec:"paneRec"};
  function dockTab(t){ document.querySelectorAll("#dockTabs .dv-tab").forEach(function(b){ b.setAttribute("aria-selected", String(b.dataset.p === t)); });
    Object.keys(DOCK).forEach(function(k){ $(DOCK[k]).classList.toggle("on", k === t); }); if(t === "sim"){ S.simSeen = !!S.opts || S.simSeen; setTimeout(renderSim, 280); setLife(); } }
  function openDock(on){ $("dock").classList.toggle("open", on); $("dockToggle").textContent = on ? "Collapse" : "Expand"; $("dockToggle").setAttribute("aria-expanded", String(on)); setTimeout(function(){ map.invalidateSize(); if(on && $("paneSim").classList.contains("on")) renderSim(); }, 260); }
  document.querySelectorAll("#dockTabs .dv-tab").forEach(function(b){ b.onclick = function(){ dockTab(b.dataset.p); openDock(true); }; });
  $("dockToggle").onclick = function(){ openDock(!$("dock").classList.contains("open")); };
  function renderDock(){ renderSim(); renderNext(); renderHeadway(); renderBuses(); renderRecovery(); }

  /* ------------------------------------------------------------------ phone: bottom sheet holds the same panels */
  var HOMES = [], SHEET = {svc:["dvFilters", "secBlock", "secServices", "secPlans", "secLayers"], all:["secNet"], opt:["secOptions"], sim:["paneSim"], imp:["secImpact", "paneHw"], tl:["paneNext"], plan:["secPlan", "paneBuses", "paneRec"]};
  function layout(){
    var m = window.matchMedia("(max-width:699px)").matches; if(m === S.mobile) return; S.mobile = m;
    var sb = $("sheetBody");
    if(m){
      Object.keys(SHEET).forEach(function(t){ var pane = document.createElement("div"); pane.className = "dv-pane" + (t === "svc" ? " on" : ""); pane.id = "sp-" + t; sb.appendChild(pane);
        SHEET[t].forEach(function(id){ var n = $(id); HOMES.push([n, n.parentNode, n.nextSibling]); pane.appendChild(n); if(n.classList.contains("dv-pane")) n.classList.add("on"); }); });
      makeChip($("fabHome"), true); $("chipHome").innerHTML = ""; $("mapChipHome").innerHTML = ""; chips = chips.filter(function(c){ return document.body.contains(c); });
      sheetTo("peek");
    }else{
      HOMES.reverse().forEach(function(h){ h[1].insertBefore(h[0], h[2]); }); HOMES = []; sb.innerHTML = "";
      Object.keys(DOCK).forEach(function(k){ $(DOCK[k]).classList.toggle("on", document.querySelector('#dockTabs [data-p="' + k + '"]').getAttribute("aria-selected") === "true"); });
      $("fabHome").innerHTML = ""; chips = []; makeChip($("chipHome"), false); makeChip($("mapChipHome"), true);
    }
    setTimeout(function(){ map.invalidateSize(); }, 80);
  }
  var SHEET_H = {peek:112, half:Math.round(window.innerHeight * .5), full:9999};
  function sheetTo(state){
    S.sheet = state; var h = state === "full" ? Math.round(window.innerHeight - 140) : state === "half" ? Math.round(window.innerHeight * .5) : SHEET_H.peek;
    document.documentElement.style.setProperty("--sheet", h + "px"); $("grab").setAttribute("aria-expanded", String(state !== "peek"));
  }
  function sheetTab(t){ document.querySelectorAll("#sheetTabs .dv-tab").forEach(function(b){ b.setAttribute("aria-selected", String(b.dataset.t === t)); });
    document.querySelectorAll("#sheetBody > .dv-pane").forEach(function(p){ p.classList.toggle("on", p.id === "sp-" + t); });
    if(S.sheet === "peek") sheetTo("half"); if(t === "sim"){ S.simSeen = !!S.opts; setTimeout(renderSim, 280); setLife(); } }
  document.querySelectorAll("#sheetTabs .dv-tab").forEach(function(b){ b.onclick = function(){ sheetTab(b.dataset.t); }; });
  (function(){
    var g = $("grab"), y0 = null, h0 = 0, moved = false;
    g.addEventListener("pointerdown", function(e){ y0 = e.clientY; h0 = $("sheet").getBoundingClientRect().height; moved = false; try{ g.setPointerCapture(e.pointerId); }catch(x){} $("sheet").style.transition = "none"; });
    g.addEventListener("pointermove", function(e){ if(y0 == null) return; var d = y0 - e.clientY; if(Math.abs(d) > 6) moved = true; if(moved) document.documentElement.style.setProperty("--sheet", Math.max(90, Math.min(window.innerHeight - 140, h0 + d)) + "px"); });
    g.addEventListener("pointerup", function(e){ $("sheet").style.transition = ""; if(y0 == null) return; var d = y0 - e.clientY; y0 = null;
      if(!moved){ sheetTo(S.sheet === "peek" ? "half" : S.sheet === "half" ? "full" : "peek"); return; }
      var h = h0 + d; sheetTo(h > window.innerHeight * .68 ? "full" : h > 200 ? "half" : "peek"); setTimeout(function(){ map.invalidateSize(); }, 260); });
  })();
  $("mLayBtn").onclick = function(){ sheetTab("svc"); sheetTo("full"); setTimeout(function(){ $("secLayers").scrollIntoView({behavior:"smooth", block:"start"}); }, 250); };

  /* ------------------------------------------------------------------ inputs */
  /* buses can only be held for a short, known closure; until further notice / whole day / long = divert */
  function waitOK(){ return S.closure.min != null && S.closure.min <= (S.waitMax || 30); }
  function wholeDayMin(){             // minutes to the end of today's service (about 01:00)
    var n = new Date(), end = new Date(n); end.setHours(n.getHours() < 1 ? 1 : 25, 0, 0, 0);
    return Math.max(120, Math.round((end - n) / 60000));
  }
  function closureFor(m, label){ return m === "open" ? {min:null, label:label} : m === "day" ? {min:wholeDayMin(), label:label} : {min:+m, label:label}; }
  function dropWaits(){
    if(waitOK()) return;
    var gone = Object.keys(S.choices).filter(function(k){ var c = S.choices[k]; return !(c.roads && c.roads.length) && !c.option; });
    gone.forEach(function(k){ delete S.choices[k]; });
    if(gone.length) toast("Closure " + S.closure.label + ": buses cannot wait. WAIT / REGULATE removed for " + gone.map(function(k){ return k.split("|").slice(0, 2).join(" D"); }).join(", ") + " \u2014 choose a diversion.");
  }
  function setClosureLabel(label){
    var btn = Array.prototype.filter.call(document.querySelectorAll("#closure button"), function(b){ return b.textContent === label; })[0];
    document.querySelectorAll("#closure button").forEach(function(b){ b.setAttribute("aria-pressed", String(b === btn)); });
    if(btn && btn.dataset.m !== "custom"){ S.closure = closureFor(btn.dataset.m, label); $("closureCustom").hidden = true; }
    else{ var m = parseFloat(label); S.closure = {min:isNaN(m) ? null : m, label:label}; $("closureCustom").hidden = false; $("closureMin").value = isNaN(m) ? "" : m;
      document.querySelector('#closure [data-m="custom"]').setAttribute("aria-pressed", "true"); }
  }
  function rerunOptions(){
    S.net = null; G.net.clearLayers(); netSeq++;
    if(S.sel && S.an){ var e = entryBy(S.sel); S.pending = {service:e.service, direction:e.direction, signature:(S.choices[S.sel] || {}).sig}; S.netPending = true; selectService(S.sel); }
    else if(S.an) runNet();
    renderNet();
  }
  document.querySelectorAll("#closure button").forEach(function(b){ b.onclick = function(){
    document.querySelectorAll("#closure button").forEach(function(x){ x.setAttribute("aria-pressed", String(x === b)); });
    if(b.dataset.m === "custom"){ $("closureCustom").hidden = false; $("closureMin").focus(); return; }
    $("closureCustom").hidden = true; S.closure = closureFor(b.dataset.m, b.textContent); dropWaits(); rerunOptions(); }; });
  $("closureMin").onchange = function(){ var m = +this.value; if(!(m > 0)) return; S.closure = {min:m, label:m + " MIN"}; dropWaits(); rerunOptions(); };
  $("mainOnly").onchange = function(){ S.allowSmall = !this.checked; rerunOptions(); };
  $("busType").value = S.bus; $("busType").onchange = function(){ S.bus = this.value; LS.set("dv.bus", S.bus); rerunOptions(); };
  function fillOcc(){ var sel = $("myOcc"); sel.innerHTML = '<option value="">Choose\u2026</option>' + S.teams.map(function(t){ return '<option' + (t === S.occ ? " selected" : "") + '>' + esc(t) + '</option>'; }).join(""); }
  $("myOcc").onchange = function(){ S.occ = this.value; LS.set("dv.occ", S.occ); renderPlan(); loadPlans(); };

  /* important stops (OCC list) */
  function loadImp(){ DS.api("/api/diversion/important").then(function(j){ $("impList").innerHTML = (j.stops || []).length ? j.stops.map(function(s){ return '<div>' + esc(s.code) + ' ' + esc(s.name) + (s.reason ? ' \u2014 ' + esc(s.reason) : '') + ' <button type="button" class="ds-btn sm ghost" data-rm="' + esc(s.code) + '" aria-label="Remove ' + esc(s.code) + '">\u2715</button></div>'; }).join("") : "None added.";
    $("impList").querySelectorAll("[data-rm]").forEach(function(b){ b.onclick = function(){ post("/api/diversion/important", {code:b.dataset.rm, remove:true}).then(loadImp); }; }); }); }
  $("impAdd").onclick = function(){ post("/api/diversion/important", {code:$("impCode").value, reason:$("impWhy").value}).then(function(j){ if(j.error) return toast(j.error); $("impCode").value = ""; $("impWhy").value = ""; loadImp(); toast("Added. Re-run the analysis to include it."); }); };

  /* ------------------------------------------------------------------ why not other routes (every route tested, and why it was not used) */
  var SRC_TXT = {router:"the routing engine", ladder:"the stop ladder", normal:"router", avoid:"TomTom, avoiding the block", busvia:"via a nearby bus road", adopt:"another service's corridor", manual:"your drawn route"};
  function whyPanel(j){
    var rj = j.rejects || [], st = j.status || {};
    var stTxt = '<div class="st">Road router: <b>' + esc(st.router || "?") + '</b>' + (st.router === "TomTom" ? ' \u00b7 ' + f0(st.tomtom_calls_today) + ' of ' + f0(st.tomtom_cap) + ' calls today \u00b7 closed section avoided with ' + f0(st.avoid_areas) + ' area(s)' : (st.tomtom_key ? '' : ' \u00b7 no TomTom key set'))
      + (st.tomtom_error ? ' \u00b7 <span style="color:#ffc3ca">TomTom error: ' + esc(st.tomtom_error) + '</span>' : '')
      + '<br>Bus route lines loaded: <b>' + f0(st.bus_lines) + '</b>' + (st.bus_lines ? '' : ' (' + esc(st.bus_lines_info || "not built yet") + ' \u2014 bus-road check falls back to stops)')
      + ' \u00b7 bus-road waypoints tried: <b>' + f0(st.busroad_waypoints) + '</b>' + (st.manual_waypoints ? ' \u00b7 your route: <b>' + st.manual_waypoints + ' points</b>' : '') + '</div>';
    if(!rj.length) return '<details class="dv-why"><summary>How the routes were found</summary>' + stTxt + '</details>';
    var groups = {}; rj.forEach(function(x){ groups[x.why] = (groups[x.why] || 0) + 1; });
    return '<details class="dv-why"' + (j.options.length ? '' : ' open') + '><summary>Why not other routes? ' + rj.length + ' route(s) not used \u2014 ' + Object.keys(groups).map(function(k){ return groups[k] + ' ' + k; }).join("; ") + '</summary>' + stTxt
      + rj.slice(0, 25).map(function(x, i){ return '<button type="button" class="dv-rej" data-rej="' + i + '" aria-pressed="false"><b>' + esc(x.why.toUpperCase()) + '</b>' + esc((x.roads || []).map(tc).join(" \u2192 ") || "unnamed roads") + ' \u00b7 ' + f1(x.km) + ' km \u00b7 found by ' + esc(SRC_TXT[x.src] || x.src) + (x.detail ? '<br><span>' + esc(x.detail) + '</span>' : '') + '</button>'; }).join("")
      + '<p class="dv-muted">Tap one to see it on the map (dotted grey). If the right road is missing, use <b>DRAW ROUTE</b> on the map.</p></details>';
  }
  document.addEventListener("click", function(e){
    var b = e.target.closest && e.target.closest(".dv-rej"); if(!b || !S.opts) return;
    var x = (S.opts.rejects || [])[+b.dataset.rej]; if(!x) return;
    var on = b.getAttribute("aria-pressed") !== "true";
    document.querySelectorAll(".dv-rej").forEach(function(o){ o.setAttribute("aria-pressed", "false"); });
    G.rej.clearLayers();
    if(on){ b.setAttribute("aria-pressed", "true"); rline(x.line, "#8a97a8", {pane:"alts", weight:4, dashArray:"2 7", opacity:1, casing:.6}, G.rej, true); map.fitBounds(L.latLngBounds(x.line).pad(.2)); if(S.mobile) sheetTo("peek"); }
  });

  /* ------------------------------------------------------------------ DRAW ROUTE: the controller draws the diversion */
  var DRAW = {on:false, pts:[]};
  function drawRender(){
    G.draw.clearLayers();
    if(DRAW.pts.length > 1) rline(DRAW.pts, "#0a7ea8", {pane:"divsel", weight:5, dashArray:"6 6", opacity:1}, G.draw, false);
    DRAW.pts.forEach(function(p, i){ L.marker(p, {pane:"bus", icon:divIcon(String(i + 1), "dv-wp"), keyboard:false}).addTo(G.draw); });
    $("drawTxt").textContent = DRAW.pts.length ? DRAW.pts.length + " point" + (DRAW.pts.length === 1 ? "" : "s") + " \u2014 keep tapping along the roads, then CHECK THIS ROUTE." : "Tap along the roads the buses should take \u2014 start before the block, end after it.";
    $("drawGo").disabled = DRAW.pts.length < 1;
  }
  function drawMode(on){
    if(on && !hasBlock()){ toast("Place the road block first."); return; }
    if(on && !S.sel){ toast("Select an affected service first."); return; }
    DRAW.on = on; $("drawBar").hidden = !on; $("drawBtn").setAttribute("aria-pressed", String(on));
    $("legend").classList.toggle("hid", on || !lay.legend);
    map.getContainer().style.cursor = on ? "crosshair" : "";
    if(on){ DRAW.pts = (S.manual || []).map(function(p){ return [p[0], p[1]]; }); if(S.mobile) sheetTo("peek"); arm(false); }
    else if(!S.manual) G.draw.clearLayers();
    drawRender();
  }
  $("drawBtn").onclick = function(){ drawMode(!DRAW.on); };
  $("drawUndo").onclick = function(){ DRAW.pts.pop(); drawRender(); };
  $("drawCancel").onclick = function(){ DRAW.pts = []; S.manual = null; drawMode(false); G.draw.clearLayers(); };
  $("drawGo").onclick = function(){
    S.manual = DRAW.pts.slice(0, 20); drawMode(false); drawRender();
    toast("Checking your route against the LTA rules \u2014 it is also offered to every other affected service.");
    var k = S.sel; resetAnalysisKeep(); if(k) selectService(k); runNet();
  };
  map.on("click", function(e){ if(!DRAW.on) return; if(DRAW.pts.length >= 20) return toast("Up to 20 points."); DRAW.pts.push([e.latlng.lat, e.latlng.lng]); drawRender(); });
  function resetAnalysisKeep(){ S.opts = null; S.optSel = null; }

  /* ------------------------------------------------------------------ scope bar: operator, services, LIVE / TEST data */
  function saveFlt(){ LS.set("dv.flt", FLT); }
  function clearFlt(){ FLT.op = ""; FLT.svcs = []; saveFlt(); rerunAll(); }
  function rerunAll(){ if(hasBlock()){ var k = S.sel && entryBy(S.sel); S.pending = k ? {service:k.service, direction:k.direction, signature:(S.choices[S.sel] || {}).sig} : S.pending; resetAnalysis(); analyse(); } renderScope(); }
  function renderScope(){
    $("fOp").value = FLT.op || "";
    var box = $("fChips"), inp = $("fSvc");
    box.querySelectorAll(".dv-chip").forEach(function(c){ c.remove(); });
    FLT.svcs.forEach(function(v){ var c = document.createElement("span"); c.className = "dv-chip"; c.innerHTML = esc(v) + '<button type="button" aria-label="Remove service ' + esc(v) + '">\u00d7</button>';
      c.querySelector("button").onclick = function(){ FLT.svcs = FLT.svcs.filter(function(x){ return x !== v; }); saveFlt(); rerunAll(); }; box.insertBefore(c, inp); });
    inp.placeholder = FLT.svcs.length ? "Add another" : "All affected (add e.g. 165)";
    document.querySelectorAll("#fData button").forEach(function(b){ b.setAttribute("aria-pressed", String((b.dataset.m === "test") === !!FLT.test)); });
    $("testBar").hidden = !FLT.test;
    var src = $("fSrc"); src.classList.toggle("test", !!FLT.test);
    src.querySelector("span").textContent = FLT.test ? "TEST data (synthetic buses)" : "Live Data (LTA)";
    $("trafficBtn").setAttribute("aria-pressed", String(TRAFFIC)); $("trafficT").textContent = TRAFFIC ? "ON" : "OFF";
    $("trafficBtn").title = TRAFFIC ? "Live traffic is included on the map and in all time estimates. Press to exclude it." : "Live traffic is excluded: times use road-routing speeds. Press to include it.";
  }
  $("fOp").onchange = function(){ FLT.op = this.value; saveFlt(); rerunAll(); };
  $("fSvc").addEventListener("keydown", function(e){
    if(e.key === "Enter" || e.key === "," || e.key === " "){ e.preventDefault(); var v = this.value.trim().toUpperCase().replace(/[^0-9A-Z]/g, "");
      if(v && FLT.svcs.indexOf(v) < 0 && FLT.svcs.length < 20){ FLT.svcs.push(v); saveFlt(); this.value = ""; rerunAll(); } else this.value = ""; }
    else if(e.key === "Backspace" && !this.value && FLT.svcs.length){ FLT.svcs.pop(); saveFlt(); rerunAll(); }
  });
  $("fSvc").addEventListener("blur", function(){ var v = this.value.trim().toUpperCase().replace(/[^0-9A-Z]/g, ""); if(v && FLT.svcs.indexOf(v) < 0){ FLT.svcs.push(v); saveFlt(); this.value = ""; rerunAll(); } });
  document.querySelectorAll("#fData button").forEach(function(b){ b.onclick = function(){
    var t = b.dataset.m === "test"; if(t === !!FLT.test) return;
    if(!t || !S.plan || ["detected", "planned", "ended"].indexOf(S.plan.status) >= 0){ FLT.test = t; saveFlt(); rerunAll(); toast(t ? "TEST data: synthetic buses \u2014 plans cannot be saved or confirmed." : "LIVE data from LTA."); }
    else toast("An active diversion is open \u2014 TEST data is not available until it has ended.");
  }; });
  $("trafficBtn").onclick = function(){
    TRAFFIC = !TRAFFIC; LS.set("dv.traffic", TRAFFIC);
    lay.speed = TRAFFIC; var cb = document.querySelector('#layers input[data-l="speed"]'); if(cb) cb.checked = TRAFFIC;
    if(!TRAFFIC){ lay.tomtom = false; var tb = document.querySelector('#layers input[data-l="tomtom"]'); if(tb) tb.checked = false; }
    applyLayers(); if(TRAFFIC) loadSpeed();
    toast(TRAFFIC ? "Live traffic included \u2014 re-estimating times." : "Live traffic excluded \u2014 times now use road-routing speeds."); rerunAll();
  };

  /* bus types: which services run double-deckers (LTA Bus Arrival, OCC entries override) */
  function loadBT(){
    DS.api("/api/diversion/bustypes?q=" + encodeURIComponent($("btQ").value.trim())).then(function(j){
      var list = j.services || [];
      $("btList").innerHTML = list.length ? list.map(function(x){ var seen = Object.keys(x.seen || {}).map(function(k){ return k + " \u00d7" + x.seen[k]; }).join(", ");
        return '<div class="dv-btrow"><b style="color:#fff">' + esc(x.service) + '</b><span>' + (x.class === "DD" ? "Double-deck" : x.class === "SD" ? "Single-deck / articulated only" : "Not known yet") + ' <small>\u00b7 ' + esc(x.source) + (x.override ? (x.note ? ": " + esc(x.note) : "") : (seen ? ": seen " + esc(seen) : "")) + '</small></span>'
          + (x.override ? '<button type="button" class="ds-btn sm ghost" data-btrm="' + esc(x.service) + '" aria-label="Remove OCC entry for ' + esc(x.service) + '">\u2715</button>' : '<span></span>') + '</div>'; }).join("")
        : "Nothing recorded yet \u2014 types are learned as LTA Bus Arrival is polled.";
      $("btList").querySelectorAll("[data-btrm]").forEach(function(b){ b.onclick = function(){ post("/api/diversion/bustypes", {service:b.dataset.btrm, remove:true}).then(function(){ loadBT(); rerunOptions(); }); }; });
    });
  }
  $("btSave").onclick = function(){
    var t = $("btType").value;
    post("/api/diversion/bustypes", {service:$("btSvc").value, types:[t], note:$("btNote").value}).then(function(j){
      if(j.error) return toast(j.error); toast("Service " + j.service + " saved \u2014 re-checking diversions"); $("btSvc").value = ""; $("btNote").value = ""; loadBT(); rerunOptions(); });
  };
  var btT = null; $("btQ").oninput = function(){ clearTimeout(btT); btT = setTimeout(loadBT, 250); };
  $("btBox").addEventListener("toggle", function(){ if($("btBox").open) loadBT(); });

  /* future automatic incident mode: suggestions only */
  function loadDetect(){
    DS.api("/api/diversion/detect").then(function(j){
      var it = j.items || [], el = $("detectBox"); if(!it.length || hasBlock()){ el.innerHTML = ""; return; }
      el.innerHTML = '<div class="dv-det"><div class="k">POTENTIAL ROAD BLOCKAGE DETECTED (' + it.length + ')</div>' + it.slice(0, 3).map(function(x, i){ return '<div class="dv-det-it"><span><b style="color:#fff">' + esc(x.type) + '</b> \u2014 ' + esc(x.message.slice(0, 90)) + '<br><small class="dv-muted">Near services ' + esc(x.services_near.slice(0, 6).join(", ")) + '</small></span><button type="button" class="ds-btn sm" data-i="' + i + '">REVIEW</button></div>'; }).join("")
        + '<p class="dv-muted" style="margin:8px 0 0">From LTA Traffic Incidents. Nothing is diverted automatically: review, place the exact section, then confirm.</p></div>';
      el.querySelectorAll("[data-i]").forEach(function(b){ b.onclick = function(){ var x = it[+b.dataset.i]; map.setView([x.lat, x.lon], 17); drop(L.latLng(x.lat, x.lon)); }; });
    });
  }

  /* ------------------------------------------------------------------ render + boot */
  function renderAll(){ renderServices(); renderNet(); renderOptions(); renderCompare(); renderPlan(); renderDock(); renderPlaybook(); setLife(); }
  layout(); window.addEventListener("resize", function(){ layout(); });
  lay.speed = TRAFFIC; (function(){ var cb = document.querySelector('#layers input[data-l="speed"]'); if(cb) cb.checked = TRAFFIC; })();
  renderScope(); renderBlockCard(); renderAll(); applyLayers(); loadIncidents(); loadImp();
  DS.api("/api/occ/meta").then(function(j){ if(j.teams){ S.teams = j.teams; fillOcc(); } loadPlans(); });
  setInterval(loadPlans, 20000); setInterval(loadIncidents, 120000); setInterval(setLife, 30000);
  var q = new URLSearchParams(location.search);
  if(q.get("id")) openPlan(+q.get("id"));
  else if(q.get("lat") && q.get("lon")){ var la = +q.get("lat"), lo = +q.get("lon"); map.setView([la, lo], 17); drop(L.latLng(la, lo)); }
  else loadDetect();
})();
