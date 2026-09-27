/* SG Transport Pulse — central navigation registry (V1)
   ONE list drives the desktop sidebar, the App Launcher, the mobile top menu
   and the mobile bottom bar / More sheet, so they can never disagree.
   A page includes this file, then calls DS_NAV.init("<module-id>"). */
(function(){
  var MODULES = [
    {id:"command",  name:"Command Centre",            icon:"\u25A3", route:"/command",           group:"COMMAND",   mobile:true,  shortcut:true, enabled:false},
    {id:"route",    name:"Route Traffic",              icon:"\u25B0", route:"/",                  group:"COMMAND",   mobile:true,  shortcut:true, enabled:true},
    {id:"headway",  name:"Headway Control",            icon:"\u2194", route:"/control",           group:"COMMAND",   mobile:true,  shortcut:true, enabled:true},
    {id:"bunching", name:"Bunching & Gap",              icon:"\u224B", route:"/bunching",          group:"COMMAND",   mobile:true,  shortcut:true, enabled:true},
    {id:"recovery", name:"Recovery Decision Engine",    icon:"\u25A4", route:"/halfway/timetable", group:"RECOVERY",  mobile:true,  shortcut:true, enabled:true},
    {id:"halfplan", name:"Halfway Planner",             icon:"\u00BD", route:"/halfway",           group:"RECOVERY",  mobile:true,  shortcut:true, enabled:true},
    {id:"trafficaware", name:"Traffic-Aware Regulation", icon:"\u26A0", route:"/traffic",          group:"RECOVERY",  mobile:true,  shortcut:true, enabled:true},
    {id:"running",  name:"Running Time Analytics",      icon:"\u23F1", route:"/running-time",      group:"ANALYTICS", mobile:true,  shortcut:true, enabled:true},
    {id:"cameras",  name:"Traffic Cameras",             icon:"\u{1F4F7}", route:"/cameras",         group:"ANALYTICS", mobile:true,  shortcut:true, enabled:true},
    {id:"settings", name:"Settings",                    icon:"\u2699", route:"/settings",          group:"SYSTEM",    mobile:false, shortcut:false, enabled:false}
  ];
  var GROUP_ORDER = ["COMMAND","RECOVERY","ANALYTICS","SYSTEM"];

  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}

  function renderSidebar(activeId){
    var el = document.getElementById("dsSidebar"); if(!el) return;
    var html = "";
    GROUP_ORDER.forEach(function(g){
      var items = MODULES.filter(function(m){return m.group===g});
      if(!items.length) return;
      html += '<div class="shell-group-label">'+g+'</div>';
      items.forEach(function(m){
        if(!m.enabled){
          html += '<span class="shell-link" style="opacity:.4;cursor:default" title="Coming soon"><span class="si">'+m.icon+'</span><span>'+esc(m.name)+'</span></span>';
        } else {
          html += '<a class="shell-link'+(m.id===activeId?' active':'')+'" href="'+m.route+'"><span class="si">'+m.icon+'</span><span>'+esc(m.name)+'</span></a>';
        }
      });
    });
    el.innerHTML = html;
  }

  function renderLauncher(activeId){
    var el = document.getElementById("dsLauncherGrid"); if(!el) return;
    var html = "";
    MODULES.filter(function(m){return m.shortcut}).forEach(function(m){
      var cls = "launcher-item"+(m.id===activeId?" active":"")+(m.enabled?"":" disabled");
      var tag = m.enabled ? "a" : "span";
      html += "<"+tag+' class="'+cls+'" '+(m.enabled?('href="'+m.route+'"'):'')+'><span class="li-ic">'+m.icon+'</span><span>'+esc(m.name)+'</span></'+tag+'>';
    });
    el.innerHTML = html;
  }

  function renderMobileTop(activeId){
    var el = document.getElementById("dsMobileTop"); if(!el) return;
    var html = "";
    MODULES.filter(function(m){return m.mobile && m.enabled}).forEach(function(m){
      html += '<a class="'+(m.id===activeId?'active':'')+'" href="'+m.route+'">'+m.icon+' '+esc(m.name)+'</a>';
    });
    el.innerHTML = html;
  }

  // First 3 modules (excluding "more") become the bottom bar; rest go in "More".
  function renderMobileBottom(activeId){
    var bar = document.getElementById("dsMobileBottom"), more = document.getElementById("dsMobileMore");
    if(!bar && !more) return;
    var primary = ["route","headway","bunching","recovery"];
    var mods = MODULES.filter(function(m){return m.mobile && m.enabled});
    var head = primary.map(function(id){return mods.find(function(m){return m.id===id})}).filter(Boolean);
    var rest = mods.filter(function(m){return primary.indexOf(m.id)===-1});
    if(bar){
      var h = "";
      head.forEach(function(m){
        h += '<a class="mbn'+(m.id===activeId?' active':'')+'" href="'+m.route+'"><span class="mi">'+m.icon+'</span>'+esc(m.name.split(" ")[0])+'</a>';
      });
      h += '<button class="mbn" type="button" style="border:0;background:transparent;font-family:inherit" onclick="document.getElementById(\'dsMobileMore\').classList.toggle(\'open\')"><span class="mi">\u2022\u2022\u2022</span>More</button>';
      bar.innerHTML = h;
    }
    if(more){
      var m2 = "";
      rest.forEach(function(m){
        m2 += '<a class="'+(m.id===activeId?'active':'')+'" href="'+m.route+'">'+m.icon+' '+esc(m.name)+'</a>';
      });
      more.innerHTML = m2;
    }
  }

  function wireCmdbar(){
    document.querySelectorAll("[data-ds-toggle]").forEach(function(btn){
      btn.addEventListener("click", function(e){
        e.stopPropagation();
        var id = btn.getAttribute("data-ds-toggle");
        var panel = document.getElementById(id);
        if(!panel) return;
        var was = panel.classList.contains("open");
        document.querySelectorAll(".dropdown.open,.launcher.open").forEach(function(p){p.classList.remove("open")});
        if(!was) panel.classList.add("open");
      });
    });
    document.addEventListener("click", function(){
      document.querySelectorAll(".dropdown.open,.launcher.open").forEach(function(p){p.classList.remove("open")});
    });
  }

  window.DS_NAV = {
    modules: MODULES,
    init: function(activeId){
      renderSidebar(activeId);
      renderLauncher(activeId);
      renderMobileTop(activeId);
      renderMobileBottom(activeId);
      wireCmdbar();
    }
  };
})();
