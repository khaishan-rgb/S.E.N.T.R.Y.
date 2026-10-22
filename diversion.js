/* SG Transport Pulse - Diversion Planner (V16.16, rebuilt).
   Flow: 1 search  2 tap the blocked road segment  3 blocked direction  4 affected services  5 easiest practical diversion.
   Every road geometry comes from the routing engine via /api/dv2/*; this page only draws and explains it. */
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); }, esc = DS.esc;
  var COL = {orig:"#38d6ff", block:"#e0263f", skip:"#e0a020", div:"#12a865", alt:"#c96a00", served:"#2f8cff", rejoin:"#12a865", inblock:"#e0263f"};
  var KX = 111320 * Math.cos(1.35 * Math.PI / 180), KY = 110574;
  var S = {cfg:null, block:null, dir:null, services:[], plans:{}, sel:null, view:null, q:[], qi:-1, run:0};

  /* ---------------------------------------------------------------- helpers */
  function get(url, t){ return DS.api(url, {timeout:t || 30000}); }
  function post(url, body, t){ return DS.api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body || {}), timeout:t || 60000}); }
  function toast(t){ var el = $("toast"); el.textContent = t; el.classList.add("on"); clearTimeout(toast._t); toast._t = setTimeout(function(){ el.classList.remove("on"); }, 2800); }
  function tc(s){ s = String(s || ""); return s === s.toUpperCase() ? s.toLowerCase().replace(/\b([a-z])/g, function(m){ return m.toUpperCase(); }) : s; }
  function f0(x){ return x == null || isNaN(x) ? "\u2013" : String(Math.round(x)); }
  function f1(x){ return x == null || isNaN(x) ? "\u2013" : (Math.round(x * 10) / 10).toFixed(1); }
  function sgn(x, d){ if(x == null || isNaN(x)) return "\u2013"; var v = d ? (Math.round(x * 10) / 10).toFixed(1) : String(Math.round(x)); return (x > 0 ? "+" : "") + v; }
  function dm(a, b){ return Math.hypot((a[1] - b[1]) * KX, (a[0] - b[0]) * KY); }
  function cumM(l){ var c = [0]; for(var i = 1; i < l.length; i++) c.push(c[i - 1] + dm(l[i - 1], l[i])); return c; }
  function pointAt(l, c, s){
    if(s <= 0) return l[0]; if(s >= c[c.length - 1]) return l[l.length - 1];
    var lo = 0, hi = c.length - 1; while(hi - lo > 1){ var m = (lo + hi) >> 1; if(c[m] <= s) lo = m; else hi = m; }
    var f = c[hi] === c[lo] ? 0 : (s - c[lo]) / (c[hi] - c[lo]); return [l[lo][0] + (l[hi][0] - l[lo][0]) * f, l[lo][1] + (l[hi][1] - l[lo][1]) * f];
  }
  function cutLine(l, c, a, b){ var o = [pointAt(l, c, a)]; for(var i = 0; i < l.length; i++) if(c[i] > a && c[i] < b) o.push(l[i]); o.push(pointAt(l, c, b)); return o; }
  function nearestOn(p, l, c){
    var best = {d:1e18, s:0};
    for(var i = 0; i < l.length - 1; i++){
      var ax = l[i][1] * KX, ay = l[i][0] * KY, bx = l[i + 1][1] * KX, by = l[i + 1][0] * KY, px = p[1] * KX, py = p[0] * KY;
      var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy, t = L2 < 1e-9 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2));
      var d = Math.hypot(px - (ax + t * dx), py - (ay + t * dy)); if(d < best.d) best = {d:d, s:c[i] + t * (c[i + 1] - c[i])};
    }
    return best;
  }
  function brg(a, b){ return (Math.atan2((b[1] - a[1]) * KX, (b[0] - a[0]) * KY) * 180 / Math.PI + 360) % 360; }
  function km(m){ return m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m"; }

  /* ---------------------------------------------------------------- map */
  var map = L.map("map", {zoomControl:true, minZoom:10, maxZoom:20, maxBounds:[[1.10, 103.50], [1.55, 104.20]]}).setView([1.3521, 103.8198], 12);
  [["road", 400], ["orig", 410], ["skip", 415], ["alt", 420], ["div", 430], ["block", 440], ["arrows", 450], ["marks", 620]].forEach(function(p){ map.createPane(p[0]).style.zIndex = p[1]; });
  var G = {};
  ["road", "block", "orig", "skip", "alt", "div", "stops", "marks", "find", "draw"].forEach(function(k){ G[k] = L.layerGroup().addTo(map); });
  function icon(html, cls){ return L.divIcon({className:"", iconSize:[0, 0], html:'<div class="' + (cls || "") + '">' + html + '</div>'}); }
  function rline(pts, color, o, grp){
    o = o || {}; var w = o.weight || 6;
    L.polyline(pts, {pane:o.pane, color:"#fff", weight:w + 4, opacity:o.casing == null ? .85 : o.casing, interactive:false, lineJoin:"round"}).addTo(grp);
    return L.polyline(pts, {pane:o.pane, color:color, weight:w, opacity:o.opacity == null ? 1 : o.opacity, dashArray:o.dash, className:o.cls || "", lineJoin:"round"}).addTo(grp);
  }
  var ARR = [];
  function arrows(grp, pts, color, pane, everyPx){
    var ag = L.layerGroup().addTo(grp), spec = {grp:grp, ag:ag, pts:pts, color:color, pane:pane || "arrows", px:everyPx || 110};
    ARR.push(spec); drawArrows(spec); return ag;
  }
  function drawArrows(sp){
    sp.ag.clearLayers(); if(!sp.pts || sp.pts.length < 2) return;
    var c = cumM(sp.pts), L_ = c[c.length - 1], mpp = 40075016.686 * Math.cos(sp.pts[0][0] * Math.PI / 180) / (256 * Math.pow(2, map.getZoom()));
    var step = Math.max(40, sp.px * mpp); if(L_ < step * .5){ step = L_; }
    for(var d = Math.min(step / 2, L_ / 2); d < L_; d += step){
      var a = pointAt(sp.pts, c, Math.max(0, d - 4)), b = pointAt(sp.pts, c, Math.min(L_, d + 4));
      L.marker(pointAt(sp.pts, c, d), {pane:sp.pane, interactive:false, keyboard:false, icon:L.divIcon({className:"", iconSize:[0, 0],
        html:'<div class="dp-arrow" style="--c:' + sp.color + ';transform:translate(-50%,-50%) rotate(' + brg(a, b).toFixed(0) + 'deg)"></div>'})}).addTo(sp.ag);
    }
  }
  map.on("zoomend", function(){ ARR = ARR.filter(function(s){ return s.grp.hasLayer(s.ag); }); ARR.forEach(drawArrows); });

  get("/api/dv2/config").then(function(cfg){
    S.cfg = cfg || {};
    if(S.cfg.tomtom_map) L.tileLayer("/api/tomtom/map/{z}/{x}/{y}.png", {maxZoom:20, maxNativeZoom:20, attribution:"\u00a9 TomTom"}).addTo(map);
    else SGBasemap.add(map, {style:"dark", minZoom:10, maxZoom:19});
  });

  /* ---------------------------------------------------------------- 1. search */
  var qT = null;
  $("q").addEventListener("input", function(){ clearTimeout(qT); var v = this.value.trim(); qT = setTimeout(function(){ search(v); }, 250); });
  $("q").addEventListener("keydown", function(e){
    var n = S.q.length; if(!n) return;
    if(e.key === "ArrowDown"){ e.preventDefault(); S.qi = (S.qi + 1) % n; paintList(); }
    else if(e.key === "ArrowUp"){ e.preventDefault(); S.qi = (S.qi - 1 + n) % n; paintList(); }
    else if(e.key === "Enter"){ e.preventDefault(); pick(S.q[Math.max(0, S.qi)]); }
    else if(e.key === "Escape"){ closeList(); }
  });
  document.addEventListener("click", function(e){ if(!$("qBox").contains(e.target)) closeList(); });
  function search(v){
    if(v.length < 2){ closeList(); return; }
    get("/api/dv2/search?q=" + encodeURIComponent(v)).then(function(j){ if($("q").value.trim() !== v) return; S.q = j.results || []; S.qi = S.q.length ? 0 : -1; paintList(); });
  }
  function paintList(){
    var ul = $("qList");
    if(!S.q.length){ ul.innerHTML = '<li aria-disabled="true"><span class="ic">?</span><b>No match</b><small>Try a road name, bus stop code or place</small></li>'; }
    else ul.innerHTML = S.q.map(function(r, i){ var t = {road:"RD", stop:"BS", place:"PL"}[r.type] || "\u2022";
      return '<li role="option" id="qo' + i + '" data-i="' + i + '" aria-selected="' + (i === S.qi) + '"><span class="ic ' + r.type + '">' + t + '</span><b>' + esc(r.label) + '</b><small>' + esc(r.sub || "") + '</small></li>'; }).join("");
    ul.hidden = false; $("qBox").setAttribute("aria-expanded", "true");
    if(S.qi >= 0) $("q").setAttribute("aria-activedescendant", "qo" + S.qi);
    ul.querySelectorAll("li[data-i]").forEach(function(li){ li.onclick = function(){ pick(S.q[+li.dataset.i]); }; });
  }
  function closeList(){ $("qList").hidden = true; $("qBox").setAttribute("aria-expanded", "false"); }
  /* ---------------------------------------------------------------- 1. search: highlight one or more roads (see the junctions) */
  var HLC = ["#38d6ff", "#ffb547", "#c792ff", "#7bdff2", "#ff8fab", "#b8f35a"];
  S.hl = [];
  function hlAdd(name, label){
    if(S.hl.some(function(h){ return h.name === name; })) return;
    var h = {name:name, label:label || tc(name), color:HLC[S.hl.length % HLC.length], grp:L.layerGroup().addTo(G.road)};
    S.hl.push(h); paintHl();
    get("/api/dv2/roadgeom?name=" + encodeURIComponent(name)).then(function(j){
      var segs = j.segments || []; if(!segs.length || S.hl.indexOf(h) < 0) return;
      segs.forEach(function(sg){ L.polyline([[sg[0], sg[1]], [sg[2], sg[3]]], {pane:"road", color:h.color, weight:7, opacity:.55, interactive:false}).addTo(h.grp); });
      var md = segs[Math.floor(segs.length / 2)];
      L.marker([(md[0] + md[2]) / 2, (md[1] + md[3]) / 2], {pane:"marks", interactive:false, keyboard:false,
        icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dp-rlbl" style="--c:' + h.color + '">' + esc(h.label) + '</div>'})}).addTo(h.grp);
    });
  }
  function hlRemove(i){ var h = S.hl.splice(i, 1)[0]; if(h) G.road.removeLayer(h.grp); paintHl(); }
  function paintHl(){
    $("hlBox").hidden = !S.hl.length;
    $("hlList").innerHTML = S.hl.map(function(h, i){ return '<span class="dp-chip" style="--c:' + h.color + '"><i></i>' + esc(h.label) + '<button type="button" data-i="' + i + '" aria-label="Remove highlight ' + esc(h.label) + '">\u00d7</button></span>'; }).join("")
      + (S.hl.length > 1 ? '<button type="button" class="ds-btn sm ghost" id="hlClear">CLEAR</button>' : '');
    $("hlList").querySelectorAll("[data-i]").forEach(function(b){ b.onclick = function(){ hlRemove(+b.dataset.i); }; });
    var c = $("hlClear"); if(c) c.onclick = function(){ while(S.hl.length) hlRemove(0); };
  }
  function pick(r){
    if(!r) return; closeList(); $("q").value = ""; G.find.clearLayers();
    if(r.type === "road"){
      hlAdd(r.road, r.label);
      if(r.bbox) map.fitBounds([[r.bbox[0], r.bbox[1]], [r.bbox[2], r.bbox[3]]], {padding:[30, 30], maxZoom:17});
      hint(S.hl.length > 1 ? "Roads highlighted in different colours \u2014 they meet where the colours cross. Tap the blocked road." : "Tap the blocked part of " + r.label + " on the map");
    }else if(r.type === "junction"){
      (r.roads || []).forEach(function(n){ hlAdd(n); });
      map.setView([r.lat, r.lon], r.found ? 18 : 16);
      if(r.found) L.marker([r.lat, r.lon], {pane:"marks", icon:icon("\u2716", "dp-jx")}).bindTooltip("Junction " + esc(r.label)).addTo(G.find);
      hint(r.found ? "Junction " + r.label + " \u2014 tap the blocked road" : r.label + ": these roads do not meet");
    }else{
      map.setView([r.lat, r.lon], r.type === "stop" ? 18 : 17);
      L.marker([r.lat, r.lon], {pane:"marks", icon:icon(r.type === "stop" ? "BS" : "\u2022", "dp-lbl")}).addTo(G.find);
      hint("Tap the blocked road near " + r.label);
    }
  }
  function hint(t){ $("mapHint").textContent = t; $("mapHint").hidden = !t; }

  /* ---------------------------------------------------------------- 2. blockages: one or more road SEGMENTS with adjustable ends */
  S.blocks = []; S.bi = -1; S.adding = false;
  function AB(){ return S.blocks[S.bi] || null; }
  function bLineOf(b){ return cutLine(b.cor, b.cm, b.a, b.b); }
  function allDirs(){ return S.blocks.length > 0 && S.blocks.every(function(b){ return !!b.dir; }); }
  map.on("click", function(e){ if(S.draw && S.draw.on){ drawAdd(e.latlng); return; } if(!BIN.drag) placeBlock(e.latlng); });
  function placeBlock(ll){
    var replace = S.blocks.length > 0 && !S.adding;
    if(replace && AB() && AB().locked){
      hint("Blockage locked \u2014 press UNLOCK to move it, or + ADD ANOTHER BLOCKAGE");
      toast("Blockage locked \u2014 the tap was ignored"); return;
    }
    hint("Finding the road\u2026");
    get("/api/dv2/block?lat=" + ll.lat.toFixed(6) + "&lon=" + ll.lng.toFixed(6)).then(function(j){
      if(!j.ok){ hint(""); toast(j.error || "No road there \u2014 tap directly on the road."); return; }
      var nb = {cor:j.corridor, cm:j.corridor_m, a:j.start_m, b:j.end_m, road:tc(j.road), labels:j.dir_labels || {}, dir:null, locked:false};
      if(replace) S.blocks[S.bi] = nb; else { S.blocks.push(nb); S.bi = S.blocks.length - 1; }
      S.adding = false; $("bAdd").setAttribute("aria-pressed", "false");
      resetServices(); G.find.clearLayers();
      paintBlockCard(); drawBlocks(true); hint("Choose the blocked direction" + (S.blocks.length > 1 ? " for blockage " + (S.bi + 1) : "") + " (step 3)");
    });
  }
  function selectBlock(i){ S.bi = i; paintBlockCard(); drawBlocks(false); }
  function removeBlock(i){
    if(i < 0 || i >= S.blocks.length) return;
    S.blocks.splice(i, 1);
    S.bi = S.blocks.length ? Math.min(Math.max(S.bi, 0), S.blocks.length - 1) : -1;
    resetServices(); paintBlockCard(); drawBlocks(false);
    if(allDirs()) runAffected(); else hint(S.blocks.length ? "Choose the blocked direction" : "Tap the road where it is blocked");
  }
  function drawBlocks(fit){
    G.block.clearLayers(); ARR = ARR.filter(function(sp){ return !(sp.grp && sp.grp._dpBlock); });
    S.blocks.forEach(function(b, i){
      var act = i === S.bi, bl = bLineOf(b), g = L.layerGroup().addTo(G.block); g._dpBlock = true;
      b._case = L.polyline(bl, {pane:"block", color:"#fff", weight:act ? 13 : 11, opacity:.9, interactive:false}).addTo(g);
      b._line = L.polyline(bl, {pane:"block", color:COL.block, weight:act ? 9 : 7, opacity:act ? 1 : .85}).addTo(g)
        .bindTooltip((S.blocks.length > 1 ? (i + 1) + ". " : "") + esc(b.road) + " \u2014 blocked", {sticky:true}).on("click", function(ev){ L.DomEvent.stopPropagation(ev); selectBlock(i); });
      if(b.dir === "fwd" || b.dir === "both") arrows(g, bl, "#fff", "arrows", 60);
      if(b.dir === "rev" || b.dir === "both") arrows(g, bl.slice().reverse(), "#fff", "arrows", b.dir === "both" ? 85 : 60);
      var mid = pointAt(b.cor, b.cm, (b.a + b.b) / 2);
      b._x = L.marker(mid, {pane:"marks", draggable:!b.locked, autoPan:false, keyboard:false, title:b.locked ? "Locked \u00b7 tap to select (UNLOCK to move or delete)" : "Tap to select \u00b7 hold and drag to the bin to delete",
        icon:icon(S.blocks.length > 1 ? String(i + 1) : "\u2715", "dp-x" + (act ? "" : " off"))}).addTo(g);
      b._x.on("click", function(){ selectBlock(i); });
      if(!b.locked) binDrag(b._x, function(){ removeBlock(S.blocks.indexOf(b)); }, function(m){ m.setLatLng(mid); toast("Drag the \u2715 onto the bin to delete \u00b7 drag the white handles to resize"); });
      if(act && !b.locked){ handle(b, g, "a"); handle(b, g, "b"); }
    });
    if(fit && AB()) map.fitBounds(L.latLngBounds(bLineOf(AB())).pad(1.2), {maxZoom:18});
  }
  function handle(b, g, end){
    var m = L.marker(pointAt(b.cor, b.cm, b[end]), {draggable:true, pane:"marks", autoPan:true, icon:icon("", "dp-handle"), keyboard:true,
      title:"Drag along the road to set this end \u00b7 drop on the bin to delete the blockage"}).addTo(g);
    m.on("drag", function(e){
      var p = e.target.getLatLng(), n = nearestOn([p.lat, p.lng], b.cor, b.cm);
      if(end === "a") b.a = Math.min(n.s, b.b - 30); else b.b = Math.max(n.s, b.a + 30);
      var bl = bLineOf(b); b._line.setLatLngs(bl); b._case.setLatLngs(bl); b._x.setLatLng(pointAt(b.cor, b.cm, (b.a + b.b) / 2)); paintLen();
    });
    binDrag(m, function(){ removeBlock(S.blocks.indexOf(b)); }, function(){ drawBlocks(false); paintBlockCard(); if(allDirs()) runAffected(); });
    return m;
  }
  /* the bin: shown while a blockage's X or handle is held and dragged; dropping on it deletes that blockage */
  var BIN = {drag:false, hot:false, xy:null};
  document.addEventListener("pointermove", function(e){ BIN.xy = [e.clientX, e.clientY]; if(BIN.drag) binHot(); }, {passive:true});
  document.addEventListener("touchmove", function(e){ if(e.touches[0]){ BIN.xy = [e.touches[0].clientX, e.touches[0].clientY]; if(BIN.drag) binHot(); } }, {passive:true});
  function binHot(){
    var el = $("dpBin"), r = el.getBoundingClientRect(), x = BIN.xy;
    BIN.hot = !!x && r.width > 0 && x[0] >= r.left - 24 && x[0] <= r.right + 24 && x[1] >= r.top - 24 && x[1] <= r.bottom + 24;
    el.classList.toggle("hot", BIN.hot); return BIN.hot;
  }
  function binDrag(m, onDelete, onEnd){
    m.on("dragstart", function(){ BIN.drag = true; BIN.hot = false; $("dpBin").classList.add("on"); });
    m.on("drag", function(e){ var o = e.originalEvent; if(o){ var t = (o.touches && o.touches[0]) || o; if(t && t.clientX != null) BIN.xy = [t.clientX, t.clientY]; } binHot(); });
    m.on("dragend", function(){
      var hot = binHot(); $("dpBin").classList.remove("on", "hot"); setTimeout(function(){ BIN.drag = false; }, 80);
      if(hot){ onDelete(); toast("Blockage deleted"); } else onEnd(m);
    });
  }
  function paintLen(){ var b = AB(); $("bLen").textContent = b ? km(b.b - b.a) + " blocked \u00b7 " + (b.locked ? "\ud83d\udd12 locked" : "drag the handles to adjust") : ""; }
  function dirTxt(b){ return !b.dir ? '<span style="color:#ffc56b">direction?</span>' : b.dir === "both" ? "both directions" : esc(String(b.labels[b.dir] || b.dir).split(" \u00b7 ")[0]); }
  function paintBlockCard(){
    var b = AB();
    $("cBlock").hidden = !S.blocks.length; if(!b) return;
    $("bCount").textContent = S.blocks.length > 1 ? S.blocks.length + " blockages" : "";
    $("bList").innerHTML = S.blocks.length > 1 ? S.blocks.map(function(x, i){ return '<button type="button" class="dp-bchip" role="listitem" data-i="' + i + '" aria-pressed="' + (i === S.bi) + '"><b>' + (i + 1) + '</b>' + esc(x.road) + (x.locked ? '<span class="lk" aria-label="locked">\ud83d\udd12</span>' : '') + '<small>' + km(x.b - x.a) + ' \u00b7 ' + dirTxt(x) + '</small></button>'; }).join("") : "";
    $("bList").querySelectorAll("[data-i]").forEach(function(c){ c.onclick = function(){ selectBlock(+c.dataset.i); }; });
    $("bRoad").textContent = (S.blocks.length > 1 ? (S.bi + 1) + ". " : "") + b.road; paintLen();
    var lb = b.labels;
    $("lblFwd").textContent = lb.fwd || "This direction"; $("lblRev").textContent = lb.rev || "Opposite direction";
    $("arrFwd").style.transform = "rotate(" + ((lb.fwd_bearing || 90) - 90) + "deg)"; $("arrRev").style.transform = "rotate(" + ((lb.rev_bearing || 270) - 90) + "deg)";
    document.querySelectorAll("#bDir button").forEach(function(x){ x.setAttribute("aria-checked", String(x.dataset.d === b.dir)); });
    $("bAsk").hidden = !!b.dir;
    document.querySelectorAll("#bDir button").forEach(function(x){ x.disabled = !!b.locked; });
    $("bLock").setAttribute("aria-pressed", String(!!b.locked));
    $("bLock").textContent = b.locked ? "\ud83d\udd12 LOCKED \u00b7 UNLOCK" : "\ud83d\udd13 LOCK";
    $("bLock").title = b.locked ? "Unlock to move, resize, change direction or delete this blockage" : "Lock so map taps cannot move it by accident";
  }
  /* ---------------------------------------------------------------- 3. direction (per blockage) */
  document.querySelectorAll("#bDir button").forEach(function(x){ x.onclick = function(){
    var b = AB(); if(!b) return;
    if(b.locked){ toast("Blockage locked \u2014 press UNLOCK to change the direction"); return; }
    b.dir = x.dataset.d; b.locked = true; toast("Blockage locked \u2014 map taps will not move it"); paintBlockCard(); drawBlocks(false); hint("");
    var miss = S.blocks.map(function(y, i){ return y.dir ? null : i + 1; }).filter(Boolean);
    if(miss.length) hint("Choose the blocked direction for blockage " + miss.join(", ")); else runAffected();
  }; });
  $("bAdd").onclick = function(){ S.adding = !S.adding; this.setAttribute("aria-pressed", String(S.adding)); hint(S.adding ? "Tap the road for the next blockage" : ""); };
  $("bZoom").onclick = function(){ if(!S.blocks.length) return; var bb = L.latLngBounds(bLineOf(S.blocks[0])); S.blocks.forEach(function(b){ bb.extend(bLineOf(b)); }); map.fitBounds(bb.pad(.8), {maxZoom:18}); };
  $("bLock").onclick = function(){
    var b = AB(); if(!b) return;
    b.locked = !b.locked; paintBlockCard(); drawBlocks(false);
    hint(b.locked ? "" : "Unlocked \u2014 drag the handles, tap the road to move it, or drag the \u2715 to the bin");
  };
  $("bClear").onclick = function(){
    if(S.blocks.some(function(b){ return b.locked; }) && !confirm("Clear all blockages, including locked ones?")) return; S.blocks = []; S.bi = -1; S.adding = false; resetServices(); G.block.clearLayers(); paintBlockCard(); hint("Tap the road where it is blocked"); };

  /* ---------------------------------------------------------------- 4. affected services, each analysed automatically */
  function resetState(){ S.ai = {}; S.share = {}; S.donors = {}; S.plans = {}; S.pick = {}; S.forced = {}; S.base = {}; S.follow = null; S.followRes = null; S.cases = []; S.caseI = 0; S.caseOff = false; S.runs = {}; S.mil = {}; S.saved = null; S.drawRes = null; S.msgSig = ""; }
  S.pick = {}; S.forced = {}; S.base = {}; S.runs = {}; S.mil = {}; S.cases = [];
  function resetServices(){ resetState(); S.run++; S.services = []; S.sel = null; S.view = null; $("cSvc").hidden = true; $("caseBox").hidden = true; drawReset(); clearServiceMap(); paintRec(); }
  function keyOf(x){ return x.service + "|" + x.direction + "|" + (x.run || 0); }
  function payload(){ return {blocks:S.blocks.map(function(b){ return {line:bLineOf(b), dir:b.dir, road:b.road}; })}; }
  S.op = "";
  function visibleServices(){ return S.services.filter(function(x){ return !S.op || String(x.operator || "").toUpperCase() === S.op; }); }
  $("fOp").onchange = function(){ S.op = this.value; if(S.sel && !visibleServices().some(function(x){ return keyOf(x) === S.sel; })){ S.sel = null; clearServiceMap(); paintRec(); } paintServices(); if(S.services.length) analyseAll(S.run); };
  function runAffected(){
    var run = ++S.run, keepFollow = S.follow; resetState(); S.follow = keepFollow; S.services = []; S.sel = null; S.view = null; clearServiceMap(); paintRec(); drawReset();
    findCases(run);
    $("cSvc").hidden = false; $("svcList").innerHTML = '<p class="dp-muted"><span class="dp-spin"></span>Finding services whose route runs along the blocked section in the blocked direction\u2026</p>';
    post("/api/dv2/affected", payload()).then(function(j){
      if(run !== S.run) return;
      if(j.error){ $("svcList").innerHTML = '<p class="dp-muted">' + esc(j.error) + '</p>'; return; }
      S.services = j.services || [];
      $("svcCount").textContent = S.services.length ? S.services.length + " service-direction" + (S.services.length === 1 ? "" : "s") : "";
      $("svcNote").textContent = S.services.length ? "Each service-direction is analysed on its own. Tap one to see its diversion." : "";
      if(!S.services.length){ $("svcList").innerHTML = '<p class="dp-empty">No bus service runs along this section in the blocked direction' + (S.blocks.some(function(b){ return b.dir !== "both"; }) ? " (services on the other carriageway are not affected)" : "") + '.</p>'; return; }
      paintServices(); analyseAll(run);
    });
  }
  function analyseAll(run){
    var queue = visibleServices().filter(function(x){ return !S.plans[keyOf(x)]; }), busy = 0;   // only what the operator filter shows
    function next(){
      if(run !== S.run) return;
      while(busy < 2 && queue.length){
        var x = queue.shift(), k = keyOf(x); busy++;
        S.plans[k] = {pending:true}; paintServices();
        (function(x, k){
          post("/api/dv2/plan", Object.assign(payload(), {service:x.service, direction:x.direction, run:x.run || 0}), 240000).then(function(p){
            if(run !== S.run) return;
            S.plans[k] = p || {ok:false, error:"No response"}; busy--; paintServices();
            if(!S.sel) selectService(k); else if(S.sel === k) showSelected();
            next();
          });
        })(x, k);
      }
      if(!queue.length && !busy) secondPass(run);
    }
    next();
  }
  /* V16.25 share pass: once every service is analysed, each one is also tried on the OTHER services' best diversions (with its
     own stops and its own bus size). The server adopts one when it works for that bus, or says exactly why not. */
  S.share = {}; S.donors = {};
  function donorOf(x){
    var p = S.plans[keyOf(x)], b = p && p.ok && p.status === "divert" ? p.best : null;
    if(!b || !b.line || b.line.length < 2 || !b.leave_pt || !b.rejoin_pt) return null;
    return {service:x.service, line:b.line, leave_pt:b.leave_pt, rejoin_pt:b.rejoin_pt, roads:b.roads || [], km:b.div_km || 0,
            sig:(b.roads || []).join("|") + "@" + b.rejoin_pt.map(function(v){ return v.toFixed(3); }).join(",")};
  }
  function sharePass(run){
    if(run !== S.run) return;
    var vis = visibleServices();
    if(vis.some(function(x){ var p = S.plans[keyOf(x)]; return !p || p.pending; })) return;      // wait for the last analysis
    var all = vis.map(function(x){ return {k:keyOf(x), x:x, d:donorOf(x)}; }), queue = [];
    all.forEach(function(o){
      var p = S.plans[o.k]; if(!p || !p.ok || S.share[o.k]) return;
      var seen = {}, ds = [];
      if(o.d) seen[o.d.sig] = 1;
      all.forEach(function(q){ if(q.x.service === o.x.service || !q.d || seen[q.d.sig]) return; seen[q.d.sig] = 1; ds.push(q.d); });
      ds.sort(function(a, b){ return a.km - b.km; });
      if(!ds.length){ S.share[o.k] = "none"; return; }
      S.share[o.k] = "queued";
      queue.push({k:o.k, x:o.x, donors:ds.slice(0, 4).map(function(d){ return {service:d.service, line:d.line, leave_pt:d.leave_pt, rejoin_pt:d.rejoin_pt, roads:d.roads}; })});
    });
    if(!queue.length){ paintMsg(); return; }
    replan(run, queue, "done");
  }
  /* V16.26: re-plan services with given routes ("donors"): the other services' routes (share pass), a saved case's route (follow)
     or a route drawn on the map. prefer = the controller's choice: the server uses it whenever it passes the checks. */
  function replan(run, items, mark, each){
    var queue = items.slice(), busy = 0, fin = false;
    items.forEach(function(o){ S.share[o.k] = "queued"; });
    paintServices();
    function next(){
      if(run !== S.run) return;
      while(busy < 2 && queue.length){
        var o = queue.shift(); busy++; S.share[o.k] = "pending"; paintServices(); if(S.sel === o.k) paintRec();
        (function(o){
          post("/api/dv2/plan", Object.assign(payload(), {service:o.x.service, direction:o.x.direction, run:o.x.run || 0, donors:o.donors}), 240000).then(function(p){
            if(run !== S.run) return;
            busy--;
            if(p && p.ok){
              S.plans[o.k] = p; S.donors[o.k] = o.donors; S.share[o.k] = mark;
              delete S.ai[o.k]; delete S.pick[o.k];              // the AI review compares the new candidate set (with the same donors)
              if(each) each(o, p);
              if(S.sel === o.k){ S.view = null; showSelected(); }
            } else S.share[o.k] = "failed";
            paintServices(); if(S.sel === o.k) paintRec(); paintMsg();
            next();
          });
        })(o);
      }
      if(!queue.length && !busy && !fin){ fin = true; if(each) each(null); paintMsg(); }
    }
    next();
  }
  function secondPass(run){ if(S.follow) followPass(run); else sharePass(run); paintMsg(); }
  function stTxt(p, k){
    var sh = k && (S.share[k] === "pending" || S.share[k] === "queued") ? ' \u00b7 <span class="dp-spin"></span>checking other services\u2019 routes' : '';
    if(!p) return ['<span class="dp-st wait">QUEUED</span>', ""];
    if(p.pending) return ['<span class="dp-st run"><span class="dp-spin"></span>ANALYSING</span>', "Searching diversion start \u00d7 rejoin stop\u2026"];
    if(p.ok === false || p.error) return ['<span class="dp-st no">ERROR</span>', esc(p.error || "")];
    var b = p.best;
    var fz = k && S.forced[k];
    if(fz) return ['<span class="dp-st no">CONTROLLER ROUTE</span>', esc(fz.geom.leave.code) + " \u2192 " + esc(fz.geom.rejoin.code) + " \u00b7 skips " + fz.geom.skipped.length + " \u00b7 \u26a0 fails a check" + sh];
    if(p.status === "divert") return ['<span class="dp-st ok">DIVERT \u00b7 ' + esc(b.confidence) + '</span>', esc(b.leave.code) + " \u2192 " + esc(b.rejoin.code) + " \u00b7 skips " + b.skipped_n + " \u00b7 " + b.turns + " turns"
      + (b.preferred ? " \u00b7 " + esc(b.preferred) : b.shared_from ? " \u00b7 same as " + esc(b.shared_from) : "") + sh];
    if(p.status === "review") return ['<span class="dp-st rev">REVIEW</span>', "Low-confidence route only" + sh];
    return ['<span class="dp-st no">NO VERIFIED ROUTE</span>', "Controller review required" + sh];
  }
  function paintServices(){
    var vis = visibleServices();
    $("svcCount").textContent = S.services.length ? (S.op ? vis.length + " of " : "") + S.services.length + " service-direction" + (S.services.length === 1 ? "" : "s") : "";
    if(S.services.length && !vis.length){ $("svcList").innerHTML = '<p class="dp-empty">None of the ' + S.services.length + ' affected service-directions is operated by ' + esc($("fOp").selectedOptions[0].text) + '.</p>'; return; }
    $("svcList").innerHTML = vis.map(function(x){ var k = keyOf(x), t = stTxt(S.plans[k], k);
      return '<button type="button" class="dp-svc" role="listitem" data-k="' + esc(k) + '" aria-pressed="' + (S.sel === k) + '"><span class="num">' + esc(x.service) + '</span><span class="to">D' + x.direction + ' \u00b7 to ' + esc(x.destination || "") + (x.operator ? ' \u00b7 ' + esc(x.operator) : '') + '</span>' + t[0] + '<span class="rs">' + t[1] + '</span></button>'; }).join("");
    $("svcList").querySelectorAll(".dp-svc").forEach(function(b){ b.onclick = function(){ selectService(b.dataset.k); }; });
    paintCommon(); paintDraw(); paintMsg();
  }
  /* V16.25: do the affected services share one diversion? (OCC wants one short route for all of them when it works) */
  function rkey(b){ return ((b.off_roads && b.off_roads.length ? b.off_roads : b.roads) || []).join(" \u2192 "); }      // the roads off the route
  function paintCommon(){
    var el = $("svcNote"), vis = visibleServices(), ks = vis.map(keyOf), base = "Each service-direction is analysed on its own. Tap one to see its diversion.";
    if(!el || !vis.length) return;
    var sharing = ks.some(function(k){ return S.share[k] === "pending" || S.share[k] === "queued"; });
    if(sharing){ el.textContent = "Checking whether every service can use the same short diversion\u2026"; return; }
    if(ks.some(function(k){ var p = S.plans[k]; return !p || p.pending; })){ el.textContent = base; return; }
    var gs = groups(), all = [];                                           // V16.26: the routes the services will really run (step 6)
    gs.list.forEach(function(g){ all = all.concat(g.svcs); }); all = all.concat(gs.none);
    if(all.length < 2){ el.textContent = base; return; }
    var top = gs.list.slice().sort(function(a, b){ return b.svcs.length - a.svcs.length; })[0];
    if(!top || top.svcs.length < 2){ el.textContent = "Each service needs its own diversion \u2014 tap one to see why it cannot use the others\u2019."; return; }
    var diff = all.filter(function(sv){ return top.svcs.indexOf(sv) < 0; });
    el.innerHTML = diff.length ? '<b style="color:#ffd391">Same diversion for ' + top.svcs.length + ' of ' + all.length + ' services</b> (' + esc(top.svcs.join(", ")) + '): ' + esc(routeText(top.f.c || top.f)) + '. Different: <b>' + esc(diff.join(", ")) + '</b> \u2014 tap one to see why.'
      : '<b style="color:#2ee59d">\u2713 All ' + all.length + ' services use the same diversion:</b> ' + esc(routeText(top.f.c || top.f)) + '.';
  }
  function selectService(k){ S.sel = k; S.view = null; paintServices(); showSelected(); }
  function showSelected(){ var p = S.plans[S.sel]; if(p && p.ok) fetchAI(S.sel); paintRec(); drawService(p); }

  /* ---------------------------------------------------------------- 5. the diversion on the map */
  function clearServiceMap(){ ["orig", "skip", "alt", "div", "stops", "marks"].forEach(function(k){ G[k].clearLayers(); }); ARR = ARR.filter(function(s){ return s.grp === G.block; }); }
  S.ai = {};
  function aiOf(){ var a = S.ai[S.sel]; return a && !a.pending ? a : null; }
  function recOf(p){
    if(p && S.sel && S.pick[S.sel] && p.candidates){ var pk = p.candidates.filter(function(x){ return x.id === S.pick[S.sel]; })[0]; if(pk) return pk; }
    if(p && p.best && p.best.preferred) return p.best;                  // the controller's own route (drawn / past case)
    var a = aiOf();
    if(a && a.status === "ai" && p && p.candidates){ var c = p.candidates.filter(function(x){ return x.id === a.candidate_id; })[0]; if(c) return c; }
    return p ? p.best : null;
  }
  function letterOf(id){ var a = S.ai[S.sel], L_ = a && a.letters || {}; for(var k in L_){ if(L_[k] === id) return k; } return ""; }
  function candOf(p){ if(!p || !p.candidates) return null; return S.view ? (p.candidates.filter(function(c){ return c.id === S.view; })[0] || recOf(p)) : recOf(p); }
  function fetchAI(k){
    var x = S.services.filter(function(s){ return keyOf(s) === k; })[0], p = S.plans[k];
    if(!x || !p || !p.ok || !(p.candidates || []).length || S.ai[k] || (p.best && p.best.preferred)) return;
    S.ai[k] = {pending:true}; var run = S.run, dn = S.donors[k];
    post("/api/diversion/ai_route_plan", Object.assign(payload(), {service:x.service, direction:x.direction, run:x.run || 0}, dn ? {donors:dn} : {}), 120000).then(function(r){
      if(run !== S.run || S.donors[k] !== dn) return;
      S.ai[k] = r && r.ok ? r : {status:"fallback", message:"AI Operational Review unavailable \u2014 displaying best algorithm-ranked route.", why:(r && r.error) || "no response"};
      if(S.sel === k){ showSelected(); }
    });
  }
  function drawService(p){
    clearServiceMap(); if(!p || p.pending || !p.ok) return;
    var c = candOf(p), b = recOf(p), bounds = L.latLngBounds(bLineOf(AB() || S.blocks[0])), fz = !S.view && S.forced[S.sel];
    if(fz){ c = null; rline(fz.line, "#c792ff", {pane:"div", weight:7, cls:"dp-anim"}, G.div).bindTooltip("Controller route (fails a check): " + esc(routeText(fz)), {sticky:true}); arrows(G.div, fz.line, "#fff", "arrows", 90); bounds.extend(fz.line); }
    S.blocks.forEach(function(x){ bounds.extend(bLineOf(x)); });
    rline(p.original_line, COL.orig, {pane:"orig", weight:5, casing:.6}, G.orig); arrows(G.orig, p.original_line, COL.orig, "arrows", 140);
    if(c){
      (p.candidates || []).forEach(function(o){
        if(o === c) return;
        var lt = letterOf(o.id);
        L.polyline(o.line, {pane:"alt", color:o === b ? COL.div : COL.alt, weight:o === b ? 5 : 4, opacity:o === b ? .5 : .32, dashArray:o === b ? null : "6 7"})
          .bindTooltip((lt ? "Alternative " + lt + ": " : (o === b ? "Recommended: " : "Alternative: ")) + o.roads.join(" \u2192 ") + " \u2014 tap to view", {sticky:true})
          .on("click", function(){ if(S.draw && S.draw.on) return; S.view = o.id === b.id ? null : o.id; showSelected(); }).addTo(G.alt);
      });
      L.polyline(c.skip_line, {pane:"skip", color:"#fff", weight:11, opacity:.7, interactive:false}).addTo(G.skip);
      L.polyline(c.skip_line, {pane:"skip", color:COL.skip, weight:7, dashArray:"8 8"}).bindTooltip("Original section skipped", {sticky:true}).addTo(G.skip);
      var main = c === b ? COL.div : COL.alt;
      rline(c.line, main, {pane:"div", weight:7, cls:"dp-anim"}, G.div).bindTooltip((c === b ? "Recommended: " : "Alternative: ") + c.roads.join(" \u2192 "), {sticky:true});
      arrows(G.div, c.line, "#fff", "arrows", 90);
      bounds.extend(c.line); bounds.extend(c.skip_line);
      L.marker(c.leave_pt, {pane:"marks", icon:icon("DIVERSION STARTS", "dp-lbl"), keyboard:false}).addTo(G.marks);
      if(c.dd_chain) c.dd_chain.stops.slice(1, -1).forEach(function(s){
        var lg = c.dd_chain.legs.filter(function(l){ return l.codes ? l.codes.indexOf(s.code) >= 0 : (l.from === s.code || l.to === s.code); }).map(function(l){ return l.svc; });
        L.marker([s.lat, s.lon], {pane:"marks", keyboard:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dp-ddstop"></div>'})})
          .bindTooltip(esc(s.code + " " + s.name) + " \u2014 " + (p.bus_type === "SD" ? "service " : "double-deck service ") + esc(lg.join(" / ")) + " stops here").addTo(G.stops);
      });
      L.marker(c.rejoin_pt, {pane:"marks", icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dp-lbl" style="--c:' + COL.rejoin + '">\u21aa REJOIN</div>'}), keyboard:false}).addTo(G.marks);
    }
    var skipped = {}, inb = {};
    if(c) c.skipped.forEach(function(s){ skipped[s.code] = true; if(s.in_block) inb[s.code] = true; });
    else if(fz) fz.geom.skipped.forEach(function(s){ skipped[s.code] = true; });
    var rjc = c ? c.rejoin.code : fz ? fz.geom.rejoin.code : null, lvc = c ? c.leave.code : fz ? fz.geom.leave.code : null;
    (p.stops || []).forEach(function(s){
      var role = s.code === rjc ? "rejoin" : skipped[s.code] ? (inb[s.code] || s.in_block ? "inblock" : "skip") : (s.in_block ? "inblock" : "served");
      var col = {rejoin:COL.rejoin, skip:COL.skip, inblock:COL.inblock, served:COL.served}[role];
      var lab = s.code + " " + s.name + " \u2014 " + {rejoin:"rejoin stop", skip:"skipped", inblock:"skipped (inside the blockage)", served:"served"}[role];
      var big = role === "rejoin" || s.code === lvc, cls = "dp-stop" + (big ? " big" : "") + (role === "skip" || role === "inblock" ? " x" : "");
      L.marker([s.lat, s.lon], {pane:"marks", keyboard:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="' + cls + '" style="--c:' + col + '"></div>'})})
        .bindTooltip(esc(lab)).addTo(G.stops);
    });
    map.fitBounds(bounds.pad(.12), {maxZoom:17});
  }

  /* ---------------------------------------------------------------- 5. AI Operational Review (chooses among VERIFIED routes only) */
  function aiStages(p, ai, b){
    var n = (p.candidates || []).length, h = '<div class="dp-stages">';
    h += '<div class="st"><span class="n">1</span><div><b>Routing Engine</b>Generated ' + n + ' feasible diversion route' + (n === 1 ? '' : 's') + ' \u2014 every one hard-checked: no blocked road, U-turn, wrong-way or prohibited movement, correct rejoin and onward movement.</div></div>';
    if(!ai || ai.pending){
      h += '<div class="st"><span class="n">2</span><div><b>AI Operational Review</b><span class="dp-spin"></span>Comparing ' + n + ' routes as an OCC controller and Bus Captain would\u2026</div></div></div>';
      return h;
    }
    if(ai.status === "ai"){
      var a = ai.ai || {};
      h += '<div class="st"><span class="n">2</span><div><b>AI Operational Review</b>' + esc((ai.review || {}).text || "") + (ai.model ? ' <span class="dp-muted">(' + esc(ai.model) + ')</span>' : '') + '</div></div>';
      h += '<div class="st"><span class="n">3</span><div><b>\u2b50 AI Recommended: Route ' + esc(a.recommended_candidate) + '</b>confidence ' + Math.round((a.confidence || 0) * 100) + '%'
        + (ai.agrees ? ' \u00b7 the algorithm ranked it first too' : ' \u00b7 <span style="color:#ffd391">the algorithm ranked Route ' + esc(ai.algorithm_letter || "?") + ' first</span>') + '</div></div></div>';
      h += '<div class="dp-ai"><div class="dp-k" style="margin:0 0 4px">AI operational assessment</div><p>\u201c' + esc(a.reason || "") + '\u201d</p>'
        + ((a.operational_advantages || []).length ? '<ul class="dp-why">' + a.operational_advantages.map(function(t){ return '<li>' + esc(t) + '</li>'; }).join("") + '</ul>' : '')
        + ((a.concerns || []).length ? '<ul class="dp-why dp-fail">' + a.concerns.map(function(t){ return '<li>' + esc(t) + '</li>'; }).join("") + '</ul>' : '')
        + '<p class="dp-muted" style="margin:6px 0 0">The AI chose among the verified routes only; every road and the map geometry come from the routing engine.</p></div>';
      if(ai.flagged) h += '<div class="dp-banner rev"><b>CONTROLLER REVIEW REQUIRED</b>' + esc(ai.flagged) + '</div>';
      return h;
    }
    return h + '<div class="st"><span class="n">2</span><div><b>AI Operational Review</b><span style="color:#ffd391">' + esc(ai.message || "AI Operational Review unavailable \u2014 displaying best algorithm-ranked route.") + '</span>'
      + (ai.why ? ' <span class="dp-muted">(' + esc(ai.why) + ')</span>' : '') + '</div></div></div>';
  }

  /* ---------------------------------------------------------------- 5. the recommendation panel */
  function pill(t, c){ return '<span class="dp-pill" style="--c:' + c + '">' + esc(t) + '</span>'; }
  var CONF = {HIGH:"#2ee59d", MEDIUM:"#ffb547", LOW:"#8a97a8", "VERY HIGH":"#2ee59d"};
  var LVL = {LOW:"#2ee59d", MEDIUM:"#ffb547", HIGH:"#ff5566"};          // BC complexity: low is good
  var FPC = {SMALL:"#2ee59d", MEDIUM:"#ffb547", LARGE:"#ff5566"};
  function paintRec(){
    var el = $("recBody"), p = S.sel ? S.plans[S.sel] : null, x = S.services.filter(function(s){ return keyOf(s) === S.sel; })[0];
    paintDraw(); paintMsg();
    if(!S.blocks.length){ el.innerHTML = '<p class="dp-empty">Search a road, tap the blocked segment and choose the blocked direction. Every affected service is then analysed automatically and the easiest practical diversion appears here.</p>'; return; }
    if(!allDirs()){ el.innerHTML = '<p class="dp-empty">Choose the blocked direction' + (S.blocks.length > 1 ? ' for every blockage' : '') + ' to find the affected services.</p>'; return; }
    if(!x){ el.innerHTML = '<p class="dp-empty">Select an affected service.</p>'; return; }
    var head = '<div class="dp-rec-h"><span class="no">' + esc(x.service) + '</span><div class="meta"><b>Service ' + esc(x.service) + ' · Direction ' + x.direction + '</b>to ' + esc(x.destination || "") + (p && p.bus_type ? ' · ' + (p.bus_type === "DD" ? "double-deck" : "single-deck") : '') + '</div></div>';
    if(!p || p.pending){ el.innerHTML = head + '<p class="dp-muted"><span class="dp-spin"></span>Analysing: how far the bus can keep its normal route, where it must leave it, and the nearest stop it can rejoin correctly…</p>'; return; }
    if(!p.ok){ el.innerHTML = head + '<div class="dp-banner no"><b>COULD NOT ANALYSE</b>' + esc(p.error || "") + '</div>'; return; }
    var c = candOf(p), b = recOf(p), h = head, ai = S.ai[S.sel], fz = S.forced[S.sel], drawBtn = '<div class="dp-acts"><button type="button" class="ds-btn sm" id="recDraw">✏ DRAW MY OWN</button></div>';
    if(fz && !S.view){                                                   // V16.26: the controller's route, used although it fails a check
      h += '<div class="dp-flag"><b>CONTROLLER DECISION — FAILS A CHECK</b>' + esc(fz.reason || "") + '</div>'
        + '<div class="dp-sum"><div class="rt">' + esc(routeText(fz)) + '</div><div class="meta">' + esc(fz.label) + ' · after ' + esc(fz.geom.leave.code) + ' ' + esc(fz.geom.leave.name) + ' · back at ' + esc(fz.geom.rejoin.code) + ' ' + esc(fz.geom.rejoin.name)
        + ' · ' + skipTxt(fz.geom.skipped) + ' · ' + f1(fz.geom.km) + ' km</div></div>'
        + '<div class="dp-acts"><button type="button" class="ds-btn sm" id="unForce">USE THE SUGGESTION INSTEAD</button></div>' + shareHtml(p, x);
      el.innerHTML = h; $("unForce").onclick = function(){ delete S.forced[S.sel]; paintServices(); showSelected(); }; return;
    }
    if(p.status === "none" || !b){
      h += '<div class="dp-banner no"><b>⚠ NO VERIFIED DIVERSION FOUND</b>Controller review required. A correct “no route” is better than a wrong diversion. You can draw your own (step 5).</div>'
        + (p.failures && p.failures.length ? '<div class="dp-k">Why</div><ul class="dp-why dp-fail">' + p.failures.map(function(f){ return '<li>' + esc(f) + '</li>'; }).join("") + '</ul>' : '')
        + drawBtn + shareHtml(p, x) + attemptsHtml(p);
      el.innerHTML = h; wireRec(el, b); return;
    }
    var aiChip = !ai || ai.pending ? (b.preferred || S.pick[S.sel] ? '' : '<span class="dp-muted"><span class="dp-spin"></span>AI review…</span>')
      : ai.status === "ai" ? '<span class="dp-tag" style="color:#ffd34d;margin:0">⭐ AI ' + (ai.agrees ? 'agrees' : 'picked Route ' + esc((ai.ai || {}).recommended_candidate || "")) + '</span>' : '';
    var lead = b.preferred ? "THE CONTROLLER'S ROUTE · " + b.preferred.toUpperCase() : S.pick[S.sel] ? "YOUR CHOICE" + (letterOf(b.id) ? " · ROUTE " + letterOf(b.id) : "")
      : ai && ai.status === "ai" ? "⭐ AI RECOMMENDED" + (letterOf(b.id) ? " · ROUTE " + letterOf(b.id) : "") : "ALGORITHM RECOMMENDED";
    h += p.status === "divert" || S.pick[S.sel] ? '<div class="dp-banner ok"><b>' + lead + '</b>' + (b.shared_from && !b.preferred ? 'Same diversion as service ' + esc(b.shared_from) + '. ' : '') + 'Checked for this bus: every hard check passed.</div>'
      : '<div class="dp-banner rev"><b>NO SUITABLE DIVERSION FOUND — CONTROLLER REVIEW REQUIRED</b>' + esc(String(p.headline || "").replace(/^.*REQUIRED\s*/, "") || "Only a low-confidence route was found") + ' — shown for reference, not recommended.</div>';
    if(c !== b) h += '<div class="dp-banner rev"><b>SHOWING AN ALTERNATIVE</b><div class="dp-acts"><button type="button" class="ds-btn sm pri" id="useAlt">USE THIS ROUTE</button><button type="button" class="ds-btn sm" id="backRec">BACK TO RECOMMENDED</button></div></div>';
    else if(S.pick[S.sel]) h += '<div class="dp-acts" style="margin:-4px 0 8px"><button type="button" class="ds-btn sm ghost" id="unPick">BACK TO THE SUGGESTION</button></div>';
    h += '<div class="dp-sum"><div class="rt">' + esc(routeText(c)) + '</div><div class="meta">' + skipTxt(c.skipped) + ' · ' + sgn(c.added_km, 1) + ' km · ' + sgn(c.added_min) + ' min · ' + c.turns + ' turns · ' + esc(c.confidence) + ' confidence</div></div>';
    if(aiChip) h += '<div class="dp-chips">' + aiChip + '</div>';
    var ddw = (c.warnings || []).some(function(w){ return /^Double-decker on /.test(w); });     // V16.24: a double-decker on a single-deck road: confirm
    if(c.warnings && c.warnings.length) h += '<div class="dp-banner rev" style="border-style:dashed"><b>' + (ddw ? 'PLEASE CONFIRM' : 'SAFEGUARD' + (c.warnings.length > 1 ? 'S' : '') + ' EXCEEDED') + '</b>' + c.warnings.map(esc).join(" · ") + (ddw ? '' : ' — still the best verified option; please confirm.') + '</div>';
    if(c.bc_brief) h += '<div class="dp-brief"><div class="dp-k" style="margin:0 0 4px">Brief for the Bus Captain</div>' + esc(c.bc_brief) + '</div>';
    h += drawBtn + shareHtml(p, x);
    // the full working, folded away (lean): AI review, roads, turn by turn, checks, why / why not
    var lr = p.last_reachable || {}, earlier = c.earlier, d = aiStages(p, ai, b);
    d += '<dl class="dp-kv">'
      + (earlier ? '<dt>Last reachable stop</dt><dd>' + esc(lr.code) + ' ' + esc(lr.name) + ' <span style="color:#ffd391">— no practical escape after it</span></dd>' : '')
      + '<dt>Last normal stop served</dt><dd><b>' + esc(c.leave.code) + '</b> ' + esc(c.leave.name) + '</dd>'
      + '<dt>Diversion begins</dt><dd>' + (c.keep_km >= 0.5 ? '<b>' + f1(c.keep_km) + ' km after ' + esc(c.leave.name) + '</b> — stays on the normal route' + (c.keep_road ? ' (' + esc(c.keep_road) + ')' : '') + ' and turns off at the last practical exit before the blockage' : 'after ' + esc(c.leave.name)) + '</dd></dl>'
      + '<div class="dp-k">Recommended roads</div><ul class="dp-chain">' + c.roads.map(function(r, i){ return (i ? '<li class="ar">→</li>' : '') + '<li>' + esc(r) + '</li>'; }).join("") + '<li class="ar">→</li><li class="rj">↪ ' + esc(c.rejoin.name) + '</li></ul>'
      + (c.dd_chain ? '<div class="dp-k">Stop to stop on ' + (p.bus_type === "SD" ? 'bus' : 'double-deck') + ' services</div><ul class="dp-ddleg">' + c.dd_chain.legs.map(function(l){
          return '<li><span class="sv">' + (l.own ? esc(x.service) + ' own' : esc(l.svc)) + '</span><span class="tx">' + esc(l.from) + ' ' + esc(tc(l.from_name)) + ' → ' + esc(l.to) + ' ' + esc(tc(l.to_name)) + ' <span class="dp-muted">(' + l.stops + ' stop' + (l.stops === 1 ? '' : 's') + ', ' + f1(l.km) + ' km)</span></span></li>'; }).join("")
          + '</ul><p class="dp-muted" style="margin:4px 0 8px">Every stop on the way is already served by ' + (p.bus_type === "SD" ? 'a bus service' : 'a service that runs double-deckers') + (c.dd_chain.transfers ? '; the route switches service at a stop both serve' : '') + '.'
          + (c.dd_chain.unconfirmed && c.dd_chain.unconfirmed.length ? ' <span style="color:#ffd391">Bus type not yet known for ' + esc(c.dd_chain.unconfirmed.join(", ")) + ' — verify.</span>' : '') + '</p>' : '')
      + '<div class="dp-k">Bus captain instructions</div><ul class="dp-chain">' + c.instruction_text.map(function(t, i){ return (i ? '<li class="ar">→</li>' : '') + '<li style="background:rgba(56,214,255,.1);border-color:rgba(56,214,255,.4);color:#dff6ff">' + esc(tc(t)) + '</li>'; }).join("") + '</ul>'
      + '<dl class="dp-kv">'
      + '<dt>Rejoin</dt><dd><b>' + esc(c.rejoin.code) + '</b> ' + esc(c.rejoin.name) + '</dd>'
      + '<dt>Next original stop verified</dt><dd>' + (c.next.ok ? '✓ ' : '') + esc(c.next.code || "") + ' ' + esc(c.next.name || "") + '</dd>'
      + '<dt>Stops skipped</dt><dd><b>' + c.skipped_n + '</b>' + (c.skipped.length ? ' · ' + c.skipped.map(function(s){ return esc(s.code) + (s.in_block ? "*" : ""); }).join(", ") : '') + (c.unavoidable ? ' <span class="dp-muted">(* inside the blockage)</span>' : '') + '</dd>'
      + '<dt>Extra distance</dt><dd>' + sgn(c.added_km, 1) + ' km</dd>'
      + '<dt>Extra time</dt><dd>' + sgn(c.added_min) + ' min</dd>'
      + '<dt>Turns</dt><dd>' + c.turns + (c.sharp ? ' (' + c.sharp + ' sharp)' : '') + (c.bc && c.bc.right ? ' · ' + c.bc.right + ' right' : '') + (c.bc && c.bc.close_turns ? ' · ' + c.bc.close_turns + ' close together' : '') + '</dd>'
      + '<dt>BC complexity</dt><dd>' + pill(c.bc_label || "–", LVL[c.bc_label] || "#8a97a8") + ' <span class="dp-muted">' + (c.bc ? c.bc.roads + ' road' + (c.bc.roads === 1 ? '' : 's') + (c.bc.unfamiliar_share > 0.5 ? ' · mostly roads no bus uses' : '') + (c.bc.minor_share > 0.3 ? ' · small roads' : '') : '') + '</span></dd>'
      + '<dt>Diversion footprint</dt><dd>' + pill(c.footprint_label || "–", FPC[c.footprint_label] || "#8a97a8") + ' <span class="dp-muted">' + f1(c.div_km) + ' km off route · leaves ' + km(c.leave_before_m || 0) + ' before, rejoins ' + km(Math.max(0, c.rejoin_after_m || 0)) + ' after the blockage</span></dd>'
      + '<dt>Bus-road confidence</dt><dd>' + pill(c.busroad_label, CONF[c.busroad_label] || "#8a97a8") + ' <span class="dp-muted">' + esc(c.evidence_text) + '</span></dd>'
      + '<dt>Overall confidence</dt><dd>' + pill(c.confidence, CONF[c.confidence]) + ' <span class="dp-muted">OCC score ' + f0(c.score) + '/100</span></dd></dl>';
    if(c === b && p.why && p.why.length) d += '<div class="dp-k">Why this route?</div><ul class="dp-why">' + p.why.map(function(t){ return '<li>' + esc(t) + '</li>'; }).join("") + '</ul>';
    if(c === b && p.why_not && p.why_not.length) d += '<div class="dp-k">Why not…</div>' + p.why_not.map(function(w){ return '<div class="dp-not"><b>' + esc(tc(w.title)) + '</b>' + esc(w.text) + '</div>'; }).join("");
    d += '<div class="dp-muted" style="margin-top:6px">OCC score ' + f0(c.score) + '/100: ' + Object.keys(c.parts || {}).map(function(k){ return esc((S.cfg && S.cfg.weight_labels || {})[k] || k) + ' ' + f1(c.parts[k]); }).join(" · ") + '. Scored only after every hard check passed (blocked road, U-turn, wrong-way, impossible turn, restricted road, rejoin direction, next original stop).</div>';
    h += '<details class="dp-det" id="recMore"' + (S.more ? ' open' : '') + '><summary>Details — why this route, turn by turn, checks</summary><div style="margin-top:6px">' + d + '</div></details>';
    var alts = (p.candidates || []).slice().sort(function(x, y){ return (letterOf(x.id) || "z").localeCompare(letterOf(y.id) || "z"); });
    if(alts.length > 1) h += '<details class="dp-det"' + (S.view ? ' open' : '') + '><summary>Show alternatives (' + (alts.length - 1) + ')</summary><div style="margin-top:6px">' + alts.map(function(a){
        var tg = (a.tags || []).map(function(t){ return {A:"MIN SKIPPED STOPS", B:"SIMPLEST", C:"FASTEST"}[t]; }).filter(Boolean).join(" · ");
        return '<button type="button" class="dp-alt" data-alt="' + esc(a.id) + '" aria-pressed="' + ((S.view || b.id) === a.id) + '"><b>' + (letterOf(a.id) ? 'Route ' + letterOf(a.id) + ' · ' : '') + esc(a.leave.code) + ' → ' + esc(a.rejoin.code) + '</b>'
          + (ai && ai.status === "ai" && a.id === ai.candidate_id ? '<span class="dp-tag" style="color:#ffd34d">⭐ AI</span>' : '') + (a.id === p.best.id ? '<span class="dp-tag" style="color:#9fdff5">ALGORITHM #1</span>' : '') + (a.id === b.id ? '<span class="dp-tag" style="color:#2ee59d">RECOMMENDED</span>' : '') + (a.dd_chain ? '<span class="dp-tag" style="color:#cdb9ff">' + (p.bus_type === "SD" ? 'STOP CHAIN' : 'DD STOP CHAIN') + '</span>' : '') + (a.found_by === "early" ? '<span class="dp-tag" style="color:#9fdff5">NEAREST REJOIN</span>' : '') + (a.shared_from ? '<span class="dp-tag" style="color:#ffd391">' + (a.preferred ? esc(a.preferred).toUpperCase() : 'SAME AS ' + esc(a.shared_from)) + '</span>' : '') + (tg ? '<span class="dp-tag">' + tg + '</span>' : '')
          + '<br>' + esc(a.roads.join(" → ")) + '<br><span class="dp-muted">skips ' + a.skipped_n + ' · ' + a.turns + ' turns · ' + sgn(a.added_min) + ' min · ' + sgn(a.added_km, 1) + ' km · ' + esc(a.busroad_label) + ' bus-road · ' + esc(a.confidence) + ' · score ' + f0(a.score) + '</span></button>'; }).join("") + '</div></details>';
    h += attemptsHtml(p);
    el.innerHTML = h;
    wireRec(el, b, c);
  }
  function wireRec(el, b, c){
    var br = $("backRec"); if(br) br.onclick = function(){ S.view = null; showSelected(); };
    var ua = $("useAlt"); if(ua) ua.onclick = function(){ S.pick[S.sel] = c.id; S.view = null; paintServices(); showSelected(); toast("Route chosen for this service"); };
    var up = $("unPick"); if(up) up.onclick = function(){ delete S.pick[S.sel]; showSelected(); };
    var rd = $("recDraw"); if(rd) rd.onclick = function(){ drawStart(); };
    var mo = $("recMore"); if(mo) mo.ontoggle = function(){ S.more = mo.open; };
    el.querySelectorAll("[data-alt]").forEach(function(a){ a.onclick = function(){ S.view = b && a.dataset.alt === b.id ? null : a.dataset.alt; showSelected(); };
      if(a.tagName === "LI") a.onkeydown = function(e){ if(e.key === "Enter" || e.key === " "){ e.preventDefault(); a.onclick(); } }; });
  }
  function shareHtml(p, x){
    var sh = p.shared || [], st_ = S.share[S.sel];
    if(st_ === "pending" || st_ === "queued") return '<p class="dp-muted" style="margin:8px 0 0"><span class="dp-spin"></span>Checking the other affected services\u2019 diversions for this bus\u2026</p>';
    if(!sh.length) return "";
    var ids = {}; (p.candidates || []).forEach(function(c){ ids[c.id] = 1; });
    return '<div class="dp-k">Other services\u2019 diversions \u2014 checked for this bus</div><ul class="dp-att dp-share">' + sh.map(function(s){
      var view = s.ok && !s.used && ids[s.id];
      return '<li class="' + (s.ok ? "ok" : "no") + '"' + (view ? ' data-alt="' + esc(s.id) + '" role="button" tabindex="0" style="cursor:pointer" title="Show this route on the map"' : '') + '><span class="i">' + (s.ok ? "\u2713" : "\u2715") + '</span><span><b>Service ' + esc(s.from) + '</b> ' + esc((s.roads || []).join(" \u2192 "))
        + (s.ok ? ' <span class="dp-muted">(' + esc(s.leave || "") + ' \u2192 ' + esc(s.rejoin || "") + ', skips ' + (s.skipped_n || 0) + ')</span>' : '') + (view ? ' <span style="color:#9fdff5">\u2014 view</span>' : '') + '</span><span class="r">' + esc(s.reason || "") + '</span></li>'; }).join("")
      + '</ul><p class="dp-muted" style="margin:4px 0 0">Each one was asked again from service ' + esc(x.service) + '\u2019s own stops for a ' + (p.bus_type === "SD" ? 'single' : 'double') + '-deck bus. Live bus positions are not used.</p>';
  }
  function attemptsHtml(p){
    var at = p.attempts || []; if(!at.length) return "";
    return '<details class="dp-det"><summary>Route search \u2014 ' + at.length + ' diversion start \u00d7 rejoin combinations, ' + f0((p.search || {}).routes) + ' road routes checked</summary>'
      + '<p class="dp-muted" style="margin:6px 0 0">Last reachable stop ' + esc((p.last_reachable || {}).code || "\u2013") + ' \u00b7 first stop after the blockage ' + esc((p.first_after || {}).code || "\u2013") + '. Nearest combinations first; earlier diversion starts only when they help.</p>'
      + (p.dd_chain ? '<p class="dp-muted" style="margin:6px 0 0"><b style="color:#cdb9ff">' + (p.bus_type === "SD" ? 'Stop-to-stop chain' : 'Double-deck stop chain') + ':</b> ' + esc(p.dd_chain.text) + '</p>' : '') + '<ul class="dp-att">'
      + at.map(function(a){ var ok = a.result === "valid"; return '<li class="' + (ok ? "ok" : "no") + '"><span class="i">' + (ok ? "\u2713" : "\u2715") + '</span><span>' + (a.earlier ? '<em style="font-style:normal;color:#ffd391">earlier \u00b7 </em>' : '') + esc(a.from_code) + ' ' + esc(a.from_name) + ' \u2192 ' + esc(a.to_code) + ' ' + esc(a.to_name) + '</span><span class="r">' + esc(a.reason) + '</span></li>'; }).join("") + '</ul></details>';
  }

  /* ---------------------------------------------------------------- weights + legend */
  $("lgBtn").onclick = function(){ var on = $("legend").hidden; $("legend").hidden = !on; this.setAttribute("aria-pressed", String(on)); };
  $("wBtn").onclick = function(){
    Promise.all([get("/api/dv2/limits"), get("/api/dv2/weights")]).then(function(r){
      var lj = r[0] || {}, wj = r[1] || {}, L_ = lj.limits || {}, ll = lj.labels || {}, w = wj.weights || {}, lb = wj.labels || {};
      $("lRows").innerHTML = Object.keys(L_).map(function(k){ var d = ll[k] || [k, "", 0, 100, 1];
        return '<label class="dp-lim"><span>' + esc(d[0]) + ' <small>(default ' + esc(String((lj.defaults || {})[k])) + ')</small></span><input type="number" min="' + d[2] + '" max="' + d[3] + '" step="' + d[4] + '" value="' + L_[k] + '" data-l="' + k + '" aria-label="' + esc(d[0]) + '"><small>' + esc(d[1]) + '</small></label>'; }).join("");
      $("wRows").innerHTML = Object.keys(w).map(function(k){ return '<label class="dp-w"><span>' + esc(lb[k] || k) + ' <small style="color:var(--ds-mut)">(default ' + f0((wj.defaults || {})[k]) + ')</small></span><input type="range" min="0" max="50" step="1" value="' + w[k] + '" data-k="' + k + '" aria-label="' + esc(lb[k] || k) + '"><b data-v="' + k + '">' + f0(w[k]) + '</b></label>'; }).join("");
      $("wRows").querySelectorAll("input").forEach(function(i){ i.oninput = function(){ $("wRows").querySelector('[data-v="' + i.dataset.k + '"]').textContent = i.value; }; });
      $("wModal").hidden = false; $("lRows").querySelector("input").focus();
    });
  };
  function closeW(){ $("wModal").hidden = true; $("wBtn").focus(); }
  $("wCancel").onclick = closeW;
  $("wModal").addEventListener("keydown", function(e){ if(e.key === "Escape") closeW(); });
  function saveW(lim, wts){
    Promise.all([post("/api/dv2/limits", lim), post("/api/dv2/weights", wts)]).then(function(r){
      var err = (r[0] || {}).error || (r[1] || {}).error; if(err) return toast(err);
      closeW(); toast("Settings saved \u2014 re-analysing"); if(allDirs()) runAffected();
    });
  }
  $("wSave").onclick = function(){
    var l = {}, w = {};
    $("lRows").querySelectorAll("input").forEach(function(i){ l[i.dataset.l] = +i.value; });
    $("wRows").querySelectorAll("input").forEach(function(i){ w[i.dataset.k] = +i.value; });
    saveW({limits:l}, {weights:w});
  };
  $("wReset").onclick = function(){ saveW({reset:true}, {reset:true}); };


  /* ---------------------------------------------------------------- V16.23 magnifying lens (🔍): a round loupe that shows the map closer,
     with every line, stop, arrow and label on it. Mouse: it follows the pointer and clicks still reach the map underneath.
     Touch: it sits in the middle of the map - move the map under it, tap the lens to tap that exact point.
     Press and hold anywhere on the map (mouse or finger): the lens opens by itself - see "press and hold" below. */
  var LENS = {on:false, el:null, map:null, z:0, touch:false, grp:null, t:null, base:"", last:null, press:false};
  var LENS_STEPS = [0, 2, 3];                     // off, x4, x8 (zoom levels added)
  function lensBuild(){
    if(LENS.el) return;
    var el = document.createElement("div"); el.className = "dp-lens"; el.setAttribute("aria-hidden", "true");
    el.innerHTML = '<div class="dp-lens-map"></div><i class="dp-lens-x"></i><span class="dp-lens-z"></span>';
    map.getContainer().parentNode.appendChild(el);
    var lm = L.map(el.querySelector(".dp-lens-map"), {zoomControl:false, attributionControl:false, dragging:false, scrollWheelZoom:false, doubleClickZoom:false, boxZoom:false,
      keyboard:false, touchZoom:false, tap:false, inertia:false, zoomAnimation:false, fadeAnimation:false, markerZoomAnimation:false, minZoom:10, maxZoom:20});
    [["road", 400], ["orig", 410], ["skip", 415], ["alt", 420], ["div", 430], ["block", 440], ["arrows", 450], ["marks", 620]].forEach(function(p){ lm.createPane(p[0]).style.zIndex = p[1]; });
    LENS.el = el; LENS.map = lm; LENS.grp = L.layerGroup().addTo(lm);
    el.addEventListener("click", function(){ if(LENS.touch) map.fire("click", {latlng:map.getCenter()}); });
  }
  function lensCopy(ly, into){
    if(ly instanceof L.LayerGroup){ ly.eachLayer(function(x){ lensCopy(x, into); }); return; }
    var pn = ly.options && ly.options.pane, o = Object.assign({}, ly.options, {interactive:false});
    delete o.pane; delete o.renderer; if(pn && LENS.map.getPane(pn)) o.pane = pn;       // never hand Leaflet an empty pane (a marker would fail)
    if(ly instanceof L.CircleMarker) L.circleMarker(ly.getLatLng(), o).addTo(into);
    else if(ly instanceof L.Polyline) (ly instanceof L.Polygon ? L.polygon : L.polyline)(ly.getLatLngs(), o).addTo(into);
    else if(ly instanceof L.Marker){ var mo = {icon:ly.options.icon, interactive:false, keyboard:false}; if(o.pane) mo.pane = o.pane; L.marker(ly.getLatLng(), mo).addTo(into); }
  }
  function lensSync(){                         // base map + every overlay, copied (only while the lens is on)
    if(!(LENS.on || LENS.press) || !LENS.map) return;
    var base = [];
    map.eachLayer(function(ly){ if(ly instanceof L.TileLayer) base.push(ly); });
    var key = base.map(function(b){ return b._url; }).join("|");
    if(key !== LENS.base){
      LENS.map.eachLayer(function(ly){ if(ly instanceof L.TileLayer) LENS.map.removeLayer(ly); });
      base.forEach(function(b){ L.tileLayer(b._url, Object.assign({}, b.options, {attribution:"", maxNativeZoom:b.options.maxNativeZoom || b.options.maxZoom || 19, maxZoom:22})).addTo(LENS.map); });   // beyond the tiles' own zoom: enlarge the last ones
      LENS.base = key;
    }
    LENS.grp.clearLayers();
    Object.keys(G).forEach(function(k){ lensCopy(G[k], LENS.grp); });
  }
  function lensLater(){ if(!(LENS.on || LENS.press)) return; clearTimeout(LENS.t); LENS.t = setTimeout(lensSync, 120); }
  function lensView(ll){
    if(!(LENS.on || LENS.press) || !ll) return;
    var z = LENS.z || 2;                         // press and hold with the button off: x4
    LENS.map.setView(ll, Math.min(20, map.getZoom() + z), {animate:false});
    LENS.el.querySelector(".dp-lens-z").textContent = "×" + Math.pow(2, z) + (LENS.press ? " · " + window.DV_LENS_LABEL() : LENS.touch ? " · tap to pick" : "");
  }
  function lensSet(step){
    LENS.z = step; LENS.on = step > 0; LENS.touch = !!(window.matchMedia && matchMedia("(hover: none), (pointer: coarse)").matches);
    var b = $("lensBtn"), host = map.getContainer().parentNode;
    b.setAttribute("aria-pressed", String(LENS.on)); b.textContent = LENS.on ? "🔍 ×" + Math.pow(2, step) : "🔍";
    b.title = LENS.on ? "Magnifying lens ×" + Math.pow(2, step) + (step === 2 ? " — click for ×8" : " — click to switch off") : "Magnifying lens: see the roads and stops closer";
    if(!LENS.on){ if(LENS.el) LENS.el.classList.remove("on"); host.classList.remove("lens-hover"); if(/tap the lens/.test($("mapHint").textContent)) hint(""); return; }
    lensBuild();
    LENS.el.classList.toggle("touch", LENS.touch);
    host.classList.toggle("lens-hover", !LENS.touch);
    if(LENS.touch){ LENS.el.classList.add("on"); LENS.el.style.left = ""; LENS.el.style.top = ""; hint("Drag the map until the road is under the cross, then tap the lens"); }
    LENS.map.invalidateSize(); LENS.base = ""; lensSync();
    lensView(LENS.touch ? map.getCenter() : (LENS.last || map.getCenter()));
  }
  $("lensBtn").onclick = function(){ var i = LENS_STEPS.indexOf(LENS.z); lensSet(LENS.on ? LENS_STEPS[(i + 1) % LENS_STEPS.length] : 2); };
  map.on("mousemove", function(e){
    LENS.last = e.latlng; if(!LENS.on || LENS.touch || LENS.press) return;
    LENS.el.style.left = e.containerPoint.x + "px"; LENS.el.style.top = e.containerPoint.y + "px"; LENS.el.classList.add("on");
    lensView(e.latlng);
  });
  map.getContainer().addEventListener("mouseleave", function(){ if(LENS.on && !LENS.touch && LENS.el) LENS.el.classList.remove("on"); });
  map.on("move zoom", function(){ if(LENS.press) return; if(LENS.on && LENS.touch) lensView(map.getCenter()); else if(LENS.on && LENS.last) lensView(LENS.last); });
  map.on("layeradd layerremove", lensLater);
  document.addEventListener("keydown", function(e){ if(e.key === "Escape"){ if(LENS.press) pressEnd(false); else if(LENS.on) lensSet(0); } });

  /* press and hold on the map: the lens opens by itself (no button needed) after HOLD_MS without moving.
     Keep holding and slide to the exact spot - on a phone the lens sits above the finger so the finger does not hide it.
     Let go: that spot is tapped (blockage placed / moved). A locked blockage is not moved - the lens then only shows.
     A quick tap, or pressing and dragging straight away, works exactly as before (tap / move the map). */
  var PRESS = {t:null, id:null, x0:0, y0:0, touch:false, ll:null, until:0}, HOLD_MS = 400;
  function canPick(){ if(S.draw && S.draw.on) return true; var b = AB(); return !(S.blocks.length && !S.adding && b && b.locked); }   // the same rule as a map tap
  window.DV_LENS_CAN = function(){ return canPick(); };
  window.DV_CLICK_THROUGH = function(){ return !!(S.draw && S.draw.on); };     // 3D: while drawing, every tap is a point
  window.DV_LENS_LABEL = function(){ return S.draw && S.draw.on ? "let go to add a point" : canPick() ? "let go to pick" : "view only"; };   // the 3D magnifier
  function pressMove(cx, cy){
    var r = map.getContainer().getBoundingClientRect();
    var p = L.point(Math.max(0, Math.min(r.width, cx - r.left)), Math.max(0, Math.min(r.height, cy - r.top))), x = p.x, y = p.y;
    if(PRESS.touch){ y = p.y - 120 < 95 ? p.y + 120 : p.y - 120; x = Math.max(70, Math.min(r.width - 70, p.x)); }
    LENS.el.style.left = x + "px"; LENS.el.style.top = y + "px";
    PRESS.ll = map.containerPointToLatLng(p); lensView(PRESS.ll);
  }
  function pressStart(){
    PRESS.t = null; LENS.press = true; lensBuild();
    if(map.dragging) map.dragging.disable();                        // the finger now moves the lens, not the map
    map.getContainer().parentNode.classList.add("lens-press");
    LENS.el.classList.remove("touch"); LENS.el.classList.add("on", "press");
    LENS.map.invalidateSize(); lensSync(); pressMove(PRESS.x0, PRESS.y0);
    if(PRESS.touch && navigator.vibrate) try{ navigator.vibrate(12); }catch(err){}
  }
  function pressCancel(){ if(PRESS.t){ clearTimeout(PRESS.t); PRESS.t = null; } if(!LENS.press) PRESS.id = null; }
  function pressEnd(pick){
    pressCancel(); if(!LENS.press) return;
    var ll = PRESS.ll; LENS.press = false; PRESS.id = null; PRESS.until = Date.now() + 700;
    if(map.dragging) map.dragging.enable();
    map.getContainer().parentNode.classList.remove("lens-press");
    LENS.el.classList.remove("press");
    if(LENS.on && LENS.touch){ LENS.el.classList.add("touch"); LENS.el.style.left = ""; LENS.el.style.top = ""; LENS.map.invalidateSize(); lensView(map.getCenter()); }
    else if(LENS.on) lensView(ll);                                  // mouse with the lens on: it keeps following the pointer
    else LENS.el.classList.remove("on");
    if(pick && ll && canPick()) map.fire("click", {latlng:ll});
  }
  (function(host){
    host.addEventListener("pointerdown", function(e){
      if(LENS.press){ pressEnd(false); return; }                     // a second finger (pinch): stop
      if(PRESS.t){ pressCancel(); return; }
      if(!e.isPrimary || (e.pointerType === "mouse" && e.button !== 0)) return;
      var t = e.target;
      if(!map.getContainer().contains(t) || (t.closest && t.closest(".leaflet-control-container, .leaflet-marker-icon.leaflet-interactive"))) return;   // not the zoom buttons, the blockage X or its handles
      PRESS.id = e.pointerId; PRESS.x0 = e.clientX; PRESS.y0 = e.clientY; PRESS.touch = e.pointerType !== "mouse";
      PRESS.t = setTimeout(pressStart, HOLD_MS);
    }, true);
    window.addEventListener("pointermove", function(e){
      if(e.pointerId !== PRESS.id) return;
      if(LENS.press){ e.preventDefault(); pressMove(e.clientX, e.clientY); }
      else if(PRESS.t && Math.hypot(e.clientX - PRESS.x0, e.clientY - PRESS.y0) > 8) pressCancel();   // moving straight away: a normal map drag
    }, true);
    window.addEventListener("pointerup", function(e){ if(e.pointerId !== PRESS.id) return; if(LENS.press) pressEnd(true); else pressCancel(); }, true);
    window.addEventListener("pointercancel", function(e){ if(e.pointerId !== PRESS.id) return; if(LENS.press) pressEnd(false); else pressCancel(); }, true);
    host.addEventListener("contextmenu", function(e){ if(LENS.press || PRESS.t) e.preventDefault(); }, true);   // no long-press menu
    host.addEventListener("click", function(e){                        // the browser's own click after letting go: already handled
      if(PRESS.until && Date.now() < PRESS.until && map.getContainer().contains(e.target)){ e.stopPropagation(); e.preventDefault(); PRESS.until = 0; }
    }, true);
  })(map.getContainer().parentNode);

  /* ================================================================ V16.26 lean planner: 5 draw your own, 6 the recommendation
     message (as OCC sends it), 7 save the case - and saved cases offered again when the same road is blocked */
  var ABR = [[/\bAvenue\b/g, "Ave"], [/\bRoad\b/g, "Rd"], [/\bStreet\b/g, "St"], [/\bDrive\b/g, "Dr"], [/\bCrescent\b/g, "Cres"], [/\bExpressway\b/g, "Expwy"],
    [/\bBoulevard\b/g, "Blvd"], [/\bCentral\b/g, "Ctrl"], [/\bPlace\b/g, "Pl"], [/\bLane\b/g, "Ln"], [/\bTerrace\b/g, "Ter"], [/\bClose\b/g, "Cl"], [/\bSquare\b/g, "Sq"],
    [/\bGardens?\b/g, "Gdns"], [/\bHeights\b/g, "Hts"], [/\bIndustrial\b/g, "Ind"], [/\bJunction\b/g, "Jct"], [/\bInterchange\b/g, "Int"], [/\bStation\b/g, "Stn"],
    [/\bUpper\b/g, "Upp"], [/\bNorth\b/g, "Nth"], [/\bSouth\b/g, "Sth"], [/\bCommonwealth\b/g, "C'wealth"], [/\bBukit\b/g, "Bt"], [/\bJalan\b/g, "Jln"],
    [/\bLorong\b/g, "Lor"], [/\bTanjong\b/g, "Tg"], [/\bKampong\b/g, "Kg"], [/\bMount\b/g, "Mt"]];
  function abbr(r){ var t = tc(r); ABR.forEach(function(a){ t = t.replace(a[0], a[1]); }); return t; }
  function chainRaw(c){ var out = []; [c.from_road].concat(c.roads || [], [c.back_road]).filter(Boolean).forEach(function(x){ if(out[out.length - 1] !== x) out.push(x); }); return out; }
  function chain(c){ var out = []; chainRaw(c).map(abbr).forEach(function(x){ if(out[out.length - 1] !== x) out.push(x); }); return out; }
  function routeText(c){ var ch = chain(c); return ch.length ? ch.join(" > ") + " (Back on route)" : "(route drawn on the map)"; }
  function joinAnd(a){ return a.length < 2 ? (a[0] || "") : a.slice(0, -1).join(", ") + " & " + a[a.length - 1]; }
  function skipCount(list){ list = list || []; return list.length + (list.length ? " (" + joinAnd(list.map(function(s){ return "BS " + s.code; })) + ")" : ""); }
  function skipTxt(list){ return "Skips " + esc(skipCount(list)); }
  function uniqA(a){ return a.filter(function(x, i){ return a.indexOf(x) === i; }); }

  /* the route each service will really run: the controller's choice, else the recommendation */
  function finalOf(k){
    var fz = S.forced[k];
    if(fz) return {src:fz.src, label:fz.label, roads:fz.roads, key:"M:" + fz.roads.join("|"), skipped:fz.geom.skipped, km:fz.geom.km, line:fz.line,
                   leave_pt:fz.line[0], rejoin_pt:fz.line[fz.line.length - 1], leave:fz.geom.leave, rejoin:fz.geom.rejoin, flags:[fz.label + ": " + (fz.reason || "fails a check")], c:fz};
    var p = S.plans[k]; if(!p || !p.ok || !p.best || p.pending) return null;
    var c = S.pick[k] ? (p.candidates || []).filter(function(x){ return x.id === S.pick[k]; })[0] : null;
    if(!c && p.status !== "divert") return null;
    var a = S.ai[k];
    if(!c && !p.best.preferred && a && a.status === "ai") c = (p.candidates || []).filter(function(x){ return x.id === a.candidate_id; })[0];
    c = c || p.best;
    var src = S.pick[k] ? "alternative" : c.preferred ? (S.share[k] === "manual" ? "manual" : "case") : (a && a.status === "ai" && c.id !== p.best.id ? "ai" : "algorithm");
    var used = c.preferred ? (p.shared || []).filter(function(x){ return x.used; })[0] : null;     // the controller's route: grouped with the same route used anyway elsewhere
    return {src:src, label:c.preferred || "", roads:c.roads, key:used ? "M:" + (used.roads || []).join("|") : rkey(c), skipped:c.skipped || [], km:c.div_km || 0, line:c.line, leave_pt:c.leave_pt, rejoin_pt:c.rejoin_pt,
            leave:c.leave, rejoin:c.rejoin, flags:c.warnings || [], c:c};
  }
  function groups(){
    var list = [], by = {}, none = [];
    visibleServices().forEach(function(x){
      var k = keyOf(x), p = S.plans[k]; if(!p || p.pending) return;
      var f = finalOf(k); if(!f){ if(p.ok && none.indexOf(x.service) < 0) none.push(x.service); return; }
      var g = by[f.key];
      if(!g){ g = by[f.key] = {key:f.key, f:f, svcs:[], skipped:[], kms:[], srcs:[], flags:[], items:[]}; list.push(g); }
      if(g.svcs.indexOf(x.service) < 0) g.svcs.push(x.service);
      f.skipped.forEach(function(s){ if(!g.skipped.some(function(y){ return y.code === s.code; })) g.skipped.push(s); });
      g.kms.push(f.km || 0); g.srcs.push(f.src); g.items.push({x:x, k:k, f:f});
      f.flags.forEach(function(t){ if(g.flags.indexOf(t) < 0) g.flags.push(t); });
    });
    list.forEach(function(g, i){ g.n = i + 1; g.km = Math.round(g.kms.reduce(function(a, b){ return a + b; }, 0) / g.kms.length * 10) / 10; });
    return {list:list, none:none};
  }
  function runsOf(g){ return String(S.runs[g.key] || "").split(/\n/).map(function(t){ return t.trim(); }).filter(Boolean); }
  function milAuto(g){ return Math.round(g.km * runsOf(g).length * 10) / 10; }
  function milOf(g){ var v = String(S.mil[g.key] || "").trim(); return v !== "" && !isNaN(+v) ? (Math.round(+v * 10) / 10).toFixed(1) : milAuto(g).toFixed(1); }
  function msgText(gs){
    var L_ = [];
    gs.list.forEach(function(g){
      if(L_.length) L_.push("");
      L_.push("Diverted Route " + g.n + " : [" + g.svcs.join(", ") + "]");
      L_.push(routeText(g.f.c || g.f));
      L_.push("", "Bus stops skipped: " + skipCount(g.skipped), "", "Diverted Runs:");
      var rs = runsOf(g); L_.push(rs.length ? rs.join("\n") : "–");
      L_.push("", "Total Mileage salvaged: " + milOf(g) + "km.");
    });
    if(gs.none.length) L_.push("", "No verified diversion yet: " + gs.none.join(", ") + " (controller review)");
    return L_.join("\n");
  }
  function paintMsg(){
    var gs = groups(), has = gs.list.length > 0;
    $("cMsg").hidden = !has; $("cSave").hidden = !has;
    if(!has){ S.msgSig = ""; return; }
    $("mText").textContent = msgText(gs);
    var sig = gs.list.map(function(g){ return g.n + ":" + g.key + ":" + g.svcs.join(","); }).join("|");
    S.msgG = gs.list;
    if(sig !== S.msgSig){
      S.msgSig = sig;
      $("mEdit").innerHTML = gs.list.map(function(g, i){
        return '<div class="dp-medit"><h4>Route ' + g.n + ' · ' + esc(g.svcs.join(", ")) + '</h4>'
          + (g.flags.length ? '<div class="dp-muted" style="color:#ffc3ca">⚠ ' + esc(g.flags.join(" · ")) + '</div>' : '')
          + '<label for="mr' + i + '">Diverted runs — one per line: run / bus / BC</label><textarea class="dp-ta" id="mr' + i + '" data-i="' + i + '" rows="2" placeholder="10957407 / SBS8973R / BC 61748">' + esc(S.runs[g.key] || "") + '</textarea>'
          + '<label for="mm' + i + '">Mileage salvaged (km)</label><input class="dp-in" id="mm' + i + '" data-m="' + i + '" inputmode="decimal" value="' + esc(S.mil[g.key] || "") + '" placeholder="auto"> <span class="dp-muted" data-auto="' + i + '"></span></div>'; }).join("");
      $("mEdit").querySelectorAll("textarea[data-i]").forEach(function(t){ t.oninput = function(){ S.runs[S.msgG[+t.dataset.i].key] = t.value; msgRefresh(); }; });
      $("mEdit").querySelectorAll("input[data-m]").forEach(function(t){ t.oninput = function(){ S.mil[S.msgG[+t.dataset.m].key] = t.value; msgRefresh(); }; });
    }
    autoHints(gs.list);
  }
  function autoHints(list){ list.forEach(function(g, i){ var e = $("mEdit").querySelector('[data-auto="' + i + '"]'); if(e) e.textContent = "auto: " + g.km.toFixed(1) + " km × " + runsOf(g).length + " run" + (runsOf(g).length === 1 ? "" : "s") + " = " + milAuto(g).toFixed(1) + " km"; }); }
  function msgRefresh(){ var gs = groups(); $("mText").textContent = msgText(gs); autoHints(gs.list); }
  $("mCopy").onclick = function(){
    var t = $("mText").textContent, done = function(){ toast("Copied — paste it into the chat"); };
    if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, function(){ fallbackCopy(t); done(); }); else { fallbackCopy(t); done(); }
  };
  function fallbackCopy(t){ var a = document.createElement("textarea"); a.value = t; a.style.position = "fixed"; a.style.opacity = "0"; document.body.appendChild(a); a.select(); try{ document.execCommand("copy"); }catch(e){} a.remove(); }

  /* ---------------------------------------------------------------- 5. draw your own diversion on the map */
  S.draw = {on:false, pts:[], line:null, roads:[], km:0, snapped:false, note:""}; S.drawFor = "all";
  function drawOff(){ $("drawBar").hidden = true; var m = document.querySelector(".dp-map"); if(m) m.classList.remove("drawing"); }
  function drawReset(){ S.draw = {on:false, pts:[], line:null, roads:[], km:0, snapped:false, note:""}; S.drawRes = null; if(G.draw) G.draw.clearLayers(); drawOff(); paintDraw(); }
  function drawStart(){
    if(!S.services.length){ toast("Place the blockage and choose its direction first"); return; }
    S.draw.on = true; S.view = null; $("drawBar").hidden = false; document.querySelector(".dp-map").classList.add("drawing"); drawBar(); hint(""); drawMap(); paintDraw();
    var r = $("map").getBoundingClientRect(); if(r.top < 0 || r.bottom > window.innerHeight) $("map").scrollIntoView({block:"center", behavior:"smooth"});
  }
  function drawBar(){ var d = S.draw; $("drawTxt").textContent = d.pts.length ? d.pts.length + " point" + (d.pts.length === 1 ? "" : "s") + (d.line ? " · " + f1(d.km) + " km · " + chain({roads:d.roads}).join(" > ") : "") + " — keep tapping, or DONE"
    : "Tap along the roads: where the bus leaves its route → the roads → where it is back on route"; }
  function drawAdd(ll){ S.draw.pts.push([+ll.lat.toFixed(6), +ll.lng.toFixed(6)]); S.draw.line = null; drawMap(); drawBar(); clearTimeout(S.drawT); S.drawT = setTimeout(drawSnap, 300); }
  function drawSnap(){
    var d = S.draw, n = d.pts.length; if(n < 2){ drawMap(); paintDraw(); return; }
    post("/api/dv2/manual", {points:d.pts}, 30000).then(function(j){
      if(S.draw !== d || d.pts.length !== n) return;
      if(j && j.ok){ d.line = j.line; d.roads = j.roads || []; d.km = j.km || 0; d.snapped = !!j.snapped; d.note = j.note || ""; }
      else toast((j && j.error) || "Could not join the points along the roads");
      drawMap(); drawBar(); paintDraw();
    });
  }
  function drawMap(){
    G.draw.clearLayers(); var d = S.draw;
    if(d.line){ L.polyline(d.line, {pane:"div", color:"#fff", weight:11, opacity:.8, interactive:false}).addTo(G.draw);
      L.polyline(d.line, {pane:"div", color:"#c792ff", weight:7, dashArray:d.snapped ? null : "8 8"}).bindTooltip("Your route: " + esc(chain({roads:d.roads}).join(" > ") || "drawn"), {sticky:true}).addTo(G.draw); }
    else if(d.pts.length > 1) L.polyline(d.pts, {pane:"div", color:"#c792ff", weight:4, dashArray:"6 8", interactive:false}).addTo(G.draw);
    if(d.on) d.pts.forEach(function(p, i){ L.marker(p, {pane:"marks", interactive:false, keyboard:false, icon:icon(String(i + 1), "dp-dpt")}).addTo(G.draw); });
  }
  $("dUndo").onclick = function(){ S.draw.pts.pop(); S.draw.line = null; drawMap(); drawBar(); clearTimeout(S.drawT); S.drawT = setTimeout(drawSnap, 200); };
  $("dClear").onclick = function(){ S.draw.pts = []; S.draw.line = null; drawMap(); drawBar(); paintDraw(); };
  $("dDone").onclick = function(){
    if(S.draw.pts.length < 2){ toast("Tap at least 2 points along the roads"); return; }
    S.draw.on = false; drawOff(); if(!S.draw.line) drawSnap(); drawMap(); paintDraw();
    var c = $("cDraw"); if(c && c.scrollIntoView) c.scrollIntoView({block:"nearest", behavior:"smooth"});
  };
  $("dCancel").onclick = function(){ var had = !!S.draw.line; S.draw.on = false; drawOff(); if(!had) drawReset(); else { drawMap(); paintDraw(); } };
  function paintDraw(){
    var el = $("drawBody"); if(!el) return;
    $("cDraw").hidden = !S.services.length;
    var d = S.draw;
    if(d.on){ el.innerHTML = '<p class="dp-muted" style="margin:0">Drawing on the map — ' + d.pts.length + ' point' + (d.pts.length === 1 ? '' : 's') + (d.line ? ' · ' + f1(d.km) + ' km' : '') + '. Press DONE on the map when finished.</p>'; return; }
    if(!d.line){ el.innerHTML = '<p class="dp-muted" style="margin:0 0 8px">Not happy with the suggestion? Tap your route on the map: the planner joins the taps along the roads and checks it for every service.</p><button type="button" class="ds-btn sm" id="dStart">✏ DRAW ON MAP</button>';
      $("dStart").onclick = drawStart; return; }
    var sel = S.services.filter(function(x){ return keyOf(x) === S.sel; })[0];
    var h = '<div class="dp-sum"><div class="rt">' + esc(routeText({roads:d.roads})) + '</div><div class="meta">' + f1(d.km) + ' km' + (d.snapped ? '' : ' · not matched to roads') + (d.note ? ' · ' + esc(d.note) : '') + '</div></div>'
      + '<label class="dp-op"><span>Use for</span><select id="dFor" aria-label="Use the drawn route for"><option value="all">All affected services</option>' + (sel ? '<option value="sel"' + (S.drawFor === "sel" ? ' selected' : '') + '>Service ' + esc(sel.service) + ' only</option>' : '') + '</select></label>'
      + '<div class="dp-acts"><button type="button" class="ds-btn sm pri" id="dUse">CHECK &amp; USE</button><button type="button" class="ds-btn sm" id="dEdit">EDIT</button><button type="button" class="ds-btn sm ghost" id="dRemove">REMOVE</button></div>';
    if(S.drawRes) h += '<ul class="dp-res2">' + Object.keys(S.drawRes).map(function(k){
        var r = S.drawRes[k], sv = k.split("|")[0], cls = {used:"ok", flag:"fl"}[r.state] || "no", ic = {used:"✓", flag:"⚠", pending:"…"}[r.state] || "✕";
        var t = {used:"uses it — every check passed", flag:"uses it as a controller decision — fails a check", pending:"checking…"}[r.state] || "not used";
        return '<li class="' + cls + '"><span class="i">' + ic + '</span><span><b>' + esc(sv) + '</b> ' + t + '</span>' + (r.reason ? '<span class="r">' + esc(r.reason) + '</span>' : '') + '</li>'; }).join("") + '</ul>';
    el.innerHTML = h;
    $("dFor").onchange = function(){ S.drawFor = this.value; };
    $("dUse").onclick = applyDrawn;
    $("dEdit").onclick = function(){ drawStart(); };
    $("dRemove").onclick = function(){ undoReplan("manual"); drawReset(); paintServices(); showSelected(); };
  }
  function applyDrawn(){
    var d = S.draw; if(!d.line) return;
    var dn = [{service:"OCC", label:"drawn by OCC", prefer:true, line:d.line, leave_pt:d.line[0], rejoin_pt:d.line[d.line.length - 1], roads:d.roads}];
    var only = S.drawFor === "sel" && S.sel ? S.sel : null;
    var items = visibleServices().filter(function(x){ var k = keyOf(x), p = S.plans[k]; return (!only || k === only) && p && p.ok; }).map(function(x){
      var k = keyOf(x); if(!S.base[k]) S.base[k] = S.plans[k]; return {k:k, x:x, donors:dn}; });
    if(!items.length){ toast("No analysed service to use it for yet"); return; }
    S.drawRes = {}; items.forEach(function(o){ S.drawRes[o.k] = {state:"pending"}; delete S.forced[o.k]; }); paintDraw();
    replan(S.run, items, "manual", function(o, p){
      if(!o){ paintDraw(); toast("Drawn route checked for " + items.length + " service" + (items.length === 1 ? "" : "s")); return; }
      var sh = (p.shared || []).filter(function(s){ return s.from === "OCC"; })[0] || {};
      if(p.best && p.best.preferred){ S.drawRes[o.k] = {state:"used"}; }
      else if(sh.geom){ S.forced[o.k] = {src:"manual", label:"drawn by OCC", roads:d.roads, line:d.line, geom:sh.geom, reason:sh.reason}; S.drawRes[o.k] = {state:"flag", reason:sh.reason}; }
      else S.drawRes[o.k] = {state:"no", reason:sh.reason || "not on this service's route"};
      paintDraw();
    });
  }
  function undoReplan(mark){                       // back to the plans from before the controller's route
    Object.keys(S.base).forEach(function(k){
      if(S.share[k] !== mark) return;
      S.plans[k] = S.base[k]; delete S.base[k]; S.share[k] = "done"; delete S.donors[k]; delete S.ai[k]; delete S.pick[k];
    });
    var src = {manual:"manual", follow:"case"}[mark] || mark;
    Object.keys(S.forced).forEach(function(k){ if(S.forced[k].src === src) delete S.forced[k]; });
  }

  /* ---------------------------------------------------------------- saved cases: offered again, followed, opened */
  function findCases(run){
    post("/api/dv2/cases/near", payload(), 20000).then(function(j){ if(run !== S.run) return; S.cases = (j && j.cases) || []; S.caseI = 0; paintCase(); });
  }
  function followOf(c){
    var lab = "the " + c.date + " route";
    return {id:c.id, date:c.date, label:lab, donors:(c.routes || []).filter(function(r){ return r.line && r.line.length > 1; }).slice(0, 4).map(function(r){
      return {service:"CASE", label:lab, prefer:true, "for":r.services || [], line:r.line, leave_pt:r.leave_pt || r.line[0], rejoin_pt:r.rejoin_pt || r.line[r.line.length - 1], roads:r.roads || []}; })};
  }
  function followPass(run){
    if(run !== S.run || !S.follow) return;
    var vis = visibleServices();
    if(vis.some(function(x){ var p = S.plans[keyOf(x)]; return !p || p.pending; })) return;
    S.followRes = S.followRes || {};
    var items = vis.filter(function(x){ var k = keyOf(x), p = S.plans[k]; return p && p.ok && ["follow", "pending", "queued"].indexOf(S.share[k]) < 0; }).map(function(x){
      var k = keyOf(x); if(!S.base[k]) S.base[k] = S.plans[k]; S.followRes[k] = {state:"pending"}; return {k:k, x:x, donors:S.follow.donors}; });
    paintCase();
    if(items.length) replan(run, items, "follow", function(o, p){
      if(o){
        var sh = (p.shared || []).filter(function(s){ return s.from === "CASE"; }), g = sh.filter(function(s){ return s.geom; })[0];
        S.followRes[o.k] = p.best && p.best.preferred ? {state:"used"} : g ? {state:"flag", reason:g.reason, sh:g} : {state:"no", reason:(sh[0] || {}).reason || "not on this service's route"};
      }
      paintCase();
    });
  }
  function paintCase(){
    var el = $("caseBox"); if(!el) return;
    if(S.follow){
      el.hidden = false;
      var rs = S.followRes || {};
      el.innerHTML = '📁 <b>Following ' + esc(S.follow.label) + '</b>'
        + '<ul class="dp-res2">' + Object.keys(rs).map(function(k){
          var r = rs[k], sv = k.split("|")[0], forced = S.forced[k] && S.forced[k].src === "case";
          var cls = r.state === "used" || forced ? "ok" : r.state === "flag" ? "fl" : "no", ic = r.state === "used" || forced ? "✓" : r.state === "flag" ? "⚠" : r.state === "pending" ? "…" : "✕";
          var t = r.state === "used" ? "same route — every check passed" : forced ? "same route — controller decision (fails a check)" : r.state === "flag" ? "not used: fails a check" : r.state === "pending" ? "checking…" : "not used";
          return '<li class="' + cls + '"><span class="i">' + ic + '</span><span><b>' + esc(sv) + '</b> ' + t + (r.state === "flag" && !forced ? ' <button type="button" class="dp-link" data-any="' + esc(k) + '">USE ANYWAY</button>' : '') + '</span>'
            + (r.reason && r.state !== "used" ? '<span class="r">' + esc(r.reason) + '</span>' : '') + '</li>'; }).join("") + '</ul>'
        + '<div class="dp-acts"><button type="button" class="ds-btn sm ghost" id="cfStop">STOP FOLLOWING</button></div>';
      el.querySelectorAll("[data-any]").forEach(function(b){ b.onclick = function(){
        var k = b.dataset.any, r = S.followRes[k], dn = S.follow.donors.filter(function(x){ return x.roads.join("|") === (r.sh.roads || []).join("|"); })[0] || S.follow.donors[0];
        S.forced[k] = {src:"case", label:S.follow.label, roads:dn.roads, line:dn.line, geom:r.sh.geom, reason:r.reason}; paintCase(); paintServices(); if(S.sel === k) showSelected(); paintMsg(); }; });
      $("cfStop").onclick = function(){ undoReplan("follow"); S.follow = null; S.followRes = null; paintCase(); paintServices(); showSelected(); paintMsg(); };
      return;
    }
    var c = S.cases[S.caseI];
    if(!c || S.caseOff){ el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = '📁 <b>Diverted here before</b> — ' + esc(c.date) + ' ' + esc(c.time) + (c.services.length ? ' · svc ' + esc(uniqA(c.services).join(", ")) : '') + (c.manual ? ' · drawn by OCC' : '') + (c.followed ? ' · followed ' + c.followed + '×' : '')
      + (c.routes || []).map(function(r){ return '<span class="rt">' + ((c.routes || []).length > 1 ? 'Route ' + r.n + ': ' : '') + esc(r.text || routeText({roads:r.roads})) + '</span>'; }).join("")
      + (c.note ? '<span class="dp-muted">' + esc(c.note) + '</span>' : '')
      + '<div style="margin-top:6px">Do you want to follow this diverted route?</div><div class="dp-acts"><button type="button" class="ds-btn sm pri" id="cfFollow">FOLLOW</button><button type="button" class="ds-btn sm ghost" id="cfNo">NOT NOW</button>'
      + (S.cases.length > 1 ? '<button type="button" class="ds-btn sm ghost" id="cfNext">' + (S.caseI + 1) + ' of ' + S.cases.length + ' ▸</button>' : '') + '</div>';
    $("cfFollow").onclick = function(){ S.follow = followOf(c); S.followRes = {}; paintCase(); followPass(S.run); };
    $("cfNo").onclick = function(){ S.caseOff = true; paintCase(); };
    var nx = $("cfNext"); if(nx) nx.onclick = function(){ S.caseI = (S.caseI + 1) % S.cases.length; paintCase(); };
  }
  $("cList").onclick = function(){ openCases(); };
  $("cClose").onclick = function(){ $("cModal").hidden = true; $("cList").focus(); };
  function openCases(){
    $("cModal").hidden = false; $("cRows").innerHTML = '<p class="dp-muted"><span class="dp-spin"></span>Loading…</p>';
    get("/api/dv2/cases?limit=60").then(function(j){
      var cs = (j && j.cases) || [];
      $("cRows").innerHTML = cs.length ? cs.map(function(c){
        return '<div class="dp-crow"><b>' + esc(c.date) + ' ' + esc(c.time) + '</b> · ' + esc(c.road) + (c.services.length ? ' · svc ' + esc(uniqA(c.services).join(", ")) : '') + ' · by ' + esc(c.by)
          + (c.manual ? ' · drawn by OCC' : '') + (c.followed ? ' · followed ' + c.followed + '×' : '')
          + (c.routes || []).map(function(r){ return '<span class="rt">' + ((c.routes || []).length > 1 ? 'Route ' + r.n + ': ' : '') + esc(r.text) + '</span>'; }).join("")
          + (c.note ? '<span class="dp-muted">' + esc(c.note) + '</span>' : '')
          + '<div class="dp-acts"><button type="button" class="ds-btn sm" data-open="' + c.id + '">OPEN</button><button type="button" class="ds-btn sm ghost" data-del="' + c.id + '">DELETE</button></div></div>'; }).join("")
        : '<p class="dp-empty">No saved cases yet. Step 7 saves one.</p>';
      $("cRows").querySelectorAll("[data-open]").forEach(function(b){ b.onclick = function(){ openCase(+b.dataset.open); }; });
      $("cRows").querySelectorAll("[data-del]").forEach(function(b){ b.onclick = function(){ if(!confirm("Delete this saved case for everyone?")) return; post("/api/dv2/cases/" + b.dataset.del + "/delete", {}).then(openCases); }; });
    });
  }
  function openCase(id){
    get("/api/dv2/cases/" + id).then(function(j){
      var c = j && j.case; if(!c){ toast((j && j.error) || "Case not found"); return; }
      if(!c.page || !c.page.length){ toast("This case has no blockage to show"); return; }
      S.blocks = c.page.filter(function(b){ return b.cor && b.cor.length > 1; }).map(function(b){
        return {cor:b.cor, cm:b.cm && b.cm.length === b.cor.length ? b.cm : cumM(b.cor), a:b.a, b:b.b, road:b.road, labels:b.labels || {}, dir:b.dir || "both", locked:true}; });
      if(!S.blocks.length){ toast("This case has no blockage to show"); return; }
      S.bi = 0; S.adding = false; $("cModal").hidden = true;
      paintBlockCard(); drawBlocks(true); S.follow = followOf(c); S.followRes = {}; runAffected(); toast("Case opened — every service is checked on its route");
    });
  }

  /* ---------------------------------------------------------------- 7. save the case (shared by everyone) */
  $("sSave").onclick = function(){
    var gs = groups(); if(!gs.list.length){ toast("Nothing to save yet"); return; }
    var rt = gs.list.map(function(g){ var s_ = g.srcs.indexOf("manual") >= 0 ? "manual" : g.srcs.indexOf("case") >= 0 ? "case" : g.srcs[0];
      return {n:g.n, services:g.svcs, roads:chainRaw(g.f.c || g.f), text:routeText(g.f.c || g.f), line:g.f.line, leave_pt:g.f.leave_pt, rejoin_pt:g.f.rejoin_pt,
              skipped:g.skipped.map(function(s){ return {code:s.code, name:s.name}; }), km:g.km, runs:S.runs[g.key] || "", mileage:milOf(g), source:s_, flags:g.flags}; });
    var sv = []; gs.list.forEach(function(g){ g.items.forEach(function(it){ sv.push({service:it.x.service, direction:it.x.direction, run:it.x.run || 0, route:g.n, leave:(it.f.leave || {}).code, rejoin:(it.f.rejoin || {}).code, source:it.f.src}); }); });
    var body = {blocks:payload().blocks, page:S.blocks.map(function(b){ return {cor:b.cor, cm:b.cm, a:b.a, b:b.b, road:b.road, labels:b.labels, dir:b.dir}; }),
                routes:rt, services:sv, text:$("mText").textContent, note:$("sNote").value, from_case:S.follow ? S.follow.id : null};
    $("sSave").disabled = true;
    post("/api/dv2/cases", body).then(function(j){
      $("sSave").disabled = false;
      if(j && j.ok){ S.saved = j.case.id; $("sMsg").textContent = "Saved as case #" + j.case.id + " · " + j.case.date + " " + j.case.time; toast("Saved — next time this road is blocked, the planner offers this route"); }
      else toast((j && j.error) || "Could not save the case");
    });
  };

  hint("Tap the road where it is blocked");
  paintRec();
})();
