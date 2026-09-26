/* SG Transport Pulse - shared basemap (V14.1)
   CARTO raster basemaps (keyed), then OneMap, then OpenStreetMap if tiles keep failing, so a map is never blank.
   Served by the backend at /basemap.js with the CARTO key filled in from the CARTO_API_KEY environment variable.
   Usage:  SGBasemap.add(map, {style:"voyager" | "dark" | "light" | "onemap", minZoom, maxZoom, switcher:true})
   The controller's choice (map button under the zoom buttons) is remembered in this browser for every page. */
(function(){
  "use strict";
  var KEY = "__CARTO_KEY__";
  var ORDER = ["voyager", "dark", "light", "onemap"];
  var STYLES = {
    voyager: {label:"CARTO Voyager", carto:"voyager", onemap:"Default"},
    dark:    {label:"CARTO Dark", carto:"dark_all", onemap:"Night"},
    light:   {label:"CARTO Light", carto:"light_all", onemap:"Grey"},
    onemap:  {label:"OneMap", carto:null, onemap:"Default"}
  };
  var Y = new Date().getFullYear();
  var A_CARTO = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions" target="_blank" rel="noopener">CARTO</a>';
  var A_OM = '<a href="https://www.onemap.gov.sg/" target="_blank" rel="noopener">OneMap</a> | &copy; ' + Y + ' Singapore Land Authority';
  var A_OSM = 'Basemap &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

  function stored(){ try{ return localStorage.getItem("basemap.style"); }catch(e){ return null; } }
  function remember(s){ try{ localStorage.setItem("basemap.style", s); }catch(e){} }
  function sources(style){
    var st = STYLES[style] || STYLES.voyager, out = [];
    if(st.carto) out.push({url:"https://basemaps.cartocdn.com/rastertiles/" + st.carto + "/{z}/{x}/{y}{r}.png" + (KEY ? "?key=" + encodeURIComponent(KEY) : ""), attr:A_CARTO, label:st.label});
    out.push({url:"https://www.onemap.gov.sg/maps/tiles/" + st.onemap + "/{z}/{x}/{y}.png", attr:A_OM, label:"OneMap " + st.onemap + (st.carto ? " (CARTO unavailable)" : "")});
    out.push({url:"https://tile.openstreetmap.org/{z}/{x}/{y}.png", attr:A_OSM + " (CARTO / OneMap unavailable)", label:"OpenStreetMap"});
    return out;
  }

  function add(map, opt){
    opt = opt || {};
    var minZ = opt.minZoom != null ? opt.minZoom : 10, maxZ = opt.maxZoom != null ? opt.maxZoom : 19;
    var st = {style:stored() || opt.style || "voyager", source:null, layer:null}, gen = 0;
    if(!STYLES[st.style]) st.style = "voyager";
    function mount(style){
      var list = sources(style), i = 0, my = ++gen;
      if(st.layer){ map.removeLayer(st.layer); st.layer = null; }
      (function next(){
        if(my !== gen || i >= list.length) return;
        var s = list[i++], ok = 0, bad = 0, gone = false;
        var lyr = L.tileLayer(s.url, {minZoom:minZ, maxZoom:maxZ, attribution:s.attr, crossOrigin:false});
        lyr.on("tileload", function(){ ok++; });
        lyr.on("tileerror", function(){ bad++; if(!gone && ok === 0 && bad >= 4){ gone = true; if(map.hasLayer(lyr)) map.removeLayer(lyr); next(); } });
        lyr.addTo(map); if(lyr.bringToBack) lyr.bringToBack();
        st.layer = lyr; st.source = s.label; if(btn) btn.title = "Basemap: " + s.label + " \u2013 click to change";
      })();
    }
    var btn = null;
    if(opt.switcher !== false && L.Control){
      var Ctl = L.Control.extend({options:{position:opt.position || "topleft"}, onAdd:function(){
        var box = L.DomUtil.create("div", "leaflet-bar sgbm");
        btn = L.DomUtil.create("a", "", box); btn.href = "#"; btn.setAttribute("role", "button"); btn.setAttribute("aria-label", "Change basemap");
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" style="vertical-align:middle"><path fill="currentColor" d="M9 3 3 5.4v15.6l6-2.4 6 2.4 6-2.4V3l-6 2.4zm1 2.3 4 1.6v11.8l-4-1.6zM5 6.8l3-1.2v11.8l-3 1.2zm11 .1 3-1.2v11.8l-3 1.2z"/></svg>';
        L.DomEvent.on(btn, "click", function(e){ L.DomEvent.stop(e); var k = (ORDER.indexOf(st.style) + 1) % ORDER.length; st.style = ORDER[k]; remember(st.style); mount(st.style); flash(map, STYLES[st.style].label); });
        L.DomEvent.disableClickPropagation(box);
        return box;
      }});
      new Ctl().addTo(map);
    }
    mount(st.style);
    return {state:st, setStyle:function(s){ if(STYLES[s]){ st.style = s; mount(s); } }};
  }
  function flash(map, text){
    var c = map.getContainer(), el = c.querySelector(".sgbm-flash");
    if(!el){ el = document.createElement("div"); el.className = "sgbm-flash"; el.style.cssText = "position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:1200;background:rgba(6,17,32,.92);color:#fff;border:1px solid #1f4468;border-radius:10px;padding:8px 14px;font:600 13px/1.3 system-ui,sans-serif;pointer-events:none"; c.appendChild(el); }
    el.textContent = "Basemap: " + text; el.style.display = "block"; clearTimeout(el._t); el._t = setTimeout(function(){ el.style.display = "none"; }, 1400);
  }
  window.SGBasemap = {add:add, styles:STYLES};
})();
