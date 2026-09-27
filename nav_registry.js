/* SG Transport Pulse — central navigation + shared OCC shell.
   ONE registry drives sidebar, launcher, shortcuts and mobile navigation. */
(function(){
  var MODULES = [
    {id:"command",name:"Command Centre",icon:"⌂",route:"/command",group:"COMMAND",mobile:true,shortcut:true,enabled:true},
    {id:"route",name:"Route Traffic",icon:"△",route:"/",group:"COMMAND",mobile:true,shortcut:true,enabled:true},
    {id:"headway",name:"Headway Control",icon:"↔",route:"/control",group:"COMMAND",mobile:true,shortcut:true,enabled:true},
    {id:"bunching",name:"Bunching & Gap",icon:"≋",route:"/bunching",group:"COMMAND",mobile:true,shortcut:true,enabled:true},
    {id:"recovery",name:"Recovery Decision Engine",icon:"◫",route:"/halfway/timetable",group:"RECOVERY",mobile:true,shortcut:true,enabled:true},
    {id:"halfplan",name:"Halfway Planner",icon:"½",route:"/halfway",group:"RECOVERY",mobile:true,shortcut:true,enabled:true},
    {id:"trafficaware",name:"Traffic-Aware",icon:"⚠",route:"/traffic",group:"RECOVERY",mobile:true,shortcut:true,enabled:true},
    {id:"running",name:"Running Time Analytics",icon:"◷",route:"/running-time",group:"ANALYTICS",mobile:true,shortcut:true,enabled:true},
    {id:"cameras",name:"Traffic Cameras",icon:"▣",route:"/cameras",group:"ANALYTICS",mobile:true,shortcut:true,enabled:true},
    {id:"settings",name:"Settings",icon:"⚙",route:"/settings",group:"SYSTEM",mobile:true,shortcut:true,enabled:true}
  ], GROUP_ORDER=["COMMAND","RECOVERY","ANALYTICS","SYSTEM"];
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
  function byPath(){var p=location.pathname;if(p==="/command")return"command";if(p==="/control")return"headway";if(p==="/bunching")return"bunching";if(p==="/halfway/timetable")return"recovery";if(p==="/halfway"||p==="/halfway/os")return"halfplan";if(p==="/traffic")return"trafficaware";if(p==="/running-time"||p==="/insight")return"running";if(p==="/cameras")return"cameras";if(p==="/settings")return"settings";return"route"}
  function ensureShell(){
    if(!document.querySelector(".desktop-shell")){var a=document.createElement("aside");a.className="desktop-shell";a.setAttribute("aria-label","Primary navigation");a.innerHTML='<div class="shell-brand"><strong>SG TRANSPORT PULSE</strong><small>Operational Intelligence Platform</small></div><div id="dsSidebar"></div><div class="shell-foot"><span class="shell-live-dot"></span> Live operational workspace</div>';document.body.insertBefore(a,document.body.firstChild)}
    if(!document.getElementById("dsMobileBottom")){var n=document.createElement("nav");n.className="mobile-bottom-nav";n.id="dsMobileBottom";document.body.appendChild(n);var m=document.createElement("div");m.className="mobile-more";m.id="dsMobileMore";document.body.appendChild(m)}
    if(!document.getElementById("dsMobileTop")){var mt=document.createElement("div");mt.className="mobile-top-menu";mt.id="dsMobileTop";document.body.appendChild(mt)}
  }
  function ensureCmdbar(){
    if(document.querySelector(".cmdbar"))return;
    var host=document.querySelector(".hdr-right")||document.querySelector(".top .sp")||document.querySelector(".top");if(!host)return;
    var w=document.createElement("div");w.className="cmdbar";
    w.innerHTML='<div class="ds-status"><span class="ds-live-dot"></span><b>LIVE</b><small id="dsFresh">Checking data…</small></div><div class="cmdwrap"><button class="cmdbtn" type="button" title="Notifications">♢<span class="dot" hidden></span></button></div><div class="cmdwrap"><button class="cmdbtn" type="button" data-ds-toggle="dsLauncher" title="Apps">▦</button><div class="launcher" id="dsLauncher"><h4>APPLICATIONS</h4><div class="launcher-grid" id="dsLauncherGrid"></div></div></div><a class="cmdbtn" href="/settings" title="Settings">⚙</a><div class="cmdwrap"><button class="profilechip" type="button" data-ds-toggle="dsProfile"><span class="av">KS</span><span>KS</span><span>⌄</span></button><div class="dropdown" id="dsProfile"><a href="/settings#account">Profile</a><a href="/settings#appearance">Preferences</a><a href="/settings">Settings</a><a href="/login">Sign out</a></div></div>';
    host.appendChild(w)
  }
  function renderSidebar(a){var e=document.getElementById("dsSidebar");if(!e)return;var h="";GROUP_ORDER.forEach(function(g){h+='<div class="shell-group-label">'+g+"</div>";MODULES.filter(function(m){return m.group===g}).forEach(function(m){h+='<a class="shell-link'+(m.id===a?" active":"")+'" href="'+m.route+'"><span class="si">'+m.icon+"</span><span>"+esc(m.name)+"</span></a>"})});e.innerHTML=h}
  function renderLauncher(a){var e=document.getElementById("dsLauncherGrid");if(e)e.innerHTML=MODULES.filter(function(m){return m.shortcut}).map(function(m){return '<a class="launcher-item'+(m.id===a?" active":"")+'" href="'+m.route+'"><span class="li-ic">'+m.icon+"</span><span>"+esc(m.name)+"</span></a>"}).join("")}
  function renderMobile(a){var b=document.getElementById("dsMobileBottom"),more=document.getElementById("dsMobileMore");if(!b||!more)return;var ids=["command","route","headway","recovery"],labels={command:"HOME",route:"TRAFFIC",headway:"CONTROL",recovery:"RECOVERY"};b.innerHTML=ids.map(function(id){var m=MODULES.find(function(x){return x.id===id});return '<a class="mbn'+(id===a?" active":"")+'" href="'+m.route+'"><span class="mi">'+m.icon+"</span>"+labels[id]+"</a>"}).join("")+'<button class="mbn" type="button" id="dsMoreBtn"><span class="mi">•••</span>MORE</button>';more.innerHTML=MODULES.filter(function(m){return ids.indexOf(m.id)<0&&m.mobile}).map(function(m){return '<a class="'+(m.id===a?"active":"")+'" href="'+m.route+'">'+m.icon+" "+esc(m.name)+"</a>"}).join("");document.getElementById("dsMoreBtn").onclick=function(){more.classList.toggle("open")}}
  function shortcuts(a){var e=document.querySelector("[data-ds-shortcuts]");if(e)e.innerHTML=MODULES.filter(function(m){return m.shortcut&&m.id!==a}).slice(0,6).map(function(m){return '<a class="ds-shortcut" href="'+m.route+'"><span>'+m.icon+"</span><b>"+esc(m.name)+"</b></a>"}).join("")}
  function wire(){document.querySelectorAll("[data-ds-toggle]").forEach(function(b){b.onclick=function(ev){ev.stopPropagation();var p=document.getElementById(b.getAttribute("data-ds-toggle"));if(!p)return;var was=p.classList.contains("open");document.querySelectorAll(".dropdown.open,.launcher.open").forEach(function(x){x.classList.remove("open")});if(!was)p.classList.add("open")}});document.addEventListener("click",function(){document.querySelectorAll(".dropdown.open,.launcher.open").forEach(function(x){x.classList.remove("open")})})}
  function health(){fetch("/api/health",{cache:"no-store"}).then(function(r){return r.json()}).then(function(j){var e=document.getElementById("dsFresh");if(e)e.textContent=j.lta?"Data live · "+String(j.time||"").slice(11,19):"Operational UI · LTA key not configured"}).catch(function(){})}
  window.DS_NAV={modules:MODULES,init:function(a){ensureShell();ensureCmdbar();a=a||byPath();renderSidebar(a);renderLauncher(a);renderMobile(a);shortcuts(a);wire();health()}};
  document.addEventListener("DOMContentLoaded",function(){if(!window.__DS_NO_AUTO__)window.DS_NAV.init(byPath())});
})();