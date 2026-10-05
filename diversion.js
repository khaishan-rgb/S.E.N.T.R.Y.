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
  ["road", "block", "orig", "skip", "alt", "div", "stops", "marks", "find"].forEach(function(k){ G[k] = L.layerGroup().addTo(map); });
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
  function pick(r){
    if(!r) return; closeList(); $("q").value = r.label; G.find.clearLayers(); G.road.clearLayers();
    if(r.type === "road"){
      get("/api/dv2/roadgeom?name=" + encodeURIComponent(r.road)).then(function(j){
        (j.segments || []).forEach(function(s){ L.polyline([[s[0], s[1]], [s[2], s[3]]], {pane:"road", color:"#38d6ff", weight:7, opacity:.45, interactive:false}).addTo(G.road); });
      });
      if(r.bbox) map.fitBounds([[r.bbox[0], r.bbox[1]], [r.bbox[2], r.bbox[3]]], {padding:[30, 30], maxZoom:17});
      hint("Tap the blocked part of " + r.label + " on the map");
    }else{
      map.setView([r.lat, r.lon], r.type === "stop" ? 18 : 17);
      L.marker([r.lat, r.lon], {pane:"marks", icon:icon(r.type === "stop" ? "BS" : "\u2022", "dp-lbl")}).addTo(G.find);
      hint("Tap the blocked road near " + r.label);
    }
  }
  function hint(t){ $("mapHint").textContent = t; $("mapHint").hidden = !t; }

  /* ---------------------------------------------------------------- 2. blockage (a road SEGMENT with adjustable ends) */
  map.on("click", function(e){ placeBlock(e.latlng); });
  function placeBlock(ll){
    hint("Finding the road\u2026");
    get("/api/dv2/block?lat=" + ll.lat.toFixed(6) + "&lon=" + ll.lng.toFixed(6)).then(function(j){
      if(!j.ok){ hint(""); toast(j.error || "No road there \u2014 tap directly on the road."); return; }
      S.block = {cor:j.corridor, cm:j.corridor_m, a:j.start_m, b:j.end_m, road:tc(j.road), labels:j.dir_labels || {}};
      S.dir = null; resetServices(); G.road.clearLayers(); G.find.clearLayers();
      paintBlockCard(); drawBlock(true); hint("Choose the blocked direction (step 3)");
    });
  }
  function blockLine(){ return cutLine(S.block.cor, S.block.cm, S.block.a, S.block.b); }
  var hA = null, hB = null, bLineL = null, bCase = null, bX = null;
  function drawBlock(fit){
    G.block.clearLayers(); ARR = ARR.filter(function(s){ return s.grp !== G.block; });
    if(!S.block) return;
    var bl = blockLine();
    bCase = L.polyline(bl, {pane:"block", color:"#fff", weight:13, opacity:.9, interactive:false}).addTo(G.block);
    bLineL = L.polyline(bl, {pane:"block", color:COL.block, weight:9, opacity:1}).addTo(G.block).bindTooltip(esc(S.block.road) + " \u2014 blocked", {sticky:true});
    if(S.dir === "fwd" || S.dir === "both") arrows(G.block, bl, "#fff", "arrows", 60);
    if(S.dir === "rev" || S.dir === "both") arrows(G.block, bl.slice().reverse(), "#fff", "arrows", S.dir === "both" ? 85 : 60);
    var mid = pointAt(S.block.cor, S.block.cm, (S.block.a + S.block.b) / 2);
    bX = L.marker(mid, {pane:"marks", icon:icon("\u2715", "dp-x"), keyboard:false}).addTo(G.block).bindTooltip("BLOCKED", {direction:"top", permanent:false});
    hA = handle(S.block.a, function(s){ S.block.a = Math.min(s, S.block.b - 30); });
    hB = handle(S.block.b, function(s){ S.block.b = Math.max(s, S.block.a + 30); });
    if(fit) map.fitBounds(L.latLngBounds(bl).pad(1.2), {maxZoom:18});
  }
  function handle(s, set){
    var m = L.marker(pointAt(S.block.cor, S.block.cm, s), {draggable:true, pane:"marks", autoPan:true, icon:icon("", "dp-handle"), keyboard:true, title:"Drag along the road to set the end of the blocked section"}).addTo(G.block);
    m.on("drag", function(e){
      var p = e.target.getLatLng(), n = nearestOn([p.lat, p.lng], S.block.cor, S.block.cm); set(n.s);
      var bl = blockLine(); bLineL.setLatLngs(bl); bCase.setLatLngs(bl); bX.setLatLng(pointAt(S.block.cor, S.block.cm, (S.block.a + S.block.b) / 2)); paintLen();
    });
    m.on("dragend", function(){ drawBlock(false); if(S.dir) runAffected(); });
    return m;
  }
  function paintLen(){ $("bLen").textContent = km(S.block.b - S.block.a) + " blocked \u00b7 drag the handles to adjust"; }
  function paintBlockCard(){
    $("cBlock").hidden = !S.block; if(!S.block) return;
    $("bRoad").textContent = S.block.road; paintLen();
    var lb = S.block.labels;
    $("lblFwd").textContent = lb.fwd || "This direction"; $("lblRev").textContent = lb.rev || "Opposite direction";
    $("arrFwd").style.transform = "rotate(" + ((lb.fwd_bearing || 90) - 90) + "deg)"; $("arrRev").style.transform = "rotate(" + ((lb.rev_bearing || 270) - 90) + "deg)";
    document.querySelectorAll("#bDir button").forEach(function(b){ b.setAttribute("aria-checked", String(b.dataset.d === S.dir)); });
    $("bAsk").hidden = !!S.dir;
  }
  /* ---------------------------------------------------------------- 3. direction */
  document.querySelectorAll("#bDir button").forEach(function(b){ b.onclick = function(){ S.dir = b.dataset.d; paintBlockCard(); drawBlock(false); hint(""); runAffected(); }; });
  $("bZoom").onclick = function(){ if(S.block) map.fitBounds(L.latLngBounds(blockLine()).pad(1.2), {maxZoom:18}); };
  $("bClear").onclick = function(){ S.block = null; S.dir = null; resetServices(); G.block.clearLayers(); paintBlockCard(); hint("Tap the road where it is blocked"); };

  /* ---------------------------------------------------------------- 4. affected services, each analysed automatically */
  function resetServices(){ S.run++; S.services = []; S.plans = {}; S.sel = null; S.view = null; $("cSvc").hidden = true; clearServiceMap(); paintRec(); }
  function keyOf(x){ return x.service + "|" + x.direction + "|" + (x.run || 0); }
  function payload(){ return {line:blockLine(), dir:S.dir, road:S.block.road}; }
  function runAffected(){
    var run = ++S.run; S.services = []; S.plans = {}; S.sel = null; S.view = null; clearServiceMap(); paintRec();
    $("cSvc").hidden = false; $("svcList").innerHTML = '<p class="dp-muted"><span class="dp-spin"></span>Finding services whose route runs along the blocked section in the blocked direction\u2026</p>';
    post("/api/dv2/affected", payload()).then(function(j){
      if(run !== S.run) return;
      if(j.error){ $("svcList").innerHTML = '<p class="dp-muted">' + esc(j.error) + '</p>'; return; }
      S.services = j.services || [];
      $("svcCount").textContent = S.services.length ? S.services.length + " service-direction" + (S.services.length === 1 ? "" : "s") : "";
      $("svcNote").textContent = S.services.length ? "Each service-direction is analysed on its own. Tap one to see its diversion." : "";
      if(!S.services.length){ $("svcList").innerHTML = '<p class="dp-empty">No bus service runs along this section in the blocked direction' + (S.dir !== "both" ? " (services on the other carriageway are not affected)" : "") + '.</p>'; return; }
      paintServices(); analyseAll(run);
    });
  }
  function analyseAll(run){
    var queue = S.services.slice(), busy = 0;
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
    }
    next();
  }
  function stTxt(p){
    if(!p) return ['<span class="dp-st wait">QUEUED</span>', ""];
    if(p.pending) return ['<span class="dp-st run"><span class="dp-spin"></span>ANALYSING</span>', "Searching diversion start \u00d7 rejoin stop\u2026"];
    if(p.ok === false || p.error) return ['<span class="dp-st no">ERROR</span>', esc(p.error || "")];
    var b = p.best;
    if(p.status === "divert") return ['<span class="dp-st ok">DIVERT \u00b7 ' + esc(b.confidence) + '</span>', esc(b.leave.code) + " \u2192 " + esc(b.rejoin.code) + " \u00b7 skips " + b.skipped_n + " \u00b7 " + b.turns + " turns"];
    if(p.status === "review") return ['<span class="dp-st rev">REVIEW</span>', "Low-confidence route only"];
    return ['<span class="dp-st no">NO VERIFIED ROUTE</span>', "Controller review required"];
  }
  function paintServices(){
    $("svcList").innerHTML = S.services.map(function(x){ var k = keyOf(x), t = stTxt(S.plans[k]);
      return '<button type="button" class="dp-svc" role="listitem" data-k="' + esc(k) + '" aria-pressed="' + (S.sel === k) + '"><span class="no">' + esc(x.service) + '</span><span class="to">D' + x.direction + ' \u00b7 to ' + esc(x.destination || "") + (x.operator ? ' \u00b7 ' + esc(x.operator) : '') + '</span>' + t[0] + '<span class="rs">' + t[1] + '</span></button>'; }).join("");
    $("svcList").querySelectorAll(".dp-svc").forEach(function(b){ b.onclick = function(){ selectService(b.dataset.k); }; });
  }
  function selectService(k){ S.sel = k; S.view = null; paintServices(); showSelected(); }
  function showSelected(){ var p = S.plans[S.sel]; paintRec(); drawService(p); }

  /* ---------------------------------------------------------------- 5. the diversion on the map */
  function clearServiceMap(){ ["orig", "skip", "alt", "div", "stops", "marks"].forEach(function(k){ G[k].clearLayers(); }); ARR = ARR.filter(function(s){ return s.grp === G.block; }); }
  function candOf(p){ if(!p || !p.candidates) return null; return S.view ? (p.candidates.filter(function(c){ return c.id === S.view; })[0] || p.best) : p.best; }
  function drawService(p){
    clearServiceMap(); if(!p || p.pending || !p.ok) return;
    var c = candOf(p), b = p.best, bounds = L.latLngBounds(blockLine());
    rline(p.original_line, COL.orig, {pane:"orig", weight:5, casing:.6}, G.orig); arrows(G.orig, p.original_line, COL.orig, "arrows", 140);
    if(c){
      if(c !== b && b) rline(b.line, COL.div, {pane:"alt", weight:4, opacity:.45, casing:.3}, G.alt);
      L.polyline(c.skip_line, {pane:"skip", color:"#fff", weight:11, opacity:.7, interactive:false}).addTo(G.skip);
      L.polyline(c.skip_line, {pane:"skip", color:COL.skip, weight:7, dashArray:"8 8"}).bindTooltip("Original section skipped", {sticky:true}).addTo(G.skip);
      var main = c === b ? COL.div : COL.alt;
      rline(c.line, main, {pane:"div", weight:7, cls:"dp-anim"}, G.div).bindTooltip((c === b ? "Recommended: " : "Alternative: ") + c.roads.join(" \u2192 "), {sticky:true});
      arrows(G.div, c.line, "#fff", "arrows", 90);
      bounds.extend(c.line); bounds.extend(c.skip_line);
      L.marker(c.leave_pt, {pane:"marks", icon:icon("DIVERSION STARTS", "dp-lbl"), keyboard:false}).addTo(G.marks);
      L.marker(c.rejoin_pt, {pane:"marks", icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="dp-lbl" style="--c:' + COL.rejoin + '">\u21aa REJOIN</div>'}), keyboard:false}).addTo(G.marks);
    }
    var skipped = {}, inb = {};
    if(c) c.skipped.forEach(function(s){ skipped[s.code] = true; if(s.in_block) inb[s.code] = true; });
    (p.stops || []).forEach(function(s){
      var role = c && s.code === c.rejoin.code ? "rejoin" : skipped[s.code] ? (inb[s.code] || s.in_block ? "inblock" : "skip") : (s.in_block ? "inblock" : "served");
      var col = {rejoin:COL.rejoin, skip:COL.skip, inblock:COL.inblock, served:COL.served}[role];
      var lab = s.code + " " + s.name + " \u2014 " + {rejoin:"rejoin stop", skip:"skipped", inblock:"skipped (inside the blockage)", served:"served"}[role];
      var big = role === "rejoin" || (c && s.code === c.leave.code), cls = "dp-stop" + (big ? " big" : "") + (role === "skip" || role === "inblock" ? " x" : "");
      L.marker([s.lat, s.lon], {pane:"marks", keyboard:false, icon:L.divIcon({className:"", iconSize:[0, 0], html:'<div class="' + cls + '" style="--c:' + col + '"></div>'})})
        .bindTooltip(esc(lab)).addTo(G.stops);
    });
    map.fitBounds(bounds.pad(.12), {maxZoom:17});
  }

  /* ---------------------------------------------------------------- 5. the recommendation panel */
  function pill(t, c){ return '<span class="dp-pill" style="--c:' + c + '">' + esc(t) + '</span>'; }
  var CONF = {HIGH:"#2ee59d", MEDIUM:"#ffb547", LOW:"#8a97a8", "VERY HIGH":"#2ee59d"};
  function paintRec(){
    var el = $("recBody"), p = S.sel ? S.plans[S.sel] : null, x = S.services.filter(function(s){ return keyOf(s) === S.sel; })[0];
    if(!S.block){ el.innerHTML = '<p class="dp-empty">Search a road, tap the blocked segment and choose the blocked direction. Every affected service is then analysed automatically and the easiest practical diversion appears here.</p>'; return; }
    if(!S.dir){ el.innerHTML = '<p class="dp-empty">Choose the blocked direction to find the affected services.</p>'; return; }
    if(!x){ el.innerHTML = '<p class="dp-empty">Select an affected service.</p>'; return; }
    var head = '<div class="dp-rec-h"><span class="no">' + esc(x.service) + '</span><div class="meta"><b>Service ' + esc(x.service) + ' \u00b7 Direction ' + x.direction + '</b>to ' + esc(x.destination || "") + (p && p.bus_type ? ' \u00b7 ' + (p.bus_type === "DD" ? "double-deck" : "single-deck") : '') + '</div></div>';
    if(!p || p.pending){ el.innerHTML = head + '<p class="dp-muted"><span class="dp-spin"></span>Analysing: how far the bus can keep its normal route, where it must leave it, and the nearest stop it can rejoin correctly\u2026</p>'; return; }
    if(!p.ok){ el.innerHTML = head + '<div class="dp-banner no"><b>COULD NOT ANALYSE</b>' + esc(p.error || "") + '</div>'; return; }
    var c = candOf(p), b = p.best, h = head;
    if(p.status === "none" || !b){
      h += '<div class="dp-banner no"><b>\u26a0 NO VERIFIED DIVERSION FOUND</b>Controller review required. A correct \u201cno route\u201d is better than a wrong diversion.</div>'
        + (p.failures && p.failures.length ? '<div class="dp-k">Why</div><ul class="dp-why dp-fail">' + p.failures.map(function(f){ return '<li>' + esc(f) + '</li>'; }).join("") + '</ul>' : '')
        + attemptsHtml(p);
      el.innerHTML = h; return;
    }
    h += p.status === "divert" ? '<div class="dp-banner ok"><b>RECOMMENDED DIVERSION</b>Easiest practical diversion that keeps the most useful part of the route.</div>'
      : '<div class="dp-banner rev"><b>CONTROLLER REVIEW REQUIRED</b>Only a low-confidence route was found \u2014 not recommended automatically.</div>';
    if(c !== b) h += '<div class="dp-banner rev"><b>SHOWING AN ALTERNATIVE</b><button type="button" class="ds-btn sm" id="backRec" style="margin-top:6px">BACK TO RECOMMENDED</button></div>';
    var lr = p.last_reachable || {}, earlier = c.earlier;
    h += '<dl class="dp-kv">'
      + (earlier ? '<dt>Last reachable stop</dt><dd>' + esc(lr.code) + ' ' + esc(lr.name) + ' <span style="color:#ffd391">\u2014 no practical escape after it</span></dd>' : '')
      + '<dt>Last normal stop served</dt><dd><b>' + esc(c.leave.code) + '</b> ' + esc(c.leave.name) + '</dd>'
      + '<dt>Diversion begins</dt><dd>after ' + esc(c.leave.name) + '</dd></dl>'
      + '<div class="dp-k">Recommended roads</div><ul class="dp-chain">' + c.roads.map(function(r, i){ return (i ? '<li class="ar">\u2192</li>' : '') + '<li>' + esc(r) + '</li>'; }).join("") + '<li class="ar">\u2192</li><li class="rj">\u21aa ' + esc(c.rejoin.name) + '</li></ul>'
      + '<div class="dp-k">Bus captain instructions</div><ul class="dp-chain">' + c.instruction_text.map(function(t, i){ return (i ? '<li class="ar">\u2192</li>' : '') + '<li style="background:rgba(56,214,255,.1);border-color:rgba(56,214,255,.4);color:#dff6ff">' + esc(tc(t)) + '</li>'; }).join("") + '</ul>'
      + '<dl class="dp-kv">'
      + '<dt>Rejoin</dt><dd><b>' + esc(c.rejoin.code) + '</b> ' + esc(c.rejoin.name) + '</dd>'
      + '<dt>Next original stop verified</dt><dd>' + (c.next.ok ? '\u2713 ' : '') + esc(c.next.code || "") + ' ' + esc(c.next.name || "") + '</dd>'
      + '<dt>Stops skipped</dt><dd><b>' + c.skipped_n + '</b>' + (c.skipped.length ? ' \u00b7 ' + c.skipped.map(function(s){ return esc(s.code) + (s.in_block ? "*" : ""); }).join(", ") : '') + (c.unavoidable ? ' <span class="dp-muted">(* inside the blockage)</span>' : '') + '</dd>'
      + '<dt>Extra distance</dt><dd>' + sgn(c.added_km, 1) + ' km</dd>'
      + '<dt>Extra time</dt><dd>' + sgn(c.added_min) + ' min</dd>'
      + '<dt>Turns</dt><dd>' + c.turns + (c.sharp ? ' (' + c.sharp + ' sharp)' : '') + '</dd>'
      + '<dt>Bus-road confidence</dt><dd>' + pill(c.busroad_label, CONF[c.busroad_label] || "#8a97a8") + ' <span class="dp-muted">' + esc(c.evidence_text) + '</span></dd>'
      + '<dt>Overall confidence</dt><dd>' + pill(c.confidence, CONF[c.confidence]) + ' <span class="dp-muted">OCC score ' + f0(c.score) + '/100</span></dd></dl>';
    if(c === b && p.why && p.why.length) h += '<div class="dp-k">Why this route?</div><ul class="dp-why">' + p.why.map(function(t){ return '<li>' + esc(t) + '</li>'; }).join("") + '</ul>';
    if(c === b && p.why_not && p.why_not.length) h += '<div class="dp-k">Why not\u2026</div>' + p.why_not.map(function(w){ return '<div class="dp-not"><b>' + esc(tc(w.title)) + '</b>' + esc(w.text) + '</div>'; }).join("");
    var alts = p.candidates || [];
    if(alts.length > 1) h += '<details class="dp-det"' + (S.view ? ' open' : '') + '><summary>Show alternatives (' + (alts.length - 1) + ')</summary><div style="margin-top:6px">' + alts.map(function(a){
        var tg = (a.tags || []).map(function(t){ return {A:"MIN SKIPPED STOPS", B:"SIMPLEST", C:"FASTEST"}[t]; }).filter(Boolean).join(" \u00b7 ");
        return '<button type="button" class="dp-alt" data-alt="' + esc(a.id) + '" aria-pressed="' + ((S.view || b.id) === a.id) + '"><b>' + esc(a.leave.code) + ' \u2192 ' + esc(a.rejoin.code) + '</b>' + (a.id === b.id ? '<span class="dp-tag" style="color:#2ee59d">RECOMMENDED</span>' : '') + (tg ? '<span class="dp-tag">' + tg + '</span>' : '')
          + '<br>' + esc(a.roads.join(" \u2192 ")) + '<br><span class="dp-muted">skips ' + a.skipped_n + ' \u00b7 ' + a.turns + ' turns \u00b7 ' + sgn(a.added_min) + ' min \u00b7 ' + sgn(a.added_km, 1) + ' km \u00b7 ' + esc(a.busroad_label) + ' bus-road \u00b7 ' + esc(a.confidence) + ' \u00b7 score ' + f0(a.score) + '</span></button>'; }).join("") + '</div></details>';
    h += '<details class="dp-det"><summary>OCC score ' + f0(c.score) + '/100 \u2014 how it is made</summary><div class="dp-muted" style="margin-top:6px">' + Object.keys(c.parts || {}).map(function(k){ return esc((S.cfg && S.cfg.weight_labels || {})[k] || k) + ' ' + f1(c.parts[k]); }).join(" \u00b7 ") + '. Scored only after every hard check passed (blocked road, U-turn, wrong-way, impossible turn, restricted road, rejoin direction, next original stop).</div></details>';
    h += attemptsHtml(p);
    el.innerHTML = h;
    var br = $("backRec"); if(br) br.onclick = function(){ S.view = null; showSelected(); };
    el.querySelectorAll("[data-alt]").forEach(function(a){ a.onclick = function(){ S.view = a.dataset.alt === b.id ? null : a.dataset.alt; showSelected(); }; });
  }
  function attemptsHtml(p){
    var at = p.attempts || []; if(!at.length) return "";
    return '<details class="dp-det"><summary>Route search \u2014 ' + at.length + ' diversion start \u00d7 rejoin combinations, ' + f0((p.search || {}).routes) + ' road routes checked</summary>'
      + '<p class="dp-muted" style="margin:6px 0 0">Last reachable stop ' + esc((p.last_reachable || {}).code || "\u2013") + ' \u00b7 first stop after the blockage ' + esc((p.first_after || {}).code || "\u2013") + '. Nearest combinations first; earlier diversion starts only when they help.</p><ul class="dp-att">'
      + at.map(function(a){ var ok = a.result === "valid"; return '<li class="' + (ok ? "ok" : "no") + '"><span class="i">' + (ok ? "\u2713" : "\u2715") + '</span><span>' + (a.earlier ? '<em style="font-style:normal;color:#ffd391">earlier \u00b7 </em>' : '') + esc(a.from_code) + ' ' + esc(a.from_name) + ' \u2192 ' + esc(a.to_code) + ' ' + esc(a.to_name) + '</span><span class="r">' + esc(a.reason) + '</span></li>'; }).join("") + '</ul></details>';
  }

  /* ---------------------------------------------------------------- weights + legend */
  $("lgBtn").onclick = function(){ var on = $("legend").hidden; $("legend").hidden = !on; this.setAttribute("aria-pressed", String(on)); };
  $("wBtn").onclick = function(){
    get("/api/dv2/weights").then(function(j){
      var w = j.weights || {}, lb = j.labels || {};
      $("wRows").innerHTML = Object.keys(w).map(function(k){ return '<label class="dp-w"><span>' + esc(lb[k] || k) + '</span><input type="range" min="0" max="50" step="1" value="' + w[k] + '" data-k="' + k + '" aria-label="' + esc(lb[k] || k) + '"><b data-v="' + k + '">' + f0(w[k]) + '</b></label>'; }).join("");
      $("wRows").querySelectorAll("input").forEach(function(i){ i.oninput = function(){ $("wRows").querySelector('[data-v="' + i.dataset.k + '"]').textContent = i.value; }; });
      $("wModal").hidden = false; $("wRows").querySelector("input").focus();
    });
  };
  function closeW(){ $("wModal").hidden = true; $("wBtn").focus(); }
  $("wCancel").onclick = closeW;
  $("wModal").addEventListener("keydown", function(e){ if(e.key === "Escape") closeW(); });
  function saveW(body){ post("/api/dv2/weights", body).then(function(j){ if(j.error) return toast(j.error); closeW(); toast("Weights saved \u2014 re-analysing"); if(S.block && S.dir) runAffected(); }); }
  $("wSave").onclick = function(){ var w = {}; $("wRows").querySelectorAll("input").forEach(function(i){ w[i.dataset.k] = +i.value; }); saveW({weights:w}); };
  $("wReset").onclick = function(){ saveW({reset:true}); };

  hint("Tap the road where it is blocked");
  paintRec();
})();
