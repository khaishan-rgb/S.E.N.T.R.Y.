/* AI Operations Centre layout (12 Nov 2026). Only on /ai-centre (the Forecast Centre keeps its own layout).
   Runs before forecast2026.js: it moves the map, the MRT / LRT panel and the cameras into a new grid and adds
     - Alerts now (counts by type) and Wet weather now                 (left)
     - AI predictive insights from the stored alert history            (right, above MRT / LRT)
     - Alerts: one list for bunching, long headway, traffic, rain      (bottom left)
     - Alert analysis: which type, which hour, which services, places  (bottom middle)
     - Live CCTV (the existing camera panel)                           (bottom right)
   Data: the page's own Forecast Centre data (window.SENTRY2026.state.data), /api/weather/impact and /api/events/insights. */
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };
  var main = document.querySelector("main.console"), map = document.querySelector(".map-panel"), mrt = $("mrtPanel"), cam = document.querySelector(".cam-panel");
  if(!main || !map) return;
  document.documentElement.dataset.layout = "ops";
  /* the service, operator and Exclude CBS filters stay usable in TV mode on this page (the Forecast Centre hides them on its wall) */
  [].forEach.call(document.querySelectorAll(".controller-only"), function(x){ x.classList.remove("controller-only"); x.classList.add("ops-keep"); });

  var CAT = {
    bunching: {name: "3-bus bunching", tile: "3BB bunching", short: "3BB", color: "#f755b5", icon: '<rect x="3" y="9" width="5" height="6" rx="1.2"/><rect x="9.5" y="9" width="5" height="6" rx="1.2"/><rect x="16" y="9" width="5" height="6" rx="1.2"/>'},
    gap: {name: "Long headway", short: "Gap", color: "#61c8ff", icon: '<path d="M3 12h4M17 12h4"/><path d="m7 9-3 3 3 3M17 9l3 3-3 3"/><path d="M10 12h4" stroke-dasharray="1 2"/>'},
    traffic: {name: "Traffic & incidents", tile: "Traffic", short: "Traffic", color: "#ff695b", icon: '<path d="M12 3 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>'},
    wet: {name: "Wet weather", short: "Rain", color: "#7cc4ff", icon: '<path d="M7 15a4.5 4.5 0 0 1 .5-9 6 6 0 0 1 11.3 2A3.5 3.5 0 0 1 18 15H7Z"/><path d="M8 18l-1 2.5M12.5 18l-1 2.5M17 18l-1 2.5"/>'},
    loading: {name: "Heavy loading", short: "Load", color: "#ffb64a", icon: '<circle cx="8" cy="7" r="2.5"/><circle cx="16" cy="7" r="2.5"/><path d="M4 20v-5a4 4 0 0 1 8 0v5M12 20v-5a4 4 0 0 1 8 0v5"/>'}
  };
  var LOG_CAT = {bunching: "bunching", long_headway: "gap", traffic: "traffic", wet_weather: "wet", heavy_loading: "loading"};      // stored log -> live names
  var svg = function(p, c){ return '<svg viewBox="0 0 24 24" class="' + (c || "ops-i") + '" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + "</svg>"; };
  var esc = function(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); };
  var hm = function(t){ return new Date(t * 1000).toLocaleTimeString("en-SG", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Singapore"}); };

  /* ---------------- build the grid ---------------- */
  var ops = document.createElement("section");
  ops.className = "ops"; ops.id = "ops"; ops.setAttribute("aria-label", "AI Operations Centre");
  var panel = function(id, area, title, sub, body){
    return '<section class="panel ops-p" id="' + id + '" style="grid-area:' + area + '"><header class="panel-head ops-h"><h2>' + title + "</h2>" + (sub || "") + "</header>" + body + "</section>";
  };
  ops.innerHTML = '<div class="ops-col" style="grid-area:left">'
      + panel("opsNow", "auto", "Alerts now", '<small id="opsNowSub"></small>', '<div class="ops-tiles" id="opsTiles"></div>')
      + panel("opsWx", "auto", "Wet weather now", '<small id="opsWxSub">NEA rain gauges</small>', '<div class="ops-body" id="opsWxBody"><p class="ops-mut">Loading…</p></div>')
    + "</div>"
    + '<div class="ops-slot" id="opsMapSlot" style="grid-area:map"></div>'
    + '<div class="ops-col" style="grid-area:right" id="opsRight">'
      + panel("opsAi", "auto", '<span class="ops-spark">' + svg('<path d="M12 2.5c.6 4.6 2.9 6.9 7.5 7.5-4.6.6-6.9 2.9-7.5 7.5-.6-4.6-2.9-6.9-7.5-7.5 4.6-.6 6.9-2.9 7.5-7.5Z" fill="currentColor" stroke="none"/>') + "</span>AI predictive insights",
        '<small id="opsAiSub">HORIZON AI</small>', '<div class="ops-body" id="opsAiScroll"><div id="opsBrief"><p class="ops-mut">HORIZON AI is reading the alert history…</p></div><h3 class="ops-stat-h">Patterns in the stored alerts <small>statistics the AI reads</small></h3><div id="opsAiBody"><p class="ops-mut">Loading…</p></div></div>')
    + "</div>"
    + panel("opsAlerts", "alerts", "Alerts", '<small id="opsAlertSub"></small>', '<div class="ops-filter" id="opsFilter" role="group" aria-label="Alert type"></div><div class="ops-body ops-list" id="opsList"></div>')
    + panel("opsAna", "analysis", "Alert analysis", '<div class="ops-period" role="group" aria-label="Period"><button type="button" data-days="7">7 days</button><button type="button" data-days="30" aria-pressed="true">30 days</button></div>',
        '<div class="ops-ana" id="opsAnaBody"><p class="ops-mut">Loading…</p></div>')
    + '<div class="ops-slot" id="opsCamSlot" style="grid-area:cctv"></div>'
    + '<div class="ops-aislot" id="opsAiSlot" style="grid-area:ai" aria-hidden="true"></div>'
    + '<div class="ops-ticker" id="opsTicker" style="grid-area:ticker" role="marquee" aria-label="Latest alerts"><span class="ops-tk-l">Latest alerts</span><div class="ops-tk-w"><div class="ops-tk-r" id="opsTickerRow"></div></div></div>';
  main.insertBefore(ops, main.firstChild);
  $("opsMapSlot").appendChild(map);
  if(mrt) $("opsCamSlot").appendChild(mrt);
  if(cam){ $("opsCamSlot").appendChild(cam); var h2 = cam.querySelector(".panel-head h2"); if(h2) h2.textContent = "Live CCTV"; }
  var HICON = {opsNow: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>', opsWx: CAT.wet.icon,
    opsAlerts: '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 21h14M12 3v2M4.2 7.2l1.4 1.4M19.8 7.2l-1.4 1.4"/>', opsAna: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    mrtPanel: '<rect x="5" y="3" width="14" height="14" rx="3"/><path d="M5 11h14M8 21l2-4M16 21l-2-4"/><circle cx="9" cy="14" r=".6"/><circle cx="15" cy="14" r=".6"/>',
    cam: '<path d="M3 7h12v10H3z"/><path d="m15 11 6-3v8l-6-3"/>'};
  Object.keys(HICON).forEach(function(id){
    var p = id === "cam" ? cam : $(id), h = p && p.querySelector(".panel-head h2");
    if(h && !h.querySelector(".ops-hi")) h.insertAdjacentHTML("afterbegin", '<span class="ops-hi">' + svg(HICON[id]) + "</span>");
  });
  function fit(){ ops.style.setProperty("--ops-top", Math.round(ops.getBoundingClientRect().top + window.scrollY) + "px"); }
  addEventListener("resize", fit); setTimeout(fit, 0); setTimeout(fit, 1500);

  /* ---------------- TV mode (?present=1): STEE-style wall layout ---------------- */
  var head = document.querySelector("header.topbar"), clock = head && head.querySelector(".clock"), tvBox = document.createElement("div");
  tvBox.className = "ops-tvtools";
  var homes = {};
  function park(id, into){ var x = $(id); if(!x) return; if(!homes[id]) homes[id] = {parent: x.parentNode, next: x.nextSibling}; into.appendChild(x); }
  function unpark(id){ var x = $(id), h = homes[id]; if(x && h && x.parentNode !== h.parent) h.parent.insertBefore(x, h.next && h.next.parentNode === h.parent ? h.next : null); }
  function dockAI(){
    var hai = $("hai"), slot = $("opsAiSlot"); if(!hai) return;
    var tv = document.body.classList.contains("tv") && slot.offsetWidth > 0;
    hai.classList.toggle("docked", tv);
    if(!tv){ ["--ai-l", "--ai-t", "--ai-w", "--ai-h"].forEach(function(v){ hai.style.removeProperty(v); }); return; }
    var r = slot.getBoundingClientRect();
    hai.style.setProperty("--ai-l", r.left + "px"); hai.style.setProperty("--ai-t", r.top + "px");
    hai.style.setProperty("--ai-w", r.width + "px"); hai.style.setProperty("--ai-h", r.height + "px");
  }
  function aiState(){
    var hai = $("hai"), min = !hai || hai.hidden;
    if(document.documentElement.classList.contains("ops-ai-min") !== min){
      document.documentElement.classList.toggle("ops-ai-min", min);
      setTimeout(function(){ dockAI(); dispatchEvent(new Event("resize")); }, 60);
    }
  }
  function tvMode(){
    var tv = document.body.classList.contains("tv");
    document.documentElement.classList.toggle("ops-tv", tv);
    fit(); [60, 400, 1200, 3000].forEach(function(ms){ setTimeout(function(){ fit(); dockAI(); if(ms === 60) dispatchEvent(new Event("resize")); }, ms); });
  }
  new MutationObserver(tvMode).observe(document.body, {attributes: true, attributeFilter: ["class"]});
  addEventListener("resize", function(){ setTimeout(dockAI, 30); });
  setInterval(function(){ if(document.body.classList.contains("tv")) dockAI(); }, 2000);        // the header or fonts may settle late
  var waitAI = setInterval(function(){ if($("hai")){ clearInterval(waitAI); new MutationObserver(aiState).observe($("hai"), {attributes: true, attributeFilter: ["hidden"]}); aiState(); tvMode(); } }, 300);
  setTimeout(function(){ clearInterval(waitAI); }, 20000);
  setTimeout(function(){ dispatchEvent(new Event("resize")); }, 1200);              // let the map re-measure its new box

  /* ---------------- live alerts (from the page's own data) ---------------- */
  var filter = "all", wx = null, lastData = null;
  function pageData(){ var s = window.SENTRY2026 && window.SENTRY2026.state; return s && s.data ? s.data : null; }
  function alerts(){
    var d = pageData(), out = [];
    if(d){
      var evk = {}; (d.events || []).forEach(function(e){ evk[e.id] = e.kind; });
      (d.alerts || []).forEach(function(a){
        var c = a.category; if(!CAT[c]) return;
        var kind = c === "traffic" ? (evk[a.event_id] || "") : "";
        out.push({cat: c, svc: a.svc, dir: a.dir, where: a.location || "", impact: (a.impact || "").replace(/^(\d+) consecutive buses converging$/, "$1 buses converging"), kind: kind, score: a.score || 0, status: a.status, id: a.id});
      });
    }
    ((wx && wx.now && wx.now.services) || []).forEach(function(s){
      out.push({cat: "wet", svc: s.service, dir: s.dir, where: s.area + " area", impact: s.expected_min != null ? "+" + s.expected_min.toFixed(1) + " min in rain" : (s.level === "heavy" ? "heavy rain" : "moderate rain"),
                kind: s.level === "heavy" ? "heavy" : "moderate", score: s.level === "heavy" ? 60 : 40});
    });
    return out.sort(function(a, b){ return b.score - a.score; });
  }
  var KIND_TXT = {accident: "Accident", roadworks: "Road works", jam: "Jam", congestion: "Congestion", breakdown: "Breakdown", closure: "Closure", heavy: "Heavy rain", moderate: "Rain"};
  function paintAlerts(){
    var all = alerts(), d = pageData(), counts = {all: all.length};
    Object.keys(CAT).forEach(function(c){ counts[c] = all.filter(function(a){ return a.cat === c; }).length; });
    var order = ["all", "bunching", "gap", "traffic", "wet", "loading"];
    $("opsFilter").innerHTML = order.map(function(c){
      return '<button type="button" data-cat="' + c + '" aria-pressed="' + (filter === c) + '" style="--c:' + (c === "all" ? "#3fd3ff" : CAT[c].color) + '">' + (c === "all" ? "All" : CAT[c].short) + "<b>" + counts[c] + "</b></button>";
    }).join("");
    var list = all.filter(function(a){ return filter === "all" || a.cat === filter; });
    $("opsAlertSub").textContent = d ? (d.mode === "demo" ? "Demo data · " : "Live · ") + (d.updated ? hm(d.updated) : "") : "Waiting for data";
    $("opsList").innerHTML = list.length ? list.slice(0, 60).map(function(a){
      var c = CAT[a.cat];
      return '<article class="ops-al" style="--c:' + c.color + '"><span class="ops-al-i">' + svg(c.icon) + '</span><div class="ops-al-m">'
        + '<div class="ops-al-t"><b>' + esc(a.kind && KIND_TXT[a.kind] ? KIND_TXT[a.kind] : c.name) + "</b>" + (a.status === "new" ? '<em>new</em>' : "") + "<strong>" + esc(a.impact) + "</strong></div>"
        + '<div class="ops-al-s"><span class="ops-svc">' + esc(a.svc) + "<i>D" + esc(a.dir) + '</i></span><span class="ops-al-w">' + esc(a.where) + "</span>"
        + '<button type="button" class="ops-ask" data-q="' + esc("Why is service " + a.svc + " " + c.name.toLowerCase() + " at " + a.where + ", and what should we do?") + '" title="Ask HORIZON AI about this alert">Ask AI</button></div></div></article>';
    }).join("") : '<p class="ops-empty">' + (filter === "all" ? "No active alerts within the current filters." : "No " + CAT[filter].name.toLowerCase() + " alerts now.") + "</p>";
    // tiles
    var k = (d && d.kpis) || {};
    $("opsTiles").innerHTML = ["bunching", "gap", "traffic", "wet", "loading"].map(function(c){
      var n = counts[c], avail = c === "wet" ? !!wx : (k[c] ? k[c].available !== false : !!d);
      return '<button type="button" class="ops-tile' + (n ? " on" : "") + '" data-cat="' + c + '" style="--c:' + CAT[c].color + '"><span class="ops-tile-i">' + svg(CAT[c].icon) + "</span><span class=\"ops-tile-n\">" + (avail ? n : "—")
        + '</span><span class="ops-tile-l">' + (CAT[c].tile || CAT[c].name) + "</span></button>";
    }).join("");
    var tk = all.slice(0, 14).map(function(a){ var c = CAT[a.cat];
      return '<span class="ops-tk" style="--c:' + c.color + '">' + svg(c.icon) + "<b>" + esc(a.kind && KIND_TXT[a.kind] ? KIND_TXT[a.kind] : c.name) + "</b>" + esc(a.svc) + " D" + esc(a.dir) + " \u00b7 " + esc(a.where) + (a.impact ? " \u00b7 " + esc(a.impact) : "") + "</span>"; }).join("");
    $("opsTickerRow").innerHTML = tk ? tk + tk : '<span class="ops-tk" style="--c:#39e4a2"><b>All clear</b>No active alerts</span>';
    $("opsTickerRow").classList.toggle("run", !!tk);
    var svcs = {}; all.forEach(function(a){ svcs[a.svc] = 1; });
    $("opsNowSub").textContent = all.length + " alerts · " + Object.keys(svcs).length + " services";
  }
  ops.addEventListener("click", function(e){
    var b = e.target.closest("[data-cat]");
    if(b && (b.closest("#opsFilter") || b.closest("#opsTiles"))){ filter = filter === b.dataset.cat && b.closest("#opsTiles") ? "all" : b.dataset.cat; paintAlerts(); return; }
    var q = e.target.closest(".ops-ask");
    if(q && window.HORIZON_AI){ window.HORIZON_AI.open(true); window.HORIZON_AI.ask(q.dataset.q); return; }
    var p = e.target.closest("[data-days]");
    if(p){ days = +p.dataset.days; [].forEach.call(document.querySelectorAll(".ops-period button"), function(x){ x.setAttribute("aria-pressed", x === p ? "true" : "false"); }); loadInsights(); }
  });

  /* ---------------- wet weather now ---------------- */
  function loadWx(){
    return fetch("/api/weather/impact?days=60", {credentials: "same-origin"}).then(function(r){ return r.ok ? r.json() : null; }).then(function(j){
      wx = j; paintWx(); paintAlerts();
    }).catch(function(){});
  }
  function paintWx(){
    var b = $("opsWxBody"), n = wx && wx.now;
    if(!n){ b.innerHTML = '<p class="ops-mut">Rain data unavailable.</p>'; return; }
    if(n.feed_ok === false){ b.innerHTML = '<p class="ops-mut">NEA rainfall not reachable right now.</p>'; return; }
    if(!(n.areas || []).length){
      var top = (wx.services || []).filter(function(s){ return s.added_min != null && s.added_min >= 1; }).slice(0, 3);
      b.innerHTML = '<div class="ops-dry">' + svg(CAT.wet.icon, "ops-dry-i") + "<div><b>No rain on bus routes</b>" + (top.length ? "<small>If it rains: " + top.map(function(s){ return s.service + " D" + s.dir + " +" + s.added_min.toFixed(1) + " min"; }).join(", ") + "</small>" : "<small>Rain impact appears as wet trips are measured.</small>") + "</div></div>";
      $("opsWxSub").textContent = "dry · NEA rain gauges";
      return;
    }
    $("opsWxSub").textContent = n.areas.length + " rain area" + (n.areas.length > 1 ? "s" : "");
    b.innerHTML = '<div class="ops-areas">' + n.areas.slice(0, 3).map(function(a){ return '<span class="' + (a.level === "heavy" ? "hv" : "") + '">' + esc(a.name) + " <small>" + (a.max_mm != null ? a.max_mm + " mm" : "") + "</small></span>"; }).join("") + "</div>"
      + '<div class="ops-wx">' + n.services.slice(0, 6).map(function(s){
        return '<div><span class="ops-svc">' + esc(s.service) + "<i>D" + s.dir + "</i></span><span class=\"ops-wx-a\">" + esc(s.area) + "</span><b>" + (s.expected_min != null ? "+" + s.expected_min.toFixed(1) + " min" : "—") + "</b></div>";
      }).join("") + (n.services.length > 6 ? '<small class="ops-mut">+' + (n.services.length - 6) + " more services</small>" : "") + "</div>";
  }

  /* ---------------- AI predictive insights + alert analysis (stored alerts) ---------------- */
  var days = 30;
  var LVL = {high: "Likely", medium: "Possible", info: "Note"};
  function loadInsights(){
    return fetch("/api/events/insights?days=" + days, {credentials: "same-origin"}).then(function(r){ return r.ok ? r.json() : null; }).then(function(j){
      if(!j){ $("opsAiBody").innerHTML = $("opsAnaBody").innerHTML = '<p class="ops-mut">The stored alert history could not be read.</p>'; return; }
      paintInsights(j); paintAnalysis(j.analysis || {}, j.storage || {});
    }).catch(function(){});
  }
  function paintInsights(j){
    var ins = j.insights || [], st = j.storage || {};
    if(!st.persistent) $("opsAiSub").textContent = "HORIZON AI · temporary storage";
    $("opsAiBody").innerHTML = ins.length ? ins.slice(0, 12).map(function(i){
      var c = CAT[LOG_CAT[i.category] || i.category] || {color: "#a07bff", icon: ""};
      return '<article class="ops-in lv-' + i.level + '" style="--c:' + c.color + '"><div class="ops-in-t"><span class="ops-in-b">' + (i.kind === "likely" ? i.likelihood + "%" : LVL[i.level] || "") + "</span><b>" + esc(i.title) + "</b></div>"
        + "<p>" + esc(i.detail) + "</p><small>" + esc(i.basis || "") + (i.kind === "likely" || i.kind === "rain_now" ? ' · <button type="button" class="ops-ask" data-q="' + esc(i.title + ". " + i.detail + " What should the OCC prepare?") + '">Ask AI</button>' : "") + "</small></article>";
    }).join("") : '<p class="ops-mut">No recurring patterns yet. Insights appear as alerts are stored.</p>';
  }
  var ANA = null, anaCat = "", KEYS = ["bunching", "long_headway", "traffic", "wet_weather", "heavy_loading"];
  var NAMES = {bunching: "3-bus bunching", long_headway: "Long headway", traffic: "Traffic & incidents", wet_weather: "Wet weather", heavy_loading: "Heavy loading"};
  var col = function(k){ return CAT[LOG_CAT[k]].color; };
  var EXP = '<button type="button" class="ops-exp" data-exp="{k}" title="Show larger, with counts" aria-label="Show larger">' + svg('<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>') + "</button>";
  function boxHours(a, big){
    var bh = a.by_hour || {}, keys = anaCat ? [anaCat] : KEYS, vals = [], hmax = 1, h;
    for(h = 0; h < 24; h++){ var s = 0; keys.forEach(function(k){ s += (bh[k] || [])[h] || 0; }); vals.push(s); hmax = Math.max(hmax, s); }
    var peak = vals.indexOf(Math.max.apply(null, vals));
    return '<div class="ops-hist' + (big ? " big" : "") + '" role="img" aria-label="Alerts by hour of day">' + vals.map(function(v, i){
      var segs = keys.map(function(k){ var n = (bh[k] || [])[i] || 0; return n ? '<em style="height:' + (n / hmax * 100).toFixed(1) + "%;background:" + col(k) + '"></em>' : ""; }).join("");
      return '<span title="' + String(i).padStart(2, "0") + ":00 · " + v + ' alerts"' + (v && i === peak ? ' class="pk"' : "") + ">" + (big && v ? "<b>" + v + "</b>" : "") + segs + "</span>";
    }).join("") + '</div><div class="ops-hist-x' + (big ? " big" : "") + '">' + (big ? vals.map(function(_, i){ return "<span>" + String(i).padStart(2, "0") + "</span>"; }).join("")
      : "<span>00</span><span>06</span><span>12</span><span>18</span><span>23</span>") + "</div>";
  }
  function boxServices(a, n){
    var keys = anaCat ? [anaCat] : KEYS, list = (a.top_services || []).map(function(s){ var t = 0; keys.forEach(function(k){ t += s[k] || 0; }); return {s: s, t: t}; })
      .filter(function(x){ return x.t; }).sort(function(x, y){ return y.t - x.t; }).slice(0, n), smax = list.length ? list[0].t : 1;
    return list.length ? list.map(function(x){
      var k = x.s.key.split(":"), seg = keys.map(function(c){ var v = x.s[c] || 0; return v ? '<em style="width:' + (v / smax * 100).toFixed(1) + "%;background:" + col(c) + '" title="' + NAMES[c] + ": " + v + '"></em>' : ""; }).join("");
      return '<div class="ops-bar ops-sv"><span><span class="ops-svc">' + esc(k[0]) + "<i>D" + esc(k[1]) + '</i></span></span><i class="stk">' + seg + "</i><b>" + x.t + "</b></div>";
    }).join("") : '<p class="ops-mut">None in this period.</p>';
  }
  function boxPlaces(a, n){
    var keys = anaCat ? [anaCat] : KEYS, list = (a.top_places || []).map(function(p){ var t = 0; keys.forEach(function(k){ t += p[k] || 0; }); return {p: p, t: t}; })
      .filter(function(x){ return x.t; }).sort(function(x, y){ return y.t - x.t; }).slice(0, n);
    return list.length ? list.map(function(x){ return '<div class="ops-place" style="--c:' + col(anaCat || x.p.category) + '"><span>' + esc(x.p.place) + "</span><b>" + x.t + "</b></div>"; }).join("")
      : '<p class="ops-mut">None in this period.</p>';
  }
  function paintAnalysis(a, st){
    ANA = a;
    var bc = a.by_category || {}, tot = a.total || 0;
    if(!tot){ $("opsAnaBody").innerHTML = '<p class="ops-mut">No alerts stored in the last ' + days + " days" + (st.persistent === false ? " (temporary storage: history resets when the server restarts)" : "") + ".</p>"; return; }
    var max = Math.max.apply(null, KEYS.map(function(k){ return bc[k] || 0; })) || 1;
    var mix = '<div class="ops-a-box"><h3>Which alert <small>' + (anaCat ? 'showing ' + NAMES[anaCat].toLowerCase() + ' · <button type="button" class="ops-link" data-acat="">show all</button>' : "press one to filter") + "</small></h3>"
      + KEYS.slice().sort(function(x, y){ return (bc[y] || 0) - (bc[x] || 0); }).map(function(k){
        var n = bc[k] || 0;
        return '<button type="button" class="ops-bar ops-pick' + (anaCat === k ? " on" : anaCat ? " dim" : "") + '" data-acat="' + k + '" aria-pressed="' + (anaCat === k) + '" style="--c:' + col(k) + '"><span>' + NAMES[k]
          + '</span><i><em style="width:' + (n / max * 100).toFixed(1) + '%"></em></i><b>' + n + "</b></button>";
      }).join("") + "</div>";
    var hours = '<div class="ops-a-box ops-a-hours"><h3>Which hour' + (anaCat ? " <small>" + NAMES[anaCat].toLowerCase() + "</small>" : a.peak_hour != null ? ' <small>peak ' + String(a.peak_hour).padStart(2, "0") + ":00</small>" : "")
      + EXP.replace("{k}", "hours") + "</h3>" + boxHours(a) + "</div>";
    var svcBox = '<div class="ops-a-box"><h3>Which services' + EXP.replace("{k}", "services") + "</h3>" + boxServices(a, 10) + "</div>";
    var places = '<div class="ops-a-box"><h3>Where' + EXP.replace("{k}", "places") + "</h3>" + boxPlaces(a, 8) + "</div>";
    $("opsAnaBody").innerHTML = mix + hours + svcBox + places;
  }
  /* larger view with every count */
  var modal = document.createElement("div");
  modal.className = "ops-modal"; modal.hidden = true; modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true");
  document.body.appendChild(modal);
  function openBig(k){
    if(!ANA) return;
    var t = {hours: "Alerts by hour of day", services: "Alerts by service", places: "Alerts by place"}[k], body = "";
    if(k === "hours"){
      var bh = ANA.by_hour || {}, keys = anaCat ? [anaCat] : KEYS;
      body = boxHours(ANA, true) + '<table class="ops-tbl"><thead><tr><th>Hour</th>' + keys.map(function(c){ return '<th style="color:' + col(c) + '">' + NAMES[c] + "</th>"; }).join("") + "<th>Total</th></tr></thead><tbody>"
        + Array.apply(null, {length: 24}).map(function(_, h){ var tt = 0, cells = keys.map(function(c){ var v = (bh[c] || [])[h] || 0; tt += v; return "<td>" + (v || "") + "</td>"; }).join("");
          return tt ? "<tr><td>" + String(h).padStart(2, "0") + ":00</td>" + cells + "<td><b>" + tt + "</b></td></tr>" : ""; }).join("") + "</tbody></table>";
    } else if(k === "services"){
      body = '<table class="ops-tbl"><thead><tr><th>Service</th>' + KEYS.map(function(c){ return '<th style="color:' + col(c) + '">' + NAMES[c] + "</th>"; }).join("") + "<th>Total</th></tr></thead><tbody>"
        + (ANA.top_services || []).map(function(s){ var x = s.key.split(":"); return '<tr><td><span class="ops-svc">' + esc(x[0]) + "<i>D" + esc(x[1]) + "</i></span></td>" + KEYS.map(function(c){ return "<td>" + (s[c] || "") + "</td>"; }).join("") + "<td><b>" + s.total + "</b></td></tr>"; }).join("") + "</tbody></table>";
    } else {
      body = '<table class="ops-tbl"><thead><tr><th>Place</th>' + KEYS.map(function(c){ return '<th style="color:' + col(c) + '">' + NAMES[c] + "</th>"; }).join("") + "<th>Total</th></tr></thead><tbody>"
        + (ANA.top_places || []).map(function(p){ return "<tr><td>" + esc(p.place) + "</td>" + KEYS.map(function(c){ return "<td>" + (p[c] || "") + "</td>"; }).join("") + "<td><b>" + p.total + "</b></td></tr>"; }).join("") + "</tbody></table>";
    }
    modal.innerHTML = '<section class="ops-modal-c"><header><h2>' + t + " <small>last " + days + " days" + (anaCat && k === "hours" ? " · " + NAMES[anaCat].toLowerCase() : "") + '</small></h2><button type="button" class="ops-x" aria-label="Close">×</button></header><div class="ops-modal-b">' + body + "</div></section>";
    modal.hidden = false; modal.querySelector(".ops-x").focus();
  }
  modal.addEventListener("click", function(e){ if(e.target === modal || e.target.closest(".ops-x")) modal.hidden = true; });
  addEventListener("keydown", function(e){ if(e.key === "Escape" && !modal.hidden) modal.hidden = true; });
  ops.addEventListener("click", function(e){
    var x = e.target.closest("[data-exp]"); if(x){ openBig(x.dataset.exp); return; }
    var c = e.target.closest("[data-acat]"); if(c && ANA){ anaCat = anaCat === c.dataset.acat ? "" : c.dataset.acat; paintAnalysis(ANA, {}); }
  });

  /* AI briefing: HORIZON AI (Claude) writes it from the patterns below, the 30-day summary, ongoing alerts and rain now */
  var LV = {high: "High", medium: "Watch", low: "Low"}, LVC = {high: "#ff5a6e", medium: "#ffb64a", low: "#3fd3ff"};
  function loadBrief(){
    return fetch("/api/events/ai-briefing", {credentials: "same-origin"}).then(function(r){ return r.ok ? r.json() : null; }).then(function(j){
      var b = $("opsBrief");
      if(!j){ b.innerHTML = '<p class="ops-mut">The AI briefing could not be loaded.</p>'; return; }
      if(!j.ok){ b.innerHTML = '<div class="ops-brief-off">' + svg('<path d="M12 2.5c.6 4.6 2.9 6.9 7.5 7.5-4.6.6-6.9 2.9-7.5 7.5-.6-4.6-2.9-6.9-7.5-7.5 4.6-.6 6.9-2.9 7.5-7.5Z" fill="currentColor" stroke="none"/>') + "<p>" + esc(j.error) + "</p></div>"; if(j.pending) setTimeout(loadBrief, 15000); return; }
      $("opsAiSub").textContent = "HORIZON AI \u00b7 " + hm(j.generated);
      b.innerHTML = (j.summary ? '<p class="ops-brief-sum">' + esc(j.summary) + "</p>" : "") + j.items.map(function(i){
        var c = LVC[i.level] || "#a07bff";
        return '<article class="ops-ai-it" style="--c:' + c + '"><div class="ops-in-t"><span class="ops-in-b">' + (LV[i.level] || "AI") + "</span><b>" + esc(i.title) + "</b></div>"
          + "<p>" + esc(i.why) + "</p>" + (i.action ? '<p class="ops-ai-act"><span>Prepare</span>' + esc(i.action) + "</p>" : "")
          + "<small>" + (i.when ? esc(i.when) + " \u00b7 " : "") + (i.services || []).map(function(k){ var x = k.split(":"); return esc(x[0]) + (x[1] ? " D" + esc(x[1]) : ""); }).join(", ")
          + ' <button type="button" class="ops-ask" data-q="' + esc(i.title + ". " + i.why + " Tell me more and what to do.") + '">Ask AI</button></small></article>';
      }).join("") + '<p class="ops-brief-note">Written by HORIZON AI (' + esc(j.model) + ') from the stored alerts and live data \u00b7 advisory only, the Service Controller decides.</p>';
    }).catch(function(){});
  }
  loadBrief(); setInterval(function(){ if(!document.hidden) loadBrief(); }, 5 * 60000);

  /* on the wall nobody scrolls: the insights move on by one card every 8 s, then start again */
  setInterval(function(){
    var b = $("opsAiScroll"); if(!document.body.classList.contains("tv") || !b || b.matches(":hover") || b.scrollHeight <= b.clientHeight + 4) return;
    var cards = b.querySelectorAll(".ops-ai-it,.ops-in"), top = b.scrollTop, next = null;
    for(var i = 0; i < cards.length; i++){ if(cards[i].offsetTop - b.offsetTop > top + 4){ next = cards[i]; break; } }
    b.scrollTo({top: next && top + b.clientHeight < b.scrollHeight - 4 ? next.offsetTop - b.offsetTop : 0, behavior: "smooth"});
  }, 8000);

  /* ---------------- refresh ---------------- */
  setInterval(function(){ var d = pageData(); if(d !== lastData){ lastData = d; paintAlerts(); } }, 1500);
  paintAlerts(); loadWx(); loadInsights();
  setInterval(function(){ if(!document.hidden) loadWx(); }, 60000);
  setInterval(function(){ if(!document.hidden) loadInsights(); }, 300000);
})();
