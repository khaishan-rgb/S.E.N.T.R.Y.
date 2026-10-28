/* HORIZON · Bus Operations Intelligence — application shell (V17 HORIZON; was SG Transport Pulse V16.0)

   ONE navigation registry (MODULES below) generates EVERY navigation surface:
     desktop sidebar · App Launcher · Command Centre shortcut cards · mobile drawer · mobile bottom bar · mobile "More" sheet.
   They therefore cannot disagree. To add, rename, lock or move a module, edit MODULES only.

   A page opts in with:
     <link rel="stylesheet" href="/design-system.css">
     <script src="/app-shell.js" data-module="<module id>"></script>
   The shell never touches a page's engine code; it only replaces the navigation chrome and exposes window.DS helpers.
   Preferences (Settings) are stored in this browser under "sgtp.prefs" and applied before first paint. */
(function(){
  "use strict";

  /* ------------------------------------------------------------------ registry (single source of truth) */
  // id, name, short (mobile label), icon, route, group, permission, shortcut (launcher + home cards), mobile (bottom bar slot or "more"),
  // enabled (false = shown grey + locked, never a dead link), desc (launcher / shortcut text), aliases (other URLs that are this module)
  var MODULES = [
    {id:"occlive",  name:"OCC Live",                  short:"OCC Live", icon:"bell",    route:"/occ-live",     group:"COMMAND",   permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     desc:"Combined live alert queue and OCC Connect \u2014 the always-on Service Controller workspace."},
    {id:"command",  name:"Forecast Centre",          short:"Home",     icon:"home",    route:"/command",      group:"COMMAND",   permission:"view", shortcut:false, mobile:"home",     enabled:true,
     aliases:["/forecast"], desc:"Predictive OCC home: verified route impact, 15 / 30 / 60-min forecasts, smart alerts"},
    {id:"route",    name:"Route Traffic",            short:"Traffic",  icon:"route",   route:"/route-traffic", aliases:["/"], group:"COMMAND",   permission:"view", shortcut:true,  mobile:"traffic",  enabled:true,
     desc:"Live route map: speed, incidents, road works, cameras"},
    {id:"headway",  name:"Headway Control",          short:"Control",  icon:"headway", route:"/control",      group:"COMMAND",   permission:"view", shortcut:true,  mobile:"control",  enabled:true,
     desc:"Pre-emptive departure adjustment by headway"},
    {id:"bunching", name:"Bunching & Gap",           short:"Bunching", icon:"bunch",   route:"/bunching",     group:"COMMAND",   permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     desc:"2BB / 3BB / 4BB and long-gap exception console"},
    {id:"recovery", name:"Recovery Decision Engine", short:"Recovery", icon:"engine",  route:"/recovery",     group:"RECOVERY",  permission:"view", shortcut:true,  mobile:"recovery", enabled:true,
     aliases:["/halfway/timetable"], desc:"Continue, regulate, adjust or halfway — compared on EWT"},
    {id:"halfplan", name:"Halfway Planner",          short:"Halfway",  icon:"half",    route:"/halfway",      group:"RECOVERY",  permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     aliases:["/halfway/os"], desc:"Where, when and how a halfway recovery happens, incl. off-service route"},
    {id:"diversion", name:"Diversion Maps",           short:"Diversion", icon:"divert", route:"/diversion",   group:"RECOVERY",  permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     desc:"Place a road block, see affected services and buses, compare diversions, then share and recover"},
    {id:"trafficaware", name:"Traffic-Aware Regulation", short:"Traffic-aware", icon:"alert", route:"/traffic", group:"RECOVERY",  permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     desc:"Congestion and incident impact on services"},
    {id:"running",  name:"Running Time Analytics",   short:"Running",  icon:"clock",   route:"/running-time", group:"ANALYTICS", permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     aliases:["/insight"], desc:"Measured running time, time-period reports"},
    {id:"stoprt",   name:"Stop-by-Stop Running Time", short:"Stop RT", icon:"stops",  route:"/stop-running-time", group:"ANALYTICS", permission:"view", shortcut:true, mobile:"more", enabled:true,
     desc:"Measured running time between every pair of stops, by time of day \u2014 Excel / CSV download"},
    {id:"ewt",      name:"EWT / Performance Analytics", short:"EWT",   icon:"chart",   route:"/performance",  group:"ANALYTICS", permission:"view", shortcut:true,  mobile:"more",     enabled:false,
     desc:"Not yet available — no EWT history store exists in this build"},
    {id:"cameras",  name:"Traffic Cameras",          short:"Cameras",  icon:"camera",  route:"/cameras",      group:"ANALYTICS", permission:"view", shortcut:true,  mobile:"more",     enabled:true,
     desc:"LTA camera wall by expressway"},
    {id:"settings", name:"Settings",                 short:"Settings", icon:"gear",    route:"/settings",     group:"SYSTEM",    permission:"view", shortcut:false, mobile:"more",     enabled:true,
     desc:"Preferences, notifications, data feeds, account"}
  ];
  var GROUPS = ["COMMAND", "RECOVERY", "ANALYTICS", "SYSTEM"];
  var BOTTOM = [["home", "command"], ["traffic", "route"], ["control", "headway"], ["recovery", "recovery"]];

  /* ------------------------------------------------------------------ icons (stroke, 24 grid) */
  var P = {
    home:'<path d="M4 11.5 12 5l8 6.5V20a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z"/>',
    route:'<circle cx="6" cy="18" r="2.2"/><circle cx="18" cy="6" r="2.2"/><path d="M8 18h6.5a3.5 3.5 0 0 0 0-7h-5a3.5 3.5 0 0 1 0-7H16"/>',
    headway:'<path d="M3 12h18"/><path d="m7 8-4 4 4 4"/><path d="m17 8 4 4-4 4"/><path d="M12 7v10"/>',
    bunch:'<rect x="3" y="9" width="5" height="6" rx="1.2"/><rect x="9.5" y="9" width="5" height="6" rx="1.2"/><path d="M17 12h4"/><path d="M5.5 18v1.5M12 18v1.5"/>',
    engine:'<path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="5.5"/><path d="m10 12 1.5 1.5L14.5 10"/>',
    half:'<path d="M3 17h18"/><path d="M3 7h8a4 4 0 0 1 4 4v6"/><circle cx="15" cy="17" r="2"/><path d="m8 4 3 3-3 3"/>',
    alert:'<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4.5M12 17v.5"/>',
    divert:'<path d="M5 21v-6.5"/><path d="M5 14.5C5 9 18 11 18 5.5"/><path d="m15.2 7.6 2.8-2.9 2.8 2.9"/><path d="m3 5 4 4M7 5 3 9"/>',
    clock:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    chart:'<path d="M4 20V4"/><path d="M4 20h16"/><path d="m7 15 4-5 3 3 5-6"/>',
    camera:'<path d="M4 8h3l1.5-2h7L17 8h3v11H4z"/><circle cx="12" cy="13.5" r="3.3"/>',
    gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 14.4H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.2-2.9l-.1-.1A2 2 0 1 1 7 4.6l.1.1a1.7 1.7 0 0 0 2.9-1.2V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    bell:'<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    grid:'<circle cx="5.5" cy="5.5" r="1.6"/><circle cx="12" cy="5.5" r="1.6"/><circle cx="18.5" cy="5.5" r="1.6"/><circle cx="5.5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18.5" cy="12" r="1.6"/><circle cx="5.5" cy="18.5" r="1.6"/><circle cx="12" cy="18.5" r="1.6"/><circle cx="18.5" cy="18.5" r="1.6"/>',
    menu:'<path d="M4 7h16M4 12h16M4 17h16"/>',
    search:'<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    stops:'<circle cx="4.5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19.5" cy="12" r="2"/><path d="M6.5 12h3.5M14 12h3.5"/><path d="M4.5 7V5.5M19.5 7V5.5M4.5 5.5h15"/>',
    tv:'<rect x="3" y="5" width="18" height="12" rx="1.5"/><path d="M8 20h8M12 17v3"/>',
    more:'<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    chev:'<path d="m6 9 6 6 6-6"/>',
    collapse:'<path d="M4 5v14"/><path d="m14 8-4 4 4 4"/><path d="M10 12h10"/>',
    user:'<circle cx="12" cy="8" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
    sliders:'<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    out:'<path d="M14 4h5v16h-5"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
    spark:'<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><path d="m12 8 1.3 2.7L16 12l-2.7 1.3L12 16l-1.3-2.7L8 12l2.7-1.3z"/>',
    lock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    inbox:'<path d="M4 13 6.5 5h11L20 13v6H4z"/><path d="M4 13h4.5l1 2h5l1-2H20"/>',
    logo:'<path d="M3 16.5c3.5-6 8-9.5 18-11" stroke="#38d6ff"/><path d="M3 20c4-4 8.5-6 18-6.5" stroke="#2f8cff" opacity=".7"/><circle cx="8.5" cy="11.2" r="1.7" fill="#38d6ff" stroke="none"/><circle cx="16" cy="7.2" r="1.7" fill="#fff" stroke="none"/>'
  };
  function icon(n, extra){ return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"' + (extra || "") + '>' + (P[n] || "") + '</svg>'; }
  function esc(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }

  /* ------------------------------------------------------------------ preferences (applied before paint) */
  var PKEY = "sgtp.prefs";
  var DEFAULT_PREFS = {theme:"dark", density:"comfortable", sidebar:"expanded", reduceMotion:false, mapFx:"normal", textSize:"normal", highContrast:false, colourBlind:false,
                       timeFormat:"24", dateFormat:"dmy", landing:"/command", sound:false, hiddenPages:[], blockHidden:true, displayName:"", staffId:"", role:"", department:"", avatar:""};
  function prefs(){ var p = {}; try{ p = JSON.parse(localStorage.getItem(PKEY) || "{}") || {}; }catch(e){} var o = {}; for(var k in DEFAULT_PREFS) o[k] = DEFAULT_PREFS[k]; for(var k2 in p) o[k2] = p[k2]; return o; }
  function savePrefs(patch){ var p = prefs(); for(var k in patch) p[k] = patch[k]; try{ localStorage.setItem(PKEY, JSON.stringify(p)); }catch(e){} applyPrefs(p); return p; }
  function applyPrefs(p){
    var h = document.documentElement;
    h.setAttribute("data-density", p.density === "compact" ? "compact" : "comfortable");
    h.setAttribute("data-textsize", p.textSize || "normal");
    h.setAttribute("data-mapfx", p.mapFx || "normal");
    h.classList.toggle("ds-reduce-motion", !!p.reduceMotion);
    h.classList.toggle("ds-hc", !!p.highContrast);
    h.classList.toggle("ds-cb", !!p.colourBlind);
    h.classList.toggle("ds-side-collapsed", p.sidebar === "collapsed");
  }
  applyPrefs(prefs());

  var me = document.currentScript;
  var ACTIVE = (me && me.getAttribute("data-module")) || detect();
  var BARE = me && me.hasAttribute("data-bare");                  // login page: preferences only, no chrome
  function detect(){
    var p = location.pathname.replace(/\/+$/, "") || "/";
    for(var i = 0; i < MODULES.length; i++){ var m = MODULES[i]; if(m.route === p || (m.aliases || []).indexOf(p) >= 0) return m.id; }
    return "";
  }
  function mod(id){ for(var i = 0; i < MODULES.length; i++) if(MODULES[i].id === id) return MODULES[i]; return null; }
  // V16.8: pages hidden in Settings > Pages (for everyone). Settings itself can never be hidden.
  // The list comes from the server (window.__SGTP_HIDDEN, put at the top of this file by /app-shell.js) so it is the same for everyone.
  function hiddenIds(){ return Array.isArray(window.__SGTP_HIDDEN) ? window.__SGTP_HIDDEN : (prefs().hiddenPages || []); }
  function blockOn(){ return typeof window.__SGTP_BLOCK === "boolean" ? window.__SGTP_BLOCK : prefs().blockHidden !== false; }
  function hid(m){ return !!m && m.id !== "settings" && hiddenIds().indexOf(m.id) >= 0; }

  /* ------------------------------------------------------------------ renderers (all from MODULES) */
  function linkHTML(m, cls){
    if(!m.enabled) return '<span class="' + cls + ' locked" aria-disabled="true" title="' + esc(m.name + " \u2014 " + m.desc) + '">' + icon(m.icon) + '<span>' + esc(m.name) + '</span><em class="ds-tag">SOON</em></span>';
    return '<a class="' + cls + (m.id === ACTIVE ? " active" : "") + '" href="' + m.route + '"' + (m.id === ACTIVE ? ' aria-current="page"' : "") + ' title="' + esc(m.name) + '">' + icon(m.icon) + '<span>' + esc(m.name) + '</span></a>';
  }
  function sidebarHTML(){
    var h = '<a class="ds-brand hz-brand" href="/command" aria-label="SBS Transit HORIZON \u2014 Bus Operations Intelligence \u2014 Forecast Centre">'
      + '<img class="hz-sbs" src="/brand/sbs-transit.webp" alt="SBS Transit" width="72" height="20">'
      + '<img class="hz-word" src="/brand/horizon-wordmark.webp" alt="HORIZON \u2014 Bus Operations Intelligence" width="216" height="37">'
      + '<img class="hz-mark" src="/brand/horizon-mark.svg" alt="HORIZON" width="36" height="36"></a><nav class="ds-nav" aria-label="Primary">';
    GROUPS.forEach(function(g){
      var items = MODULES.filter(function(m){ return m.group === g && !hid(m); }); if(!items.length) return;
      h += '<div class="ds-grp">' + g + '</div>' + items.map(function(m){ return linkHTML(m, "ds-link"); }).join("");
    });
    h += '</nav><div class="ds-side-foot"><i class="ds-dot" id="dsSideDot"></i><span id="dsSideTxt">Checking data feeds\u2026</span>'
      + '<button class="ds-iconbtn ds-hide-m" id="dsCollapse" type="button" style="margin-left:auto;width:30px;height:30px" aria-label="Collapse navigation" title="Collapse / expand navigation">' + icon("collapse") + '</button></div>';
    return h;
  }
  function launcherHTML(){
    var h = '<h4>APPS <a href="/settings">Settings</a></h4>';
    GROUPS.forEach(function(g){
      var items = MODULES.filter(function(m){ return m.group === g && !hid(m); }); if(!items.length) return;
      h += '<div class="ds-lgrp">' + g + '</div><div class="ds-lgrid">' + items.map(function(m){
        var inner = '<span class="ic">' + icon(m.enabled ? m.icon : "lock") + '</span>' + esc(m.name);
        return m.enabled ? '<a class="ds-app-tile' + (m.id === ACTIVE ? " active" : "") + '" href="' + m.route + '" title="' + esc(m.desc) + '">' + inner + '</a>'
                         : '<span class="ds-app-tile locked" title="' + esc(m.desc) + '">' + inner + '</span>';
      }).join("") + '</div>';
    });
    return h;
  }
  function bottomHTML(){
    var h = BOTTOM.filter(function(b){ return !hid(mod(b[1])); }).map(function(b){ var m = mod(b[1]);
      return '<a href="' + m.route + '" class="' + (m.id === ACTIVE ? "active" : "") + '"' + (m.id === ACTIVE ? ' aria-current="page"' : "") + '>' + icon(m.icon) + '<span>' + b[0].toUpperCase() + '</span></a>'; }).join("");
    var inMore = MODULES.some(function(m){ return m.mobile === "more" && m.id === ACTIVE; });
    return h + '<button type="button" id="dsMoreBtn" class="' + (inMore ? "active" : "") + '" aria-expanded="false" aria-controls="dsSheet">' + icon("more") + '<span>MORE</span></button>';
  }
  function sheetHTML(){
    return '<h4>MORE</h4>' + MODULES.filter(function(m){ return m.mobile === "more" && !hid(m); }).map(function(m){
      return m.enabled ? '<a href="' + m.route + '" class="' + (m.id === ACTIVE ? "active" : "") + '">' + icon(m.icon) + esc(m.name) + '</a>'
                       : '<a class="locked" aria-disabled="true">' + icon("lock") + esc(m.name) + '<em class="ds-tag">SOON</em></a>';
    }).join("") + '<a href="#" data-ds-open="dsLaunch">' + icon("grid") + 'All apps</a>';
  }
  function cmdHTML(){
    var m = mod(ACTIVE);
    var crumb = m ? '<span>' + esc(m.group.charAt(0) + m.group.slice(1).toLowerCase()) + '</span><i>/</i><b>' + esc(m.name) + '</b>' : '<b>' + esc(document.title.split(/[\u00b7|\u2013-]/)[0]) + '</b>';
    return '<button class="ds-iconbtn ds-drawer-btn" type="button" id="dsDrawerBtn" aria-label="Open navigation">' + icon("menu") + '</button>'
      + '<nav class="ds-crumb" aria-label="Breadcrumb"><span>HORIZON</span><i>/</i>' + crumb + '</nav>'
      + '<span class="ds-mbrand">HORIZON</span><span class="ds-cmd-sp"></span>'
      + '<button class="ds-kbd" type="button" data-hz-qk aria-label="Launch an engine (Ctrl+K)" title="Launch any engine \u2014 Ctrl/\u2318 + K">' + icon("search") + '<span>Launch engine</span><kbd>Ctrl K</kbd></button>'
      + '<div class="ds-status"><span class="ds-live" id="dsLive" title="Checking data feeds"><i class="ds-dot"></i><span id="dsLiveT">CHECKING</span></span>'
      + '<span class="ds-fresh" id="dsFresh">Data updated <b id="dsFreshT">\u2013</b></span></div>'
      + '<button class="ds-iconbtn" type="button" data-ds-open="dsNotes" aria-label="Notifications" aria-expanded="false">' + icon("bell") + '<span class="ds-badge" id="dsBadge" hidden>0</span></button>'
      + '<button class="ds-iconbtn" type="button" data-ds-open="dsLaunch" aria-label="App launcher" aria-expanded="false" title="Apps">' + icon("grid") + '</button>'
      + '<a class="ds-iconbtn ds-hide-m" href="/settings" aria-label="Settings" title="Settings">' + icon("gear") + '</a>'
      + '<button class="ds-prof ds-hide-m" type="button" data-ds-open="dsProfile" aria-expanded="false" aria-label="Profile menu"><span class="ds-av" id="dsAv">OCC</span><span id="dsWho">Controller</span>' + icon("chev") + '</button>';
  }
  function profileHTML(u){
    var p = prefs(), nm = (u && u.display_name) || p.displayName || "Controller", role = (u && u.role) || p.role || "", sid = (u && u.staff_id) || p.staffId || "";
    return '<div class="ds-who"><span class="ds-av">' + avatar(nm, p.avatar) + '</span><div><b>' + esc(nm) + '</b><small>' + esc([role, sid].filter(Boolean).join(" \u00b7 ") || (AUTH.enabled ? "Signed in" : "Open access \u2014 no sign-in provider")) + '</small></div></div>'
      + '<div class="ds-menu"><a href="/settings#account">' + icon("user") + 'Profile</a><a href="/settings#appearance">' + icon("sliders") + 'Preferences</a><a href="/settings">' + icon("gear") + 'Settings</a><hr>'
      + '<button type="button" id="dsSignOut">' + icon("out") + (AUTH.enabled ? "Sign out" : "Sign-in page") + '</button></div>';
  }
  function initials(n){ var w = String(n || "").trim().split(/\s+/).filter(Boolean); return w.length ? (w[0][0] + (w.length > 1 ? w[w.length - 1][0] : (w[0][1] || ""))).toUpperCase() : "OCC"; }
  function avatar(nm, img){ return img ? '<img alt="" src="' + esc(img) + '">' : esc(initials(nm)); }

  /* ------------------------------------------------------------------ mount */
  var AUTH = {enabled:false, user:null};
  /* ------------------------------------------------------------------ HORIZON logos never show as broken images:
     if a /brand/ file cannot load (e.g. the brand folder is missing on the server), it is replaced by a styled text logo. */
  function logoFallback(img){
    if(!img || img.getAttribute("data-hz-fb")) return; img.setAttribute("data-hz-fb", "1");
    var src = img.getAttribute("src") || "", sp = document.createElement("span");
    sp.setAttribute("role", "img"); sp.setAttribute("aria-label", img.getAttribute("alt") || "HORIZON");
    var word = '<span class="w">H<span class="o"></span>RIZON</span>', tag = '<span class="t">BUS OPERATIONS INTELLIGENCE</span>', sbs = '<span class="hz-tl-sbs"><b>SBS</b><i>Transit</i></span>';
    if(/sbs-transit/.test(src)){ sp.className = "hz-tl-sbs " + img.className; sp.innerHTML = '<b>SBS</b><i>Transit</i>'; }
    else if(/horizon-mark/.test(src)){ sp.className = "hz-tl-mark " + img.className; sp.innerHTML = '<span class="o"></span>'; }
    else if(/horizon-lockup/.test(src)){ sp.className = "hz-tl-hz " + img.className; sp.innerHTML = sbs + word + tag; }
    else { sp.className = "hz-tl-hz " + img.className; sp.innerHTML = word + (/wordmark/.test(src) ? tag : ""); }
    if(img.parentNode) img.parentNode.replaceChild(sp, img);
  }
  document.addEventListener("error", function(e){ var t = e.target; if(t && t.tagName === "IMG" && /\/brand\//.test(t.getAttribute("src") || "")) logoFallback(t); }, true);
  function checkLogos(){ document.querySelectorAll('img[src*="/brand/"]').forEach(function(i){ if(i.complete && !i.naturalWidth) logoFallback(i); }); }
  window.addEventListener("load", checkLogos);

  /* ------------------------------------------------------------------ HORIZON: document brand, boot sequence, quick launch */
  var EXTRA = [   // launch targets that are views of a module rather than modules
    {id:"tv-forecast", name:"Forecast Centre \u2014 TV wall", icon:"tv", route:"/command?present=1", group:"COMMAND", enabled:true, desc:"Full-screen OCC wall: heavy loading, incidents, bunching & gaps"},
    {id:"tv-diversion", name:"Diversion Maps \u2014 TV wall", icon:"tv", route:"/diversion/tv", group:"RECOVERY", enabled:true, desc:"Live diversions on an LED / TV panel"}
  ];
  function brandDocument(){
    if(/SG Transport Pulse|S\.E\.N\.T\.R\.Y/i.test(document.title)) document.title = document.title.replace(/SG Transport Pulse|S\.E\.N\.T\.R\.Y/ig, "HORIZON");
    else if(!/HORIZON/.test(document.title)) document.title = (document.title ? document.title + " \u00b7 " : "") + "HORIZON";
    if(!document.querySelector('link[rel~="icon"]')){ var l = document.createElement("link"); l.rel = "icon"; l.type = "image/svg+xml"; l.href = "/brand/horizon-mark.svg"; document.head.appendChild(l); }
  }
  function reduced(){ return prefs().reduceMotion || (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches); }
  function boot(){
    var seen = false; try{ seen = sessionStorage.getItem("hz.boot") === "1"; sessionStorage.setItem("hz.boot", "1"); }catch(e){ seen = true; }
    if(seen || reduced() || /[?&]present=1/.test(location.search) || /\/tv\b/.test(location.pathname)) return;
    var o = document.createElement("div"); o.className = "hz-boot"; o.setAttribute("aria-hidden", "true");
    o.innerHTML = '<div class="hz-boot-in"><img src="/brand/horizon-lockup.webp" alt=""><div class="ln"></div><div class="st">INITIALISING OPERATIONS INTELLIGENCE</div></div>';
    document.body.appendChild(o);
    var end = function(){ o.classList.add("done"); setTimeout(function(){ o.remove(); }, 500); };
    o.addEventListener("click", end); setTimeout(end, 1500);
  }
  function qkItems(){
    return MODULES.filter(function(m){ return !hid(m); }).concat(EXTRA);
  }
  function qkHTML(){
    return '<div class="hz-qk-box" role="dialog" aria-modal="true" aria-label="Launch an engine"><div class="hz-qk-in">' + icon("search")
      + '<input id="hzQkIn" type="text" autocomplete="off" spellcheck="false" placeholder="Launch engine \u2014 type a name, e.g. bunching, diversion, TV" aria-label="Engine name" aria-controls="hzQkList"><kbd>Esc</kbd></div>'
      + '<div class="hz-qk-list" id="hzQkList" role="listbox"></div>'
      + '<div class="hz-qk-foot"><span><kbd>\u2191</kbd><kbd>\u2193</kbd>choose</span><span><kbd>Enter</kbd>launch</span><span><kbd>Ctrl</kbd><kbd>Enter</kbd>new tab</span></div></div>';
  }
  var QK = {sel:0, list:[]};
  function qkRender(){
    var q = (document.getElementById("hzQkIn").value || "").trim().toLowerCase(), words = q.split(/\s+/).filter(Boolean);
    QK.list = qkItems().filter(function(m){ var hay = [m.name, m.short, m.desc, m.group, m.route].concat(m.aliases || []).join(" ").toLowerCase();
      return words.every(function(w){ return hay.indexOf(w) >= 0; }); });
    QK.sel = Math.min(QK.sel, Math.max(0, QK.list.length - 1));
    document.getElementById("hzQkList").innerHTML = QK.list.length ? QK.list.map(function(m, i){
      return '<a class="hz-qk-item' + (i === QK.sel ? " sel" : "") + (m.enabled ? "" : " locked") + '" role="option" aria-selected="' + (i === QK.sel) + '" data-i="' + i + '"' + (m.enabled ? ' href="' + m.route + '"' : ' aria-disabled="true"') + '>'
        + '<span class="ic">' + icon(m.enabled ? m.icon : "lock") + '</span><span class="tx"><b>' + esc(m.name) + '</b><small>' + esc(m.desc || "") + '</small></span><span class="grp">' + esc(m.id === ACTIVE ? "OPEN" : m.group) + '</span></a>';
    }).join("") : '<div class="hz-qk-none">No engine matches \u201c' + esc(q) + '\u201d</div>';
    var el = document.querySelector(".hz-qk-item.sel"); if(el && el.scrollIntoView) el.scrollIntoView({block:"nearest"});
  }
  function qkOpen(on){
    var o = document.getElementById("hzQk"); if(!o) return;
    o.classList.toggle("open", on);
    if(on){ var i = document.getElementById("hzQkIn"); i.value = ""; QK.sel = 0; qkRender(); setTimeout(function(){ i.focus(); }, 30); }
  }
  function qkGo(i, newTab){ var m = QK.list[i]; if(!m || !m.enabled) return; if(newTab) window.open(m.route, "_blank", "noopener"); else location.href = m.route; }
  function quickLaunch(){
    var o = document.createElement("div"); o.className = "hz-qk"; o.id = "hzQk"; o.innerHTML = qkHTML(); document.body.appendChild(o);
    o.addEventListener("click", function(e){ if(e.target === o) qkOpen(false); var it = e.target.closest(".hz-qk-item"); if(it){ e.preventDefault(); qkGo(+it.getAttribute("data-i"), e.ctrlKey || e.metaKey); } });
    o.addEventListener("mousemove", function(e){ var it = e.target.closest(".hz-qk-item"); if(it && +it.getAttribute("data-i") !== QK.sel){ QK.sel = +it.getAttribute("data-i"); document.querySelectorAll(".hz-qk-item").forEach(function(x, i){ x.classList.toggle("sel", i === QK.sel); x.setAttribute("aria-selected", String(i === QK.sel)); }); } });
    document.getElementById("hzQkIn").addEventListener("input", function(){ QK.sel = 0; qkRender(); });
    document.addEventListener("keydown", function(e){
      var open = o.classList.contains("open");
      if((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && (e.key === "k" || e.key === "K")){ e.preventDefault(); qkOpen(!open); return; }
      if(!open) return;
      if(e.key === "Escape"){ e.preventDefault(); qkOpen(false); }
      else if(e.key === "ArrowDown"){ e.preventDefault(); QK.sel = Math.min(QK.list.length - 1, QK.sel + 1); qkRender(); }
      else if(e.key === "ArrowUp"){ e.preventDefault(); QK.sel = Math.max(0, QK.sel - 1); qkRender(); }
      else if(e.key === "Enter"){ e.preventDefault(); qkGo(QK.sel, e.ctrlKey || e.metaKey); }
    });
    document.addEventListener("click", function(e){ if(e.target.closest("[data-hz-qk]")){ e.preventDefault(); qkOpen(true); } });
  }
  window.HORIZON = {launch:function(){ qkOpen(true); }, modules:function(){ return qkItems().map(function(m){ return {id:m.id, name:m.name, route:m.route, enabled:m.enabled}; }); }};

  function mount(){
    var b = document.body; if(!b) return;
    brandDocument(); boot(); setTimeout(checkLogos, 0);
    b.classList.add("ds-app"); b.setAttribute("data-ds-module", ACTIVE || "");
    document.documentElement.classList.remove("shell-off");            // the V13.14 per-page hide-menu flag is replaced by the collapsible sidebar
    if(BARE) return;
    quickLaunch();
    if(ACTIVE && hid(mod(ACTIVE)) && blockOn()){                 // V16.7: a hidden page opened by link / bookmark
      var alt = MODULES.filter(function(m){ return m.enabled && !hid(m) && m.id !== "settings"; })[0], ov = document.createElement("div");
      ov.setAttribute("role", "alertdialog"); ov.setAttribute("aria-label", "Page hidden");
      ov.style.cssText = "position:fixed;inset:0;z-index:2147483000;background:#0b1622;color:#e6eef7;display:flex;align-items:center;justify-content:center;text-align:center;font:16px/1.5 system-ui,sans-serif;padding:24px";
      ov.innerHTML = '<div style="max-width:420px"><div style="font-size:20px;font-weight:700;margin-bottom:8px">This page is hidden</div><div style="opacity:.8;margin-bottom:18px">It was hidden in Settings \u203a Pages.</div>'
        + (alt ? '<a href="' + alt.route + '" style="display:inline-block;margin:4px;padding:10px 16px;border-radius:8px;background:#1f6feb;color:#fff;text-decoration:none">Open ' + esc(alt.name) + '</a>' : "")
        + '<a href="/settings#pages" style="display:inline-block;margin:4px;padding:10px 16px;border-radius:8px;border:1px solid #3b5573;color:#e6eef7;text-decoration:none">Settings \u203a Pages</a></div>';
      b.appendChild(ov); return;
    }
    // the legacy per-page navigation lists are no longer rendered; anything left over is hidden, never used
    document.querySelectorAll(".desktop-shell,.mobile-bottom-nav,.mobile-more,.mobile-top-menu,.mobile-menu-btn,nav.navl,header nav.nav,.shell-show,.shell-hide,.cmdbar,.ai-shell-card")
      .forEach(function(e){ e.classList.add("ds-dup"); e.setAttribute("aria-hidden", "true"); });

    var side = document.createElement("aside"); side.className = "ds-side"; side.id = "dsSide"; side.setAttribute("aria-label", "Primary navigation"); side.innerHTML = sidebarHTML();
    var scrim = document.createElement("div"); scrim.className = "ds-scrim"; scrim.id = "dsScrim";
    var cmd = document.createElement("header"); cmd.className = "ds-cmd"; cmd.id = "dsCmd"; cmd.setAttribute("role", "banner"); cmd.innerHTML = cmdHTML();
    b.insertBefore(cmd, b.firstChild); b.insertBefore(scrim, b.firstChild); b.insertBefore(side, b.firstChild);

    var pops = [["dsLaunch", launcherHTML()], ["dsNotes", '<h4>NOTIFICATIONS <a href="/command">Forecast Centre</a></h4><div id="dsNoteList"><div class="ds-note-empty">Loading\u2026</div></div>'], ["dsProfile", profileHTML(null)]];
    pops.forEach(function(x){ var d = document.createElement("div"); d.className = "ds-pop"; d.id = x[0]; d.setAttribute("role", "dialog"); d.innerHTML = x[1]; b.appendChild(d); });
    var bn = document.createElement("nav"); bn.className = "ds-bnav"; bn.setAttribute("aria-label", "Mobile navigation"); bn.innerHTML = bottomHTML(); b.appendChild(bn);
    var sh = document.createElement("div"); sh.className = "ds-sheet"; sh.id = "dsSheet"; sh.innerHTML = sheetHTML(); b.appendChild(sh);

    wire();
    status().then(notes); session();
    setInterval(status, 30000); setInterval(notes, 45000);
    if(!prefs().reduceMotion) b.classList.add("ds-enter");
    setTimeout(function(){ window.dispatchEvent(new Event("resize")); }, 80);   // maps re-measure after the shell takes its space
  }

  function closeAll(except){
    document.querySelectorAll(".ds-pop.open").forEach(function(p){ if(p.id !== except) p.classList.remove("open"); });
    document.querySelectorAll("[data-ds-open]").forEach(function(t){ if(t.getAttribute("data-ds-open") !== except) t.setAttribute("aria-expanded", "false"); });
    if(except !== "dsSheet"){ var s = document.getElementById("dsSheet"); if(s) s.classList.remove("open"); var mb = document.getElementById("dsMoreBtn"); if(mb) mb.setAttribute("aria-expanded", "false"); }
  }
  function wire(){
    document.addEventListener("click", function(e){
      var t = e.target.closest("[data-ds-open]");
      if(t){ e.preventDefault(); e.stopPropagation(); var id = t.getAttribute("data-ds-open"), p = document.getElementById(id); var was = p.classList.contains("open");
        closeAll(id); p.classList.toggle("open", !was); document.querySelectorAll('[data-ds-open="' + id + '"]').forEach(function(x){ x.setAttribute("aria-expanded", String(!was)); });
        if(!was && id === "dsNotes") notes(); return; }
      if(!e.target.closest(".ds-pop,.ds-sheet,#dsMoreBtn")) closeAll("");
    });
    document.addEventListener("keydown", function(e){ if(e.key === "Escape"){ closeAll(""); document.documentElement.classList.remove("ds-drawer"); } });
    var mb = document.getElementById("dsMoreBtn");
    if(mb) mb.addEventListener("click", function(e){ e.stopPropagation(); var s = document.getElementById("dsSheet"), o = !s.classList.contains("open"); closeAll("dsSheet"); s.classList.toggle("open", o); mb.setAttribute("aria-expanded", String(o)); });
    document.getElementById("dsDrawerBtn").addEventListener("click", function(){ document.documentElement.classList.add("ds-drawer"); });
    document.getElementById("dsScrim").addEventListener("click", function(){ document.documentElement.classList.remove("ds-drawer"); });
    document.getElementById("dsCollapse").addEventListener("click", function(){ var c = !document.documentElement.classList.contains("ds-side-collapsed"); savePrefs({sidebar:c ? "collapsed" : "expanded"}); setTimeout(function(){ window.dispatchEvent(new Event("resize")); }, 260); });
    document.body.addEventListener("click", function(e){ if(e.target.closest("#dsSignOut")) signOut(); });
  }

  /* ------------------------------------------------------------------ live status (read from the server; never invented) */
  var STATUS = null;
  function hms(iso){ if(!iso) return "\u2013"; var d = new Date(iso); if(isNaN(d)) return "\u2013"; return fmtTime(d, true); }
  function fmtTime(d, sec){
    var p = prefs(), o = {timeZone:"Asia/Singapore", hour:"2-digit", minute:"2-digit", hour12:p.timeFormat === "12"}; if(sec) o.second = "2-digit";
    return new Intl.DateTimeFormat("en-GB", o).format(d);
  }
  function api(url, opt){
    var c = new AbortController(), t = setTimeout(function(){ c.abort(); }, (opt && opt.timeout) || 20000);
    return fetch(url, Object.assign({signal:c.signal, credentials:"same-origin"}, opt || {})).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){ if(!r.ok && !j.error) j.error = "HTTP " + r.status; j._status = r.status; return j; });
    }).catch(function(e){ return {error:e.name === "AbortError" ? "Timed out" : "Server not reachable", _status:0}; }).finally(function(){ clearTimeout(t); });
  }
  function summarise(s){
    if(!s || s.error || !s.feeds) return {cls:"bad", txt:"OFFLINE", title:"The server status could not be read" + (s && s.error ? " (" + s.error + ")" : "")};
    if(s.config && s.config.httpx_ok === false) return {cls:"bad", txt:"DEPLOY PROBLEM", title:s.config.problem || "A stray httpx.py next to app.py blocks every live feed: delete it and redeploy."};
    if(s.config && !s.config.lta_key) return {cls:"bad", txt:"NO LTA KEY", title:"LTA_ACCOUNT_KEY is not set on the server: live bus and traffic feeds cannot load."};
    var all = s.feeds.concat(s.engines || []), ok = all.filter(function(f){ return f.status === "ok"; }).length, bad = all.filter(function(f){ return f.status === "error"; }).length, stale = all.filter(function(f){ return f.status === "stale"; }).length;
    if(bad) return {cls:"warn", txt:"DEGRADED", title:bad + " feed(s) returning errors \u2014 see Settings \u203a Data & Refresh"};
    if(ok) return {cls:stale ? "warn" : "ok", txt:"LIVE", title:ok + " feed(s) current" + (stale ? ", " + stale + " stale" : "")};
    return {cls:"", txt:"STANDBY", title:"Connected. Feeds load when a module requests them."};
  }
  function status(){
    return api("/api/system/status").then(function(s){
      STATUS = s; var m = summarise(s), el = document.getElementById("dsLive");
      if(el){ el.className = "ds-live " + m.cls; el.title = m.title; document.getElementById("dsLiveT").textContent = m.txt; }
      var ft = document.getElementById("dsFreshT"); if(ft) ft.textContent = s && s.newest ? hms(s.newest) : "\u2013";
      var sd = document.getElementById("dsSideDot"), st = document.getElementById("dsSideTxt");
      if(sd){ sd.className = "ds-dot " + (m.cls === "ok" ? "ok" : m.cls === "warn" ? "warn" : m.cls === "bad" ? "bad" : ""); st.textContent = m.cls === "ok" ? "System operational" : m.cls === "" ? "Connected \u00b7 standby" : m.title.split(":")[0].split(" \u2014 ")[0]; }
      document.dispatchEvent(new CustomEvent("ds:status", {detail:s}));
      return s;
    });
  }
  var SEV_ORDER = {critical:0, warning:1, info:2};
  function notes(){
    return api("/api/system/notifications").then(function(j){
      var list = document.getElementById("dsNoteList"), badge = document.getElementById("dsBadge"); if(!list) return j;
      if(j.error){ list.innerHTML = '<div class="ds-note-empty">Notifications unavailable: ' + esc(j.error) + '</div>'; badge.hidden = true; return j; }
      var p = prefs(), items = (j.items || []).slice();
      var sm = summarise(STATUS);                                       // data-feed interruption, from the same live status as the command bar
      if(STATUS && (sm.cls === "bad" || sm.cls === "warn")) items.unshift({id:"feed", kind:"feed", severity:sm.cls === "bad" ? "critical" : "warning", title:"Data feeds \u00b7 " + sm.txt, detail:sm.title, acked:false, href:"/settings#data"});
      items = items.filter(function(x){ var k = x.kind === "gap" ? "longgap" : x.kind; return p["note_" + k] !== false && p["sev_" + x.severity] !== false; });
      var n = items.filter(function(x){ return !x.acked; }).length;
      badge.hidden = !n; badge.textContent = n > 99 ? "99+" : String(n);
      if(n > (notes._last || 0) && notes._last != null && p.sound) beep();
      notes._last = n;
      list.innerHTML = items.length ? items.sort(function(a, b){ return (a.acked - b.acked) || (SEV_ORDER[a.severity] - SEV_ORDER[b.severity]); }).map(function(x){
        return '<a class="ds-note-item' + (x.acked ? " acked" : "") + '" href="' + esc(x.href || "#") + '">' + sev(x.severity) + '<div><b>' + esc(x.title) + '</b><small>' + esc(x.detail || "") + (x.acked ? " \u00b7 acknowledged" : "") + '</small></div></a>';
      }).join("") : '<div class="ds-note-empty">No active alerts. The collectors raise bunching, long-gap and high-priority traffic alerts here.</div>';
      return j;
    });
  }
  function beep(){ try{ var a = new (window.AudioContext || window.webkitAudioContext)(), o = a.createOscillator(), g = a.createGain(); o.frequency.value = 880; g.gain.value = .04; o.connect(g); g.connect(a.destination); o.start(); o.stop(a.currentTime + .12); }catch(e){} }
  function session(){
    return api("/api/auth/session").then(function(s){
      AUTH = {enabled:!!s.enabled, user:s.user || null, since:s.since};
      var p = prefs(), nm = (s.user && s.user.display_name) || p.displayName || "Controller";
      var av = document.getElementById("dsAv"); if(av) av.innerHTML = avatar(nm, p.avatar);
      var w = document.getElementById("dsWho"); if(w) w.textContent = nm.split(" ")[0];
      var pp = document.getElementById("dsProfile"); if(pp) pp.innerHTML = profileHTML(s.user);
      document.dispatchEvent(new CustomEvent("ds:session", {detail:AUTH}));
      return AUTH;
    });
  }
  function signOut(){
    var go = function(){ location.href = "/login"; };
    if(AUTH.enabled) api("/api/auth/logout", {method:"POST"}).then(go); else go();
  }

  /* ------------------------------------------------------------------ shared UI components for pages */
  function sev(level, label){
    var map = {critical:["crit", "CRITICAL"], bad:["crit", "CRITICAL"], warning:["warn", "WARNING"], warn:["warn", "ATTENTION"], info:["info", "INFO"], ok:["ok", "HEALTHY"], ai:["ai", "AI"], off:["off", "INACTIVE"]};
    var m = map[level] || map.info; return '<span class="ds-sev ' + m[0] + '">' + esc(label || m[1]) + '</span>';
  }
  function empty(o){
    o = o || {};
    return '<div class="ds-empty"><span class="ic">' + icon(o.icon || "inbox") + '</span><b>' + esc(o.title || "NOTHING TO SHOW") + '</b><p>' + esc(o.text || "") + '</p>'
      + (o.action ? '<a class="ds-btn sm" href="' + esc(o.href || "#") + '"' + (o.onclick ? ' onclick="' + esc(o.onclick) + '"' : "") + '>' + esc(o.action) + '</a>' : "") + '</div>';
  }
  var DEFAULT_STEPS = ["Checking current headways\u2026", "Evaluating recovery options\u2026", "Simulating downstream trips\u2026", "Calculating EWT impact\u2026"];
  function loading(el, steps, title){
    steps = steps || DEFAULT_STEPS; if(typeof el === "string") el = document.getElementById(el); if(!el) return {done:function(){}, stop:function(){}};
    el.innerHTML = '<div class="ds-load" role="status" aria-live="polite"><b>' + esc(title || "ANALYSING NETWORK STATE") + '</b><div class="bar"><i></i></div><ol>' + steps.map(function(s){ return "<li>" + esc(s) + "</li>"; }).join("") + '</ol></div>';
    var lis = el.querySelectorAll("li"), i = 0; if(lis[0]) lis[0].className = "on";
    var t = setInterval(function(){ if(i < lis.length - 1){ lis[i].className = "done"; lis[++i].className = "on"; } }, 1400);   // paced display only: the steps describe what the server is doing, not a measured progress
    return {stop:function(){ clearInterval(t); }, done:function(){ clearInterval(t); lis.forEach(function(l){ l.className = "done"; }); }};
  }
  // AI OPERATIONAL INSIGHT — compact decision-support module (never a floating chatbot). Every field must come from an engine result.
  function ai(o){
    o = o || {};
    var row = function(k, v){ return v ? '<dt>' + esc(k) + '</dt><dd>' + v + '</dd>' : ""; };
    return '<section class="ds-ai' + (o.thinking ? " thinking" : "") + '" aria-label="AI operational insight"><div class="ds-ai-h">' + icon("spark") + '<span>' + esc(o.title || "AI OPERATIONAL INSIGHT") + '</span><span class="sp"></span>' + (o.badge || "") + '</div>'
      + '<dl>' + row("DETECTED", o.detected) + row("CAUSE", o.cause) + row("RECOMMENDATION", o.recommendation) + row("EXPECTED EFFECT", o.effect) + '</dl>'
      + '<div class="ds-ai-f">' + (o.href ? '<a class="ds-btn ai sm" href="' + esc(o.href) + '">' + esc(o.action || "Open analysis") + '</a>' : "") + (o.basis ? '<span class="basis">' + esc(o.basis) + '</span>' : "") + '</div></section>';
  }
  function countUp(el, to, ms){
    if(typeof el === "string") el = document.getElementById(el); if(!el) return;
    if(to == null || isNaN(to)){ el.textContent = "\u2013"; return; }
    if(prefs().reduceMotion || document.documentElement.classList.contains("ds-reduce-motion")){ el.textContent = String(to); return; }
    var from = +el.getAttribute("data-v") || 0, t0 = performance.now(); ms = ms || 320; el.setAttribute("data-v", to);
    (function f(t){ var k = Math.min(1, (t - t0) / ms), v = Math.round(from + (to - from) * (1 - Math.pow(1 - k, 3))); el.textContent = String(v); if(k < 1) requestAnimationFrame(f); })(t0);
  }

  window.DS = {modules:MODULES.filter(function(m){ return !hid(m); }), allModules:MODULES, hiddenIds:hiddenIds, blockOn:blockOn, groups:GROUPS, module:mod, active:ACTIVE, icon:icon, esc:esc, api:api, prefs:prefs, savePrefs:savePrefs, defaults:DEFAULT_PREFS,
               status:function(){ return STATUS; }, refreshStatus:status, refreshNotes:notes, session:function(){ return AUTH; }, refreshSession:session,
               sev:sev, empty:empty, loading:loading, ai:ai, countUp:countUp, fmtTime:fmtTime, summarise:summarise};
  if(document.body) mount(); else document.addEventListener("DOMContentLoaded", mount);
})();
