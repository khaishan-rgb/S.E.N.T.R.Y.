/* SG Transport Pulse - Diversion history (V16.27). Served at /diversion-history.js for /diversion/history.
   Every saved diversion with its period: search by date, road, service and status; open one (map, routes, the OCC message);
   use it again with a new start / end time (DUPLICATE); end a live one now; change its times; preview it on the TV; open it in
   the planner; delete it; export what the filters show to Excel. Times are Singapore time whatever the computer's clock zone. */
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };
  function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[c]; }); }
  function get(u){ return fetch(u, {credentials:"same-origin", cache:"no-store"}).then(function(r){ return r.json().catch(function(){ return {error:"HTTP " + r.status}; }); }).catch(function(){ return {error:"The server could not be reached"}; }); }
  function post(u, b){ return fetch(u, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify(b || {})})
    .then(function(r){ return r.json().catch(function(){ return {error:"HTTP " + r.status}; }); }).catch(function(){ return {error:"The server could not be reached"}; }); }
  var tT = 0;
  function toast(t){ var e = $("toast"); e.textContent = t; e.classList.add("on"); clearTimeout(tT); tT = setTimeout(function(){ e.classList.remove("on"); }, 3600); }
  function sgt(ms){ return new Date(ms + 8 * 3600e3); }
  function sgtNow(){ return sgt(Date.now()).toISOString().slice(0, 16); }
  function sgtDay(back){ return sgt(Date.now() - back * 86400e3).toISOString().slice(0, 10); }
  var MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ");
  function hm(ts){ var d = sgt(ts * 1000); return ("0" + d.getUTCHours()).slice(-2) + ":" + ("0" + d.getUTCMinutes()).slice(-2); }
  function day(ts){ var d = sgt(ts * 1000); return d.getUTCDate() + " " + MON[d.getUTCMonth()]; }
  function dur(s){ var m = Math.max(0, Math.round(s / 60)), h = Math.floor(m / 60), d = Math.floor(h / 24);
    return d >= 2 ? d + " days" : h ? h + " h" + (m % 60 ? " " + (m % 60) + " min" : "") : m + " min"; }
  function uniq(a){ return (a || []).filter(function(x, i){ return a.indexOf(x) === i; }); }
  var ST = {live:"LIVE", upcoming:"UPCOMING", ended:"ENDED", saved:"NO PERIOD"};
  function chip(st){ return '<span class="st ' + esc(st) + '">' + (ST[st] || esc(st)) + '</span>'; }
  function period(c){
    if(!c.start_ts) return {a:"Saved " + day(c.ts) + " " + hm(c.ts), b:"no start or end time"};
    var a = day(c.start_ts) + " " + hm(c.start_ts), now = Date.now() / 1000;
    if(!c.end_ts) return {a:a + " – until further notice", b:c.state === "live" ? "running " + dur(now - c.start_ts) : c.state === "upcoming" ? "starts in " + dur(c.start_ts - now) : ""};
    var b = day(c.end_ts) === day(c.start_ts) ? hm(c.end_ts) : day(c.end_ts) + " " + hm(c.end_ts);
    return {a:a + " – " + b, b:c.state === "live" ? dur(c.end_ts - now) + " left" : c.state === "upcoming" ? "starts in " + dur(c.start_ts - now) : "lasted " + dur(c.end_ts - c.start_ts)};
  }
  var S = {cases:[], sel:null, c:null, map:null, grp:null};

  /* ---------------------------------------------------------------- search */
  function qs(){
    var p = new URLSearchParams();
    [["from", "fFrom"], ["to", "fTo"], ["road", "fRoad"], ["service", "fSvc"], ["state", "fState"]].forEach(function(x){ var v = $(x[1]).value.trim(); if(v) p.set(x[0], v); });
    return p.toString();
  }
  function search(then){
    var q = qs();
    if($("fFrom").value && $("fTo").value && $("fTo").value < $("fFrom").value){ $("sum").innerHTML = '<span style="color:#ff8a96">The To date is before the From date.</span>'; return; }
    $("fXls").href = "/api/dv2/history/export" + (q ? "?" + q : "");
    try{ history.replaceState(null, "", "/diversion/history" + (q ? "?" + q : "") + (S.sel ? (q ? "&" : "?") + "case=" + S.sel : "")); }catch(e){}
    $("sum").innerHTML = "<span>Loading…</span>";
    get("/api/dv2/history?" + q + (q ? "&" : "") + "limit=500").then(function(j){
      if(j.error){ $("sum").innerHTML = '<span style="color:#ff8a96">' + esc(j.error) + '</span>'; return; }
      S.cases = j.cases || []; paintList(); if(then) then();
    });
  }
  function paintList(){
    var cs = S.cases, n = function(st){ return cs.filter(function(c){ return c.state === st; }).length; }, live = n("live"), up = n("upcoming");
    $("sum").innerHTML = '<span><b>' + cs.length + '</b> diversion' + (cs.length === 1 ? "" : "s") + (live ? ', <b style="color:#ff8a96">' + live + ' live now</b>' : "") + (up ? ', <b style="color:var(--ds-amber)">' + up + ' upcoming</b>' : "") + '</span><span class="sp"></span>'
      + '<span>Click one to open it' + (cs.length >= 500 ? " (first 500 shown: narrow the dates)" : "") + '</span>';
    $("none").hidden = !!cs.length;
    $("none").innerHTML = cs.length ? "" : (qs() ? "No saved diversion matches these filters. Widen the dates or clear Road / Service." : "No saved diversion yet. The planner's step 7 saves one with its start and end time.");
    $("rows").innerHTML = cs.map(function(c){
      var p = period(c), r0 = (c.routes || [])[0] || {};
      return '<tr tabindex="0" data-id="' + c.id + '" aria-selected="' + (S.sel === c.id) + '"><td>' + chip(c.state) + '</td><td class="per">' + esc(p.a) + (p.b ? '<small>' + esc(p.b) + '</small>' : '') + '</td>'
        + '<td class="rd"><b>' + esc(c.blocked || c.road) + '</b>' + esc(c.dir_txt || "") + '</td><td>' + uniq(c.services).map(function(s){ return '<span class="sv">' + esc(s) + '</span>'; }).join("") + '</td>'
        + '<td class="rt">' + esc(r0.text || "") + ((c.routes || []).length > 1 ? ' <span style="color:var(--ds-mut)">(and ' + (c.routes.length - 1) + ' more route' + (c.routes.length > 2 ? "s" : "") + ')</span>' : '') + '</td><td class="by">' + esc(c.by) + '<br><span style="color:var(--ds-mut)">#' + c.id + '</span></td></tr>';
    }).join("");
    [].forEach.call($("rows").querySelectorAll("tr"), function(tr){
      tr.onclick = function(){ select(+tr.dataset.id); };
      tr.onkeydown = function(e){ if(e.key === "Enter" || e.key === " "){ e.preventDefault(); select(+tr.dataset.id); } };
    });
  }

  /* ---------------------------------------------------------------- one record */
  function select(id){
    S.sel = id;
    [].forEach.call($("rows").querySelectorAll("tr"), function(tr){ tr.setAttribute("aria-selected", +tr.dataset.id === id); });
    try{ var q = qs(); history.replaceState(null, "", "/diversion/history?" + (q ? q + "&" : "") + "case=" + id); }catch(e){}
    get("/api/dv2/cases/" + id).then(function(j){
      if(!j || !j.case){ toast((j && j.error) || "This diversion was not found"); return; }
      var b = S.cases.filter(function(x){ return x.id === id; })[0];
      S.c = Object.assign({}, b || {}, j.case); paintDet();
      if(window.matchMedia && matchMedia("(max-width:899px)").matches) $("det").scrollIntoView({behavior:"smooth", block:"start"});
    });
  }
  function paintDet(){
    var c = S.c, p = period(c), live = c.state === "live" || c.state === "upcoming";
    var h = '<div class="hx-h"><h2>' + esc(c.blocked || c.road) + '<small>' + esc(c.dir_txt || "") + (c.dir_txt ? " • " : "") + '#' + c.id + '</small></h2>' + chip(c.state) + '</div>'
      + '<div class="hx-map" id="hmap" role="img" aria-label="Map of the closure and the diversion"></div>'
      + '<div><p class="hx-k">Period</p><p class="hx-p"><b>' + esc(p.a) + '</b>' + (p.b ? '<br>' + esc(p.b) : '') + '</p></div>'
      + '<div><p class="hx-k">Saved</p><p class="hx-p">' + esc(c.date) + ' ' + esc(c.time) + ' by ' + esc(c.by) + (c.manual ? ', route drawn by OCC' : '')
      + (c.from_case ? ', a copy of <a href="#" data-go="' + c.from_case + '" style="color:var(--ds-cyan)">#' + c.from_case + '</a>' : '') + (c.followed ? ', used again ' + c.followed + '×' : '') + '</p></div>'
      + (c.note ? '<div><p class="hx-k">Note</p><p class="hx-p">' + esc(c.note) + '</p></div>' : '')
      + '<div><p class="hx-k">Diverted route' + ((c.routes || []).length > 1 ? 's' : '') + '</p>' + (c.routes || []).map(function(r){
          var sk = r.skipped || [];
          return '<div class="hx-rt"><b style="color:#fff">' + ((c.routes || []).length > 1 ? 'Route ' + esc(r.n) + ' for ' : 'For ') + esc((r.services || []).join(", ")) + '</b><span class="tx">' + esc(r.text) + '</span>'
            + 'Skips ' + sk.length + (sk.length ? ' (' + sk.map(function(s){ return "BS " + esc(s.code); }).join(", ") + ')' : '') + (r.km ? ', ' + (+r.km).toFixed(1) + ' km diversion' : '')
            + (r.runs ? ', ' + String(r.runs).split(/\n/).filter(function(x){ return x.trim(); }).length + ' runs' : '') + (r.mileage && +r.mileage ? ', ' + esc(r.mileage) + ' km salvaged' : '') + '</div>'; }).join("") + '</div>'
      + (c.text ? '<div><p class="hx-k" style="display:flex;align-items:center">The OCC message<span style="flex:1"></span><button type="button" class="ds-btn sm ghost" id="dCopy">COPY</button></p><pre class="hx-msg" id="dMsg">' + esc(c.text) + '</pre></div>' : '')
      + '<div class="hx-acts">'
      + '<button type="button" class="ds-btn sm pri" id="aDup" aria-expanded="false" aria-controls="bDup">DUPLICATE…</button>'
      + (live ? '<button type="button" class="ds-btn sm" id="aEnd" style="border-color:#ff8a96;color:#ffc3ca">END NOW</button>' : '')
      + '<button type="button" class="ds-btn sm" id="aTimes" aria-expanded="false" aria-controls="bTimes">CHANGE TIMES…</button>'
      + '<a class="ds-btn sm" href="/diversion/tv?case=' + c.id + '" target="_blank" rel="noopener">PREVIEW ON TV</a>'
      + '<a class="ds-btn sm" href="/diversion?case=' + c.id + '">OPEN IN PLANNER</a>'
      + '<button type="button" class="ds-btn sm ghost" id="aDel">DELETE</button></div>'
      + '<div class="hx-box" id="bDup" hidden><h3>Use this diversion again</h3><p class="hx-p">A new record with the same blockage, routes and message. Live on the TV between the start and the end.</p>'
      + '<div class="hx-when"><label>Start<input type="datetime-local" class="ds-input" id="dStart"></label><label>End (empty = until further notice)<input type="datetime-local" class="ds-input" id="dEnd"></label></div>'
      + '<input type="text" class="ds-input note" id="dNote" placeholder="Note (optional)" aria-label="Note for the copy"><button type="button" class="ds-btn sm pri" id="dGo">DUPLICATE</button><p class="hx-err" id="dErr"></p></div>'
      + '<div class="hx-box" id="bTimes" hidden><h3>Change the start / end time</h3>'
      + '<div class="hx-when"><label>Start<input type="datetime-local" class="ds-input" id="tStart"></label><label>End (empty = until further notice)<input type="datetime-local" class="ds-input" id="tEnd"></label></div>'
      + '<button type="button" class="ds-btn sm pri" id="tGo">SAVE TIMES</button><p class="hx-err" id="tErr"></p></div>';
    $("det").innerHTML = h;
    drawMap(c);
    var go = $("det").querySelector("[data-go]"); if(go) go.onclick = function(e){ e.preventDefault(); select(+go.dataset.go); };
    if($("dCopy")) $("dCopy").onclick = function(){ var t = c.text; (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(function(){ toast("Message copied"); }, function(){ toast("Select the message and copy it"); }); };
    function box(btn, id, open){ var b = $(id), on = b.hidden; ["bDup", "bTimes"].forEach(function(x){ $(x).hidden = true; }); ["aDup", "aTimes"].forEach(function(x){ $(x).setAttribute("aria-expanded", "false"); }); b.hidden = !on; btn.setAttribute("aria-expanded", on ? "true" : "false"); if(on && open) open(); }
    $("aDup").onclick = function(){ box(this, "bDup", function(){ $("dStart").value = sgtNow(); $("dEnd").value = ""; $("dNote").value = c.note || ""; $("dErr").textContent = ""; $("dStart").focus(); }); };
    $("aTimes").onclick = function(){ box(this, "bTimes", function(){ $("tStart").value = c.start_in || sgtNow(); $("tEnd").value = c.end_in || ""; $("tErr").textContent = ""; $("tStart").focus(); }); };
    $("dGo").onclick = function(){
      var a = $("dStart").value || sgtNow(), b = $("dEnd").value;
      if(b && b <= a){ $("dErr").textContent = "The end time must be after the start time."; return; }
      this.disabled = true; var btn = this;
      post("/api/dv2/cases/" + c.id + "/duplicate", {start:a, end:b, note:$("dNote").value}).then(function(j){
        btn.disabled = false;
        if(!j || !j.ok){ $("dErr").textContent = (j && j.error) || "Could not duplicate"; return; }
        var n = j.case; toast("Duplicated as #" + n.id + (n.state === "live" ? " — live on the TV now" : n.state === "upcoming" ? " — starts " + n.start : ""));
        tvCount(); search(function(){ select(n.id); });
      });
    };
    $("tGo").onclick = function(){
      var a = $("tStart").value, b = $("tEnd").value;
      if(!a){ $("tErr").textContent = "Give a start time."; return; }
      if(b && b <= a){ $("tErr").textContent = "The end time must be after the start time."; return; }
      post("/api/dv2/cases/" + c.id + "/times", {start:a, end:b}).then(function(j){
        if(!j || !j.ok){ $("tErr").textContent = (j && j.error) || "Could not save the times"; return; }
        toast("Times saved"); tvCount(); search(function(){ select(c.id); });
      });
    };
    if($("aEnd")) $("aEnd").onclick = function(){
      if(!confirm(c.state === "upcoming" ? "Call off this diversion? It will not go live." : "End this diversion now? It comes off the TV.")) return;
      post("/api/dv2/cases/" + c.id + "/times", {end_now:true}).then(function(j){
        if(!j || !j.ok){ toast((j && j.error) || "Could not end it"); return; }
        toast("Ended at " + hm(j.case.end_ts)); tvCount(); search(function(){ select(c.id); });
      });
    };
    $("aDel").onclick = function(){
      if(!confirm("Delete diversion #" + c.id + " for everyone? This cannot be undone.")) return;
      post("/api/dv2/cases/" + c.id + "/delete", {}).then(function(){ toast("Deleted #" + c.id); S.sel = null; S.c = null; resetDet(); tvCount(); search(); });
    };
  }
  function resetDet(){ if(S.map){ S.map.remove(); S.map = null; } $("det").innerHTML = '<div class="hx-ph"><b style="color:#fff">Pick a diversion</b>Its map, routes and message show here, with duplicate, end and preview on the TV.</div>'; }
  function drawMap(c){
    if(S.map){ S.map.remove(); S.map = null; }
    if(typeof L === "undefined" || !$("hmap")) return;
    var map = S.map = L.map("hmap", {zoomControl:true, attributionControl:true, scrollWheelZoom:false}).setView([1.35, 103.82], 12);
    if(window.SGBasemap) SGBasemap.add(map, {style:"dark", switcher:false});
    else L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {subdomains:"abcd", maxZoom:20, attribution:"&copy; OpenStreetMap contributors &copy; CARTO"}).addTo(map);
    var all = [], line = function(pts, o){ if(pts && pts.length > 1){ L.polyline(pts, o).addTo(map); all = all.concat(pts); } };
    (c.routes || []).forEach(function(r){ line(r.orig, {color:"#38d6ff", weight:4, opacity:.55}); });
    (c.routes || []).forEach(function(r, i){ line(r.line, {color:i ? "#c96a00" : "#12a865", weight:6, opacity:.95}); });
    (c.blocks || []).forEach(function(b){ line(b.line, {color:"#e0263f", weight:8}); });
    if(all.length) map.fitBounds(all, {padding:[18, 18], maxZoom:17});
    setTimeout(function(){ if(S.map) S.map.invalidateSize(); }, 60);
  }
  function tvCount(){ get("/api/dv2/live").then(function(j){ var n = j && j.live ? j.live.length : 0, el = $("tvLive"); if(!el) return; el.hidden = !n; el.textContent = n; el.title = n + " live diversion" + (n === 1 ? "" : "s") + " on the TV"; }); }

  /* ---------------------------------------------------------------- go */
  var P = new URLSearchParams(location.search);
  $("fFrom").value = P.get("from") || sgtDay(30); $("fTo").value = P.get("to") || sgtDay(0);
  $("fRoad").value = P.get("road") || ""; $("fSvc").value = P.get("service") || ""; $("fState").value = P.get("state") || "";
  $("f").onsubmit = function(e){ e.preventDefault(); search(); };
  $("fState").onchange = function(){ search(); };
  var first = +(P.get("case") || 0);
  search(function(){ if(first) select(first); });
  tvCount();
})();
