import {HORIZONS,COLORS,TITLES,esc,num,apiURL,quality,riskServices,slicePage,badge,icon,chartSVG,operationalQueue,initialView,loadSummary,stopText,watchLocation,headwayView,toggleService,resetFilters,hourlyHeadway,hourlyChart,spotlightDeck,categoryView} from './forecast2026-core.js?v=20261028-hz19';
import {ForecastMap,LAYERS} from './forecast2026-map.js?v=20261028-hz19';
import {boardModel,loadingCard,trafficCard,headwayCard,emptyState,paginate} from './forecast2026-tv.js?v=20261028-hz19';
const $=id=>document.getElementById(id),query=new URLSearchParams(location.search);
const initial=initialView(query,{mode:document.documentElement.dataset.defaultMode||'live',horizon:document.documentElement.dataset.defaultHorizon||0});
if(initial.h===120)initial.h=60;   // old links to the retired NEXT 2H view open the 60-minute view
const state={...initial,scenario:query.get('scenario')||'combined',tick:0,services:[],exclude:false,category:'all',tv:query.get('present')==='1',tab:'active',page:0,playing:false,search:'',catalog:[],operator:'',headwayScope:'network',headwayDirection:'all',headwayInterval:60,analysis:'headway',delayMin:5,data:null};
let requestId=0,controller=null,playTimer=null,lastFocus=null,modalFocus=null,cameraTime=0,cameraPoint=null,cameraRows=[],cameraMeta={},cameraRequest=0,headwayData=null; 
const spotlight={enabled:query.get('tour')!=='0',current:null,step:-1,cursor:0,deck:[],nextAt:0,pausedUntil:0,baseAnalysis:'headway',baseHeadway:{scope:'network',direction:'all'}};
const networkMap=new ForecastMap($('networkMap'),(svc,point)=>point?loadCameras(point):selectService(svc));
const timeString=(timestamp)=>new Date(timestamp*1000).toLocaleTimeString('en-SG',{timeZone:'Asia/Singapore',hour:'2-digit',minute:'2-digit'});
function clock(){let d=new Date();$('clock').textContent=d.toLocaleTimeString('en-SG',{timeZone:'Asia/Singapore',hour12:false});$('date').textContent=d.toLocaleDateString('en-SG',{timeZone:'Asia/Singapore',weekday:'short',day:'numeric',month:'short',year:'numeric'});if(state.data)updateStatus();if(state.tv)tickSpotlight();}
// Which data sources have nothing at the selected horizon, and why (shown on the status chip instead of a vague "degraded").
function missingSources(d){const h=state.h,why={loading:h>0?'Passenger loading: observed only at NOW, not forecast':'Passenger loading: no LTA load readings yet',
 traffic:'Traffic: incident / speed feed not live',bunching:h>30?'Three-bus bunching: forecast covers 30 min':'Three-bus bunching: headway collector has no data',gap:h>30?'Long gaps: forecast covers 30 min':'Long gaps: headway collector has no data'};
 return Object.keys(d.coverage||{}).filter(k=>!d.coverage[k]).map(k=>why[k]||k);}
function updateStatus(){let d=state.data,miss=missingSources(d),q=quality(d.mode,d.updated,Date.now()/1000,!!d.error);
 let label=q.kind==='live'?(state.h===0?'LIVE · OBSERVED':'LIVE · ESTIMATE'):q.label,total=Object.keys(d.coverage||{}).length||4;
 if(q.kind==='live'&&miss.length)label+=' · '+(total-miss.length)+'/'+total+' SOURCES';
 $('systemStatus').textContent=label;$('systemStatus').className='status '+(q.kind==='live'&&miss.length?'delayed':q.kind);
 $('systemStatus').title=(d.error?'Data error: '+d.error+'\n':'')+(miss.length?'No data at this time for:\n· '+miss.join('\n· '):'All four data sources available');let age=Math.max(0,Math.round(Date.now()/1000-d.updated));$('mapFreshness').textContent=(d.mode==='demo'?'SIMULATED · ':age>150?'DELAYED · ':'OBSERVED · ')+timeString(d.updated)+' SGT';}
function controls(){
 networkMap.setSelection(state.services);
 $('timeline').innerHTML=HORIZONS.filter(h=>h!==120).map(h=>`<button data-h="${h}" aria-pressed="${state.h===h}">${h===0?'NOW':h===120?'NEXT 2H':'NEXT '+h+'M'}</button>`).join('');
 document.querySelectorAll('[data-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.mode===state.mode);b.setAttribute('aria-pressed',b.dataset.mode===state.mode);});
 document.querySelectorAll('[data-category]').forEach(b=>{b.classList.toggle('active',b.dataset.category===state.category);b.setAttribute('aria-pressed',b.dataset.category===state.category);});
 $('demoControls').hidden=state.mode!=='demo';$('scenario').value=state.scenario;$('excludeCBS').checked=state.exclude;
 $('serviceLabel').textContent=state.services.length?state.services.join(', '):'All services';
 $('selectedServices').innerHTML=state.services.length?state.services.map(s=>`<button class="service-chip" data-remove-service="${esc(s)}" aria-label="Remove service ${esc(s)}">Service ${esc(s)} <span aria-hidden="true">×</span></button>`).join(''):'<span class="network-chip">FULL NETWORK</span>';
 $('clearService').disabled=!state.services.length;
 if($('delayThreshold').value!=='custom')$('delayThreshold').value=String(state.delayMin);
 $('delayBasis').textContent=state.mode==='demo'?'DEMO · simulated delays':'Provider-reported / free-flow baseline · typical history where available';
 spotlightControl();
 $('mapLayers').innerHTML=Object.entries(LAYERS).map(([key,label])=>`<label><input type="checkbox" data-layer="${key}" ${networkMap.flags[key]?'checked':''}>${label}</label>`).join('');
}
function kpiCards(d){
 $('riskCards').innerHTML=Object.entries(TITLES).map(([cat,title])=>{let k=d.kpis[cat],value=k.available?num(k.count):'—',detail=cat==='loading'?(k.occupancy!=null?num(k.occupancy)+'% peak':d.horizon?'Demand history needed':'Categorical load readings'):cat==='traffic'?(k.delay!=null?'+'+num(k.delay,1)+' min avg':'Route-specific impact'):cat==='bunching'?`3BB · headway < ${num(d.rules?.bunch_min??3,0)} min`:`Scheduled + ${num(d.rules?.gap_add_min??10,0)} min`;return `<button class="risk-card ${state.category===cat?'active':''} ${k.available?(k.count>0?'hot':'clear'):''}" data-category="${cat}" style="--risk:${COLORS[cat]}" aria-pressed="${state.category===cat}"><span class="risk-icon">${icon(cat)}</span><span><h2 title="${title}">${title.replace('passenger ','')}</h2><strong>${value}</strong><small>${k.available?detail:'Not available at this horizon'}</small></span><span class="risk-aside"><b>${k.available?num(k.services):'—'}</b>services<small>${d.mode==='demo'?'SIMULATED':d.horizon?'ESTIMATED':'OBSERVED'}</small></span></button>`;}).join('');
}
function networkStatus(d){
 let headways=d.services.map(s=>s.focus_headway).filter(v=>v!=null),avg=headways.length?headways.reduce((a,b)=>a+b,0)/headways.length:null;
 let heavy=d.kpis.loading.occupancy,avail=Object.values(d.coverage).filter(Boolean).length;
 $('networkMode').textContent=d.mode==='demo'?'SIMULATED':'SOURCE DATA';
 const row=(i,label,value,aside='')=>`<div class="network-row">${icon(i)}<div><span>${label}</span><strong>${value}</strong></div><div class="right">${aside}</div></div>`;
 $('networkStatus').innerHTML=row('bus',state.tv?'Sampled buses':'Buses with positions',num(d.buses.length),`<small>${d.mode==='demo'?'DEMO':'Sampled'}<br>not fleet total</small>`)+row('loading',heavy==null?(state.tv?'Passenger load':'Passenger-load coverage'):(state.tv?'Peak load':'Peak forecast occupancy'),heavy==null?(d.coverage.loading?(state.tv?'LTA bands':'Categorical'):(state.tv?'—':'Unavailable')):num(heavy)+'%',heavy!=null?`<span class="tag estimated">ESTIMATE</span>`:'')+row('spark',state.tv?'Risk engines':'Risk engines with data',`${avail} / 4`,`<small>At selected<br>horizon</small>`)+row('clock',state.tv?'Mean ETA pair':state.h?'Mean forecast pair headway':'Mean observed pair headway',avg==null?(state.tv?'—':'Unavailable'):num(avg,1)+' min')+`<div class="network-foot">${d.services.length} service-directions · ${d.model.trained?'Trained model':'Statistical baselines'}<br>Accuracy ${d.calibration?.n>=30?'observations available':'not yet validated'}</div>`;
 let last=d.timeline.at(-1),max=Math.max(1,...d.timeline.map(t=>Object.keys(TITLES).reduce((n,c)=>n+(t[c].available?t[c].count:0),0)));
 $('riskOutlook').innerHTML=Object.keys(TITLES).map(c=>`<div class="timeline-row" style="--risk:${COLORS[c]}">${icon(c)}<div><b>${TITLES[c].replace('passenger ','')}</b><small>${last[c].available?'2-hour baseline':'2-hour data unavailable'}</small></div><strong>${last[c].available?num(last[c].count):'—'}</strong></div>`).join('')+`<div class="risk-timeline">${d.timeline.map(t=>`<div class="risk-time-column"><div class="risk-time-bar">${Object.keys(TITLES).map(c=>`<i style="height:${t[c].available?(t[c].count/max*100):0}%;background:${COLORS[c]}"></i>`).join('')}</div>${t.h===0?'NOW':t.h+'m'}</div>`).join('')}</div><p class="evidence-note">${d.mode==='demo'?'DEMO · All figures and demand profiles are simulated.':'Evidence quality varies by source and horizon. No percentage occupancy inferred from categorical loads.'}</p>`;
}
function tvAlertHTML(a){
 const rec=state.data.recommendations.find(r=>r.id===a.id),stamp=a.last||state.data.updated;
 return `<article class="alert ${['acknowledged','monitoring'].includes(a.status)?'acked':''}" style="--risk:${COLORS[a.category]}" data-alert="${esc(a.id)}" tabindex="0" role="button"><div class="alert-top"><strong>${badge(a.category)} ${esc(a.svc)} · DIR ${a.dir}</strong><span class="score">PRIORITY ${a.score}</span></div><h3>${esc(TITLES[a.category])} · ${esc(a.location)}</h3><p class="alert-impact"><b>${state.mode==='demo'?'DEMO':state.h?'FORECAST':'CURRENT ESTIMATE'}:</b> ${esc(a.impact)}${a.onset?' · in '+num(a.onset)+' min':''}</p><p class="alert-action"><b>Action:</b> ${esc(rec?.action||'Review / verify')}${a.buses?.length?' · Buses '+a.buses.map(esc).join(', '):''}</p><div class="alert-bottom"><small>${a.evidence==='stale'?'LAST KNOWN · ':''}${timeString(stamp)} SGT · ${Math.round(a.confidence*100)}% evidence · ${esc(a.status.toUpperCase())}</small></div></article>`;
}
// Render the largest complete page that fits, without changing font sizes.
function fitTVPage(host,rows,renderRow,maxSize,estimate=100,wrap=html=>html,focusID=null){
 let size=state.tv?Math.max(1,Math.min(maxSize,Math.floor((host.clientHeight||estimate)/estimate))):maxSize;
 let page;
 do{const index=focusID?rows.findIndex(r=>r.id===focusID):-1;page=slicePage(rows,index>=0?Math.floor(index/size):state.page,size);host.innerHTML=wrap(page.rows.map(renderRow).join(''));if(!state.tv||host.scrollHeight<=host.clientHeight+1||size===1)break;size--;}while(size>0);
 return page;
}
function alertHTML(a){if(state.tv)return tvAlertHTML(a);let d=state.data,rec=d.recommendations.find(r=>r.id===a.id),buses=a.buses?.length?a.buses.join(', '):'Not identified',stamp=a.last||d.updated;return `<article class="alert ${['acknowledged','monitoring'].includes(a.status)?'acked':''}" style="--risk:${COLORS[a.category]}" data-alert="${esc(a.id)}" tabindex="0" role="button" aria-label="${esc(TITLES[a.category]+' service '+a.svc+' direction '+a.dir)}"><div class="alert-top"><strong>${badge(a.category)} ${esc(a.svc)} · DIR ${a.dir}</strong><span class="score">PRIORITY ${a.score}</span></div><h3>${esc(TITLES[a.category])} · ${esc(a.location)}</h3><p class="alert-impact"><b>${state.mode==='demo'?'DEMO':state.h?'FORECAST':'CURRENT ESTIMATE'}:</b> ${esc(a.impact)}${a.onset?' · in '+num(a.onset)+' min':''}${a.delay!=null?' · +'+num(a.delay,1)+' min extra':''}</p><p class="alert-observed"><b>Evidence:</b> ${esc(a.category==='traffic'?(a.cause.toLowerCase().includes('verified')&&!a.cause.toLowerCase().includes('not verified')?'Verified route / direction match':'Route speed / weather model'):a.category==='loading'?'LTA categorical load sample':a.category==='bunching'?'Two consecutive ETA headways':'ETA headway vs scheduled')}</p><p class="alert-action"><b>Action:</b> ${esc(rec?.action||'Review / verify with controller')} · <b>Buses:</b> ${esc(buses)}</p><div class="alert-bottom"><small>${a.evidence==='stale'?'LAST KNOWN · ':''}Updated ${timeString(stamp)} SGT · ${Math.round(a.confidence*100)}% evidence quality</small><span class="tag ${state.mode==='demo'?'demo':'estimated'}">${esc(a.status.toUpperCase())}</span></div></article>`;}

function alerts(d){
 if(!$('alerts'))return;  // Operational alerts panel retired; the bottom row lists every alert
 let list=operationalQueue(d,state.tab==='history').filter(a=>state.tab==='history'||a.status!=='resolved');
 if(state.search)list=list.filter(a=>(a.svc+' '+a.location+' '+a.category+' '+a.cause).toLowerCase().includes(state.search.toLowerCase()));
 const size=Math.max(1,Math.min(3,Math.floor(($('alerts').clientHeight||220)/(innerWidth>=2600?290:state.tv?175:220))));const page=fitTVPage($('alerts'),list,alertHTML,size,innerWidth>=2600?290:175,html=>html,spotlight.current?.id);$('alertCount').textContent=`(${list.length})`;$('alertPage').textContent=`${page.page} / ${page.pages}`;$('alerts').innerHTML=page.rows.map(alertHTML).join('')||'<div class="empty">No matching operational alerts.<br>Unavailable data is not a safety clearance.</div>';$('alertFoot').textContent=state.tab==='history'?'Audited lifecycle · select an entry for details':`Priority sorted · rotates every 12 s · ${state.mode==='demo'?'DEMO':'evidence-based estimates'}`;
 highlightSpotlight();
 document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===state.tab));
}
function analytics(d){
 const labels=d.timeline.map(t=>t.h===0?'Now':t.h+'m');
 if(d.mode==='live'){
   const load=loadSummary(d);$('loadMode').textContent='CURRENT LTA READINGS';
   const categories=[['SEA','Seats available','#39e4a2'],['SDA','Standing available','#ffb64a'],['LSD','Limited standing · heavy','#ff6474']];
   $('loadingChart').innerHTML=load.known?categories.map(([code,label,color])=>`<div class="load-category" style="--risk:${color}"><div><span>${label}</span><b>${load[code]} buses</b></div><div class="category-track"><i style="width:${100*load[code]/load.known}%"></i></div></div>`).join('')+`<p class="load-foot">${load.LSD} / ${load.known} known readings report heavy load.${load.UNKNOWN?' '+load.UNKNOWN+' unknown.':''}<br>Each bus is counted in its own LTA band (never averaged into a %). Buses sampled at watched stops, not the whole fleet.</p>`:'<div class="empty">No current categorical load readings.<br><small>Waiting for LTA collection; unknown does not mean empty.</small></div>';
 }else{
   $('loadMode').textContent='SIMULATED FORECAST';const occ=d.timeline.map(t=>t.loading.available?t.loading.occupancy:null);
   $('loadingChart').innerHTML=chartSVG(occ,{color:COLORS.loading,labels,max:100,bars:true,unit:'%'})+'<div class="chart-caption">Simulated occupancy · not observed passenger counts</div>';
 }
 renderHeadways(d);
 let clusters=[];d.services.forEach(s=>{if(s.three>0)(s.bunching_details?.length?s.bunching_details:[{location:s.location}]).forEach(g=>clusters.push({service:s,group:g}));});
 const bp=slicePage(clusters,state.page,state.tv?Math.max(1,Math.min(2,Math.floor(($('bunchingPanel').clientHeight||100)/110))):2),twoKnown=d.services.some(s=>s.two!=null),two=twoKnown?d.services.reduce((n,s)=>n+(s.two||0),0):null;
 $('bunchingCounts').textContent=`2BB ${num(two)} · 3BB ${d.kpis.bunching.available?num(d.kpis.bunching.count):'—'}`;
 $('bunchingPanel').innerHTML=bp.rows.map(({service:s,group:g})=>`<div class="bunch-row detail" data-service="${esc(s.svc)}" tabindex="0" role="button"><div class="bunch-heading"><b>Svc ${esc(s.svc)} · Dir ${s.dir}</b><div class="bunch-buses">${icon('bus').repeat(3)}</div></div><small class="stop-context">${esc(stopText(g.stop))}</small><span class="bus-order">${g.bus_order?.length?'Order: '+g.bus_order.map(x=>'#'+esc(x)).join(' → '):'Bus order unavailable'}${g.headways?.length?' · '+g.headways.map(x=>num(x,1)).join(' / ')+' min':''}</span></div>`).join('')||`<div class="empty">${d.kpis.bunching.available?'No confirmed three-bus cluster at this horizon.':'Bunching model unavailable at this horizon.'}<br><small>Two-bus events are counted separately.</small></div>`;
 const ranked=riskServices(d).slice(0,10);
 const page=fitTVPage($('serviceWatch'),ranked,s=>{
   let cat=s.blocked||s.building?'traffic':s.three?'bunching':s.gaps?'gap':s.heavy_buses?'loading':'traffic';
   let impact=cat==='traffic'?(s.blocked?'Blocked':s.delay==null?'—':'+'+num(s.delay,1)+'m'):cat==='loading'?(s.occupancy!=null?num(s.occupancy)+'%':'Heavy'):cat==='bunching'?s.three+' × 3BB':num(s.headway)+'m';
   const reason=cat==='traffic'?(s.building?'Traffic building':s.blocked?'Road closed':'Traffic delay'):TITLES[cat].replace('passenger ','').replace('Three-bus ','');
   const detail=cat==='traffic'?s.traffic_details?.[0]:cat==='bunching'?s.bunching_details?.[0]:s.gap_details?.[0];const stop=detail?.stops?.[0]||detail?.stop;
   const where=state.tv&&stop?[stopText(stop),detail.road||detail.location].filter(Boolean).join(' · '):watchLocation(s,cat);
   return `<tr data-service="${esc(s.svc)}" tabindex="0" role="button"><td><b>${esc(s.svc)}</b> D${s.dir}</td><td>${badge(cat)} ${esc(reason)}</td><td style="color:${COLORS[cat]}">${impact}</td></tr><tr class="watch-context" data-service="${esc(s.svc)}" tabindex="0" role="button"><td colspan="3"><div class="stop-lines" title="${esc(where)}">${esc(where)}</div></td></tr>`;
 },2,140,html=>`<table class="tiny-table"><thead><tr><th style="width:25%">Svc / Dir</th><th style="width:48%">Watch reason</th><th>Impact</th></tr></thead><tbody>${html}</tbody></table>`);$('watchPage').textContent=`${page.page}/${page.pages} · top 10`;
 fitTVPage($('recommendations'),d.recommendations,a=>{const i=d.recommendations.indexOf(a);return `<div class="recommendation"><button data-recommend="${i}">${icon('spark')}<span>Svc ${esc(a.svc)} · ${esc(a.action)}</span></button><small>Evidence-linked · approval required</small></div>`;},2,125,html=>html+`<p class="advisory-note">${state.tv?'Advisory only · validate duty / layover / breaks.':'Advisory only. Duty, layover and welfare constraints must be checked.'}</p>`);

}
function headwayTable(view){return `<table class="headway-table"><thead><tr><th>SGT</th><th>Scheduled</th><th>Actual</th><th>Forecast</th><th>Actual − scheduled</th></tr></thead><tbody>${view.rows.map(r=>`<tr class="${r.abnormal?'abnormal':''}"><td>${r.label}</td><td>${num(r.scheduled,1)}</td><td>${num(r.actual,1)}</td><td>${num(r.forecast,1)}</td><td>${num(r.difference,1)}</td></tr>`).join('')}</tbody></table>`;}
function renderHeadways(d){
 const services=[...new Set(d.services.map(s=>s.svc))];if(state.headwayScope!=='network'&&!services.includes(state.headwayScope))state.headwayScope=services[0]||'network';
 $('headwayScope').innerHTML='<option value="network">Network · current filters</option>'+services.map(s=>`<option value="${esc(s)}">Service ${esc(s)}</option>`).join('');$('headwayScope').value=state.headwayScope;
 headwayData=hourlyHeadway(d,{interval:state.headwayInterval,scope:state.headwayScope,direction:state.headwayDirection});
 $('headwayMode').textContent=state.tv?(state.headwayScope==='network'?'NETWORK · ETA PAIRS':'SVC '+state.headwayScope+' · DIR '+state.headwayDirection)+(d.mode==='demo'?' · DEMO':''):d.mode==='demo'?'DEMO · SIMULATED':'HISTORICAL / FORECAST';
 const message=d.mode==='demo'?(state.tv?'DEMO · simulated data':'DEMO: simulated hourly observations and forecasts.'):!headwayData.actualAvailable?(state.tv?'ACTUAL DATA UNAVAILABLE · ETA forecast ≤30 min':'ACTUAL DATA UNAVAILABLE · verified stop-arrival history is not connected.'):'HISTORICAL · verified recorded arrivals only.';
 const width=Math.max(300,($('headwayChart').clientWidth||924)-24),height=state.tv?Math.max(90,($('headwayChart').clientHeight||180)-(innerWidth>=2600?62:44)):200;
 $('headwayChart').innerHTML=hourlyChart(headwayData,width,height,innerWidth>=2600?18:13)+`<div class="headway-legend"><span style="--line:#92a8bd">Scheduled</span><span style="--line:#40ddb5">Actual</span><span style="--line:#61c8ff">Forecast</span><span class="headway-coverage">${headwayData.directions} sampled directions</span></div><p class="headway-evidence">${message}${d.mode==='live'&&!state.tv?' Forecast: available ETA focus pairs, up to 30 min.':''}</p><div class="headway-inspect" id="headwayInspect" role="status">Hover or select a time period for values · minutes</div><button id="headwayValues" class="values-button">View hourly values / expand chart ↗</button>`;
}
function expandAnalysis(){let title={headway:'Hourly headway intelligence',loading:'Passenger load outlook',bunching:'Three-bus analysis'}[state.analysis];if(state.analysis==='headway')openModal(title,`<p>${headwayData.demo?'DEMO · simulated':'Verified history only; missing values stay unavailable. Forecasts use available service-direction ETA focus pairs, not a fleet-wide census.'}</p>${hourlyChart(headwayData)}<div class="headway-inspect" id="modalHeadwayInspect">Select a period for scheduled, actual, forecast and difference.</div><details open><summary>Hourly / interval values · minutes</summary>${headwayTable(headwayData)}</details>`);else openModal(title,$(state.analysis==='loading'?'loadingChart':'bunchingPanel').innerHTML);}
function inspectHour(index){let r=headwayData?.rows[index];if(!r)return;const text=`${r.label} SGT · Scheduled ${num(r.scheduled,1)} · Actual ${num(r.actual,1)} · Forecast ${num(r.forecast,1)} min · Difference ${num(r.difference,1)} min${r.confidence!=null?' · Evidence quality '+Math.round(r.confidence*100)+'%':''}`;for(const id of ['headwayInspect','modalHeadwayInspect'])if($(id)){$(id).textContent=text;$(id).classList.add('active');setTimeout(()=>$(id)?.classList.remove('active'),5500);}}
function render(d){
 const previousIds=new Set((state.data?.alerts||[]).map(a=>a.id));if(state.tv){const fresh=spotlightDeck(d).filter(a=>!previousIds.has(a.id)),freshIds=new Set(fresh.map(a=>a.id));spotlight.deck=[...fresh,...spotlight.deck.filter(a=>!freshIds.has(a.id))];}
 state.data=d;updateStatus();controls();kpiCards(d);networkStatus(d);alerts(d);analytics(d);networkMap.update(d);renderBoard(d);
 $('mapWatermark').hidden=d.mode!=='demo';$('mapWatermark').style.display=d.mode==='demo'?'grid':'none';
 $('mapMode').textContent=d.mode==='demo'?'DEMO':state.h?'ESTIMATED':'LIVE';$('mapHorizon').textContent=state.h===0?'Current observations':state.h+'-minute forecast';$('mapWatermark').hidden=d.mode!=='demo';$('weather').textContent=d.weather.text;
 $('mapDescription').textContent=d.mode==='demo'?'Simulated buses · real route roads and stops':state.h?'Live bus positions retained · projected positions unavailable':'Verified observations · categorical loading';
 if(state.tv&&spotlight.current){if(!d.alerts.some(a=>a.id===spotlight.current.id))clearSpotlight(false);else{spotlight.current=d.alerts.find(a=>a.id===spotlight.current.id);showActivityBanner(spotlight.current);highlightSpotlight();}}
 let u=new URL(location.href);u.searchParams.set('mode',state.mode);u.searchParams.set('h',state.h);u.searchParams.set('scenario',state.scenario);history.replaceState(null,'',u);$('error').textContent=d.journal_error||'';
}
async function fetchState(){
 const id=++requestId;if(controller)controller.abort();controller=new AbortController();const activeController=controller;let timedOut=false;const deadline=setTimeout(()=>{timedOut=true;activeController.abort();},55000);document.body.classList.add('fetching');
 try{let p={...state,category:'all'};if(state.operator){let byOp=state.catalog.filter(s=>s.operator===state.operator).map(s=>s.svc);p.services=state.services.length?state.services.filter(s=>byOp.includes(s)):byOp;if(!p.services.length)p.services=['NO_MATCH'];}
  let response=await fetch(apiURL(p),{credentials:'same-origin',signal:controller.signal});if(!response.ok)throw Error('Forecast adapter returned '+response.status);let d=await response.json();if(id!==requestId)return;if(d.context_routes==null&&d.context_routes_cached&&state.contextRoutes)d.context_routes=state.contextRoutes;else{state.contextRoutes=d.context_routes||null;state.routesV=d.context_routes?d.context_routes_version||null:null;}state.full=d;render(categoryView(d,state.category));if(lastFocus){networkMap.focus(lastFocus);lastFocus=null;}if(state.mode==='live'&&Date.now()-cameraTime>60000)loadCameras();else if(state.mode==='demo')cameras([]);
 }catch(e){if(e.name==='AbortError'&&!timedOut)return;$('error').textContent='Forecast unavailable. '+e.message+'. Last successful data is retained and freshness stays visible.';if(state.data){state.data.error=e.message;updateStatus();}else{$('systemStatus').textContent='OFFLINE';$('systemStatus').className='status offline';}}
 finally{clearTimeout(deadline);if(id===requestId)document.body.classList.remove('fetching');}
}
async function loadCatalog(){if(state.mode==='demo'){state.catalog=(state.data?.services||[]).map(s=>({svc:s.svc,operator:s.operator||'DEMO'}));$('operator').innerHTML='<option value="">All operators</option><option>SBST</option><option>SMRT</option>';serviceOptions();return;}try{let j=await(await fetch('/api/forecast/services',{signal:AbortSignal.timeout(12000)})).json();state.catalog=j.services||[];}catch{state.catalog=[];}if(!state.catalog.length&&state.data)state.catalog=state.data.services.map(s=>({svc:s.svc,operator:s.operator||'DEMO'}));let ops=[...new Set(state.catalog.map(s=>s.operator).filter(Boolean))].sort();$('operator').innerHTML='<option value="">All operators</option>'+ops.map(op=>`<option>${esc(op)}</option>`).join('');serviceOptions();}
function serviceOptions(){let q=$('serviceSearch').value.toUpperCase();$('serviceOptions').innerHTML=state.catalog.filter(s=>s.svc.includes(q)).map(s=>`<label><input type="checkbox" data-pick="${esc(s.svc)}" ${state.services.includes(s.svc)?'checked':''}>${esc(s.svc)}</label>`).join('');}
async function selectService(svc){cameraTime=0;cameraPoint=null;cameraRequest++;state.services=toggleService(state.services,svc);state.page=0;lastFocus=state.services.includes(svc)?svc:null;serviceOptions();controls();await fetchState();if(!state.services.length)networkMap.fit();}
let cameraAI=null,cameraAIAt=0,cameraAIKey='';
const LEVEL_CLASS={'Smooth':'lv-smooth','Medium':'lv-medium','Jam':'lv-jam','Super Jam':'lv-super'};
async function loadCameraAI(force=false){if(state.mode==='demo'){cameraAI=null;paintCameraAI();return;}const ids=cameraRows.map(c=>c.id).join(',');if(!force&&ids===cameraAIKey&&Date.now()-cameraAIAt<120000)return;cameraAIKey=ids;cameraAIAt=Date.now();
 try{const r=await fetch('/api/forecast/centre/camera-ai?ids='+encodeURIComponent(ids));cameraAI=r.ok?await r.json():{status:'Camera analytics unavailable (server answered HTTP '+r.status+')'};}catch(e){cameraAI={status:'Camera analytics unavailable (network error)'};}paintCameraAI();}
const CHECKPOINT_CAMS={'2701':'woodlands','2702':'woodlands','4703':'tuas','4713':'tuas'};
function camAIHTML(id,busy=true){const ai=cameraAI,r=ai?.cameras?.[id],x=CHECKPOINT_CAMS[id]?(ai?.crossings||[]).find(k=>k.id===CHECKPOINT_CAMS[id]):null;
 const dir=(label,d)=>d?`<div class="ca-dir"><em>${label}</em><b>≈ ${d.minutes} min</b><i class="${LEVEL_CLASS[d.level]||''}">${esc(d.level)}</i></div>`:'';
 let h=`<div class="ca-head">${icon('spark')}<span>${r?.source==='AI camera analytics'?'AI CAMERA ANALYTICS':'TRAFFIC LEVEL'}</span></div>`;
 if(r?.level)h+=`<div class="ca-level ${LEVEL_CLASS[r.level]||''}">${esc(r.level.toUpperCase())}</div><div class="ca-src">${r.source==='AI camera analytics'?'Read from this photo by AI'+(r.confidence!=null?' · '+Math.round(r.confidence*100)+'% sure':''):'LTA speed band near camera'+(r.speed!=null?` · ${r.speed}–${r.free_speed} km/h`:'')}</div>`;
 else h+=`<div class="ca-src">${busy?'Analysing photo…':'No traffic level for this camera'}</div>`;
 if(x)h+=x.any?dir('JB / SG',x.any):dir('→ JOHOR',x.to_jb)+dir('→ SINGAPORE',x.to_sg);
 if(ai?.error&&ai?.configured?.ai)h+=`<div class="ca-warn">${esc(ai.error)}</div>`;
 else if(ai?.configured&&!ai.configured.ai)h+=`<div class="ca-warn">AI is off: ANTHROPIC_API_KEY not set on the server</div>`;
 if(ai?.status)h+=`<div class="ca-warn">${esc(ai.status)}</div>`;
 if(ai?.ai_paused)h+=`<div class="ca-src">${esc(ai.ai_paused)}</div>`;
 if(ai?.ai_usage&&ai?.configured?.ai)h+=`<div class="ca-src">AI reads the 4 checkpoint photos every ${ai.ai_usage.every_min} min · ${ai.ai_usage.calls_today}/${ai.ai_usage.daily_cap} today</div>`;
 return h+`<a class="ca-test" href="/api/camera-ai/test" target="_blank" rel="noopener">Run AI check ↗</a>`;}
async function analyseCamera(id){const ids=[...new Set([id,...cameraRows.map(c=>c.id)])].slice(0,6).join(',');
 try{const r=await fetch('/api/forecast/centre/camera-ai?ids='+encodeURIComponent(ids));cameraAI=r.ok?await r.json():{status:'Camera analytics unavailable (server answered HTTP '+r.status+')'};}catch(e){cameraAI={status:'Camera analytics unavailable (network error)'};}
 cameraAIAt=Date.now();paintCameraAI();const box=document.getElementById('camAI');if(box&&box.dataset.cam===String(id))box.innerHTML=camAIHTML(id,false);}
function cameraAIStatus(ai){if(ai.status)return ai.status;const c=ai.configured||{};if(!c.ai&&!c.speed)return 'JB / SG crossing time is off · add ANTHROPIC_API_KEY in Render → Environment (LTA speed bands also unavailable)';if(ai.error)return 'JB / SG crossing time: '+ai.error+(c.speed?' · speed data not available for the checkpoint cameras':'');return 'JB / SG crossing time: checkpoint cameras not in the live LTA feed right now';}
function paintCameraAI(){const box=$('crossings');const ai=state.mode==='demo'?null:cameraAI;
 document.querySelectorAll('[data-cam-level]').forEach(el=>{const r=ai?.cameras?.[el.dataset.camLevel];el.hidden=!r?.level;if(r?.level){el.className='cam-level '+(LEVEL_CLASS[r.level]||'');el.textContent=(r.source==='AI camera analytics'?'AI · ':'')+r.level.toUpperCase();el.title=r.source+(r.speed!=null?` · ${r.speed}–${r.free_speed} km/h`:'')+(r.confidence!=null?` · confidence ${Math.round(r.confidence*100)}%`:'');}});
 if(!box)return;const rows=ai?.crossings||[];box.hidden=!ai;if(!ai)return;if(!rows.length){box.innerHTML=`<div class="xing-off">${esc(cameraAIStatus(ai))}</div>`;return;}
 const dir=(label,x)=>x?`<span class="xing-dir"><em>${label}</em><b>≈ ${x.minutes} min</b><i class="${LEVEL_CLASS[x.level]||''}">${esc(x.level)}</i></span>`:`<span class="xing-dir muted"><em>${label}</em><b>—</b></span>`;
 box.innerHTML=rows.map(x=>`<div class="xing" title="${esc(ai.note||'')}"><strong>${esc(x.name)}</strong>${x.any?dir('JB / SG',x.any):dir('→ JB',x.to_jb)+dir('→ SG',x.to_sg)}</div>`).join('')+(rows.length?`<small class="xing-note">${rows[0].source==='AI camera analytics'?'AI camera':'LTA speed-band'} estimate · road queue only</small>`:'')+(ai?.error&&ai?.configured?.ai?`<small class="xing-warn" title="Open /api/camera-ai/test for details">${esc(ai.error)} · showing LTA speed bands</small>`:'');}
function cameras(items,meta={}){cameraRows=items.slice(0,6);cameraMeta=meta;const count=state.tv?4:6,cameraPage=slicePage(cameraRows,state.page,count);$('cameraStatus').textContent=state.mode==='demo'?'No simulated photos':(meta.context?.startsWith('Nearest')?'NEAREST':'SOURCE IMAGES')+(cameraPage.pages>1?' · '+cameraPage.page+'/'+cameraPage.pages:'');
 $('cameras').innerHTML=cameraRows.length?cameraPage.rows.map(c=>{const i=cameraRows.indexOf(c);return `<button class="camera" data-camera="${i}" title="Enlarge camera ${esc(c.id)}"><img src="${esc(c.image)}" alt="${esc(c.label||'Traffic camera')}" loading="lazy" referrerpolicy="no-referrer"><span class="cam-level" data-cam-level="${esc(c.id)}" hidden></span><p>${esc(c.label||'Camera '+c.id)}${c.distance_km!=null?' · '+c.distance_km+' km':''}</p><small class="${c.stale?'stale':''}">${c.taken?timeString(Date.parse(c.taken)/1000)+' SGT':'Capture time unknown'}${c.stale?' · STALE / UNVERIFIED':''}</small></button>`;}).join(''):`<div class="camera-message">${state.mode==='demo'?'DEMO · no artificial camera images. Switch to LIVE for source cameras.':esc(meta.error||'Waiting for camera adapter…')}</div>`;
 paintCameraAI();loadCameraAI();
 if(meta.error||meta.diagnostics?.length)$('cameras').innerHTML+=`<details class="camera-diagnostics"><summary>Camera source diagnostics</summary><p>${esc([meta.error,...(meta.diagnostics||[])].filter(Boolean).join(' · '))}</p></details>`;
}
async function loadCameras(point=cameraPoint){if(state.mode!=='live')return;cameraPoint=point;cameraTime=Date.now();const id=++cameraRequest;try{let p=point?'?lon='+encodeURIComponent(point[0])+'&lat='+encodeURIComponent(point[1]):'',r=await fetch('/api/forecast/centre/cameras'+p,{signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error('Camera adapter returned '+r.status);let j=await r.json();if(state.mode==='live'&&id===cameraRequest)cameras((j.cameras||[]).map(c=>({...c,stale:c.stale||!j.fetched_epoch||Date.now()/1000-j.fetched_epoch>180})),j);}catch(e){if(state.mode==='live'&&id===cameraRequest)cameras(cameraRows.map(c=>({...c,stale:true})),{error:'Camera refresh failed: '+e.message+'. Last known images retained.'});}}
function openModal(title,body){const m=$('modal');if(m&&document.body&&m.parentElement!==document.body&&typeof document.body.appendChild==='function')document.body.appendChild(m);   // a transformed shell parent would shift a fixed modal under the sidebar
 modalFocus=document.activeElement;$('modalTitle').textContent=title;$('modalBody').innerHTML=body;$('modal').hidden=false;$('closeModal').focus();}
function closeModal(){$('modal').hidden=true;modalFocus?.focus();}
function alertDetail(id){let a=operationalQueue(state.data).find(a=>a.id===id)||(state.data?.history||[]).find(a=>a.id===id);if(!a)return;if(a.category==='traffic'&&a.point)loadCameras(a.point);let rec=state.data.recommendations.find(r=>r.id===id);openModal(`${TITLES[a.category]} · Service ${a.svc}`,`<span class="tag ${state.mode==='demo'?'demo':'estimated'}">${state.mode.toUpperCase()} · ${state.h===0?'CURRENT':'FORECAST'}</span><p><b>${esc(a.impact)}</b> near ${esc(a.location)}${a.evidence==='stale'?'<br>LAST KNOWN RISK: not re-observed in the current data.':''}<br>Occurrence: ${num(a.onset)} minutes ahead · evidence quality ${Math.round(a.confidence*100)} / 100 (not measured accuracy)</p><p><b>Why:</b> ${esc(a.cause)}</p><p><b>Direction:</b> ${a.dir} · <b>Stop:</b> ${esc(a.stop?stopText(a.stop):a.affected_stops?.length?a.affected_stops.map(stopText).join(' → '):'Bus stop / sequence unavailable')}</p>${a.headways?`<p>Consecutive headways: ${a.headways.map(v=>num(v,1)+' min').join(' / ')}</p>`:''}${a.buses?`<p>Bus references: ${a.buses.map(esc).join(', ')}</p>`:''}${a.headway?`<p>Predicted headway ${num(a.headway)} min · scheduled ${num(a.scheduled)} min</p>`:''}<p><b>Recommendation:</b> ${esc(rec?.explanation||'Review available evidence and confirm operational constraints before acting.')}</p><p>Current status: <b>${esc(a.status)}</b>. Actions below update the audit trail only; they do not dispatch or regulate buses.</p><div style="display:flex;gap:8px;flex-wrap:wrap">${['acknowledged','monitoring','resolved'].map(status=>`<button data-transition="${status}" data-id="${esc(id)}">${status==='acknowledged'?'Acknowledge':status==='monitoring'?'Monitor':'Resolve'}</button>`).join('')}<button data-service="${esc(a.svc)}">Focus service on map</button></div>`);}
function recommendationDetail(index){let r=state.data.recommendations[index];if(!r)return;openModal('Operational scenario comparison · '+r.svc,`<p>${esc(r.explanation)}</p><p><b>Supporting evidence:</b> ${esc(r.reason)}</p><table><thead><tr><th>Scenario</th><th>Headway</th><th>Feasibility</th></tr></thead><tbody>${r.options.map(o=>`<tr><td>${esc(o.name)}</td><td>${o.headway==null?'Not quantified':num(o.headway)+' min'}</td><td>${esc(o.feasible)}</td></tr>`).join('')}</tbody></table><p><b>Expected improvement:</b> ${esc(r.improvement)}<br><b>Constraints:</b> ${esc(r.constraints)}<br><b>Approval:</b> ${esc(r.approval)}</p><p>Minimum layover, break protection, departed trips, adjustment limits and vehicle availability must all be validated. No action is automatically executed.</p>`);}
function sourcesModal(){let d=state.data;if(!d)return;openModal('Data sources & forecast integrity',`<span class="tag ${state.mode==='demo'?'demo':'estimated'}">${esc(d.model.name)}</span>${d.sources.map(s=>`<div class="source-row"><b>${esc(s.name)}</b><span>${esc(s.status)}</span><small>${esc(s.detail)}</small></div>`).join('')}<p>Last successful observation: ${new Date(d.updated*1000).toLocaleString('en-SG',{timeZone:'Asia/Singapore'})} SGT</p><p>${esc(d.model.confidence_note)}</p><h3>Current limitations</h3>${d.model.limitations.map(l=>`<p>• ${esc(l)}</p>`).join('')}<h3>Forecast accuracy</h3><p>${d.calibration?.n>=30?'Historical calibration observations available; see legacy forecast integrity records.':'Not validated. Precision, recall, false-alarm rate and occupancy error need outcome history; no accuracy percentage is claimed.'}</p>`);}
function tv(on){if(!on){clearSpotlight(true);setAnalysis(spotlight.baseAnalysis,true);}else{spotlight.baseAnalysis=state.analysis;spotlight.baseHeadway={scope:state.headwayScope,direction:state.headwayDirection};spotlight.nextAt=Date.now()+6000;spotlight.pausedUntil=0;spotlight.step=-1;spotlight.deck=[];}state.tv=on;spotlightControl();state.page=0;document.body.classList.toggle('tv',on);if(on&&document.documentElement.requestFullscreen)document.documentElement.requestFullscreen().catch(()=>{});else if(!on&&document.fullscreenElement)document.exitFullscreen().catch(()=>{});setTimeout(refreshTVLayout,120);let u=new URL(location.href);if(on)u.searchParams.set('present','1');else u.searchParams.delete('present');history.replaceState(null,'',u);}
function setAnalysis(key,temporary=false){state.analysis=key;if(!temporary)spotlight.baseAnalysis=key;document.querySelectorAll('[data-analysis]').forEach(b=>b.classList.toggle('active',b.dataset.analysis===key));for(const name of ['headway','loading','bunching'])$(name+'Module').hidden=name!==key;if(state.data)analytics(state.data);}
function spotlightControl(){const button=$('spotlightToggle');button.textContent=spotlight.enabled?(spotlight.pausedUntil>Date.now()?'Auto resumes '+Math.ceil((spotlight.pausedUntil-Date.now())/1000)+'s':'Pause spotlight'):'Resume spotlight';button.setAttribute('aria-pressed',String(spotlight.enabled));}
function highlightSpotlight(){document.querySelectorAll('.tour-highlight').forEach(el=>el.classList.remove('tour-highlight'));if(!state.tv||!spotlight.current)return;const a=spotlight.current;document.querySelectorAll('[data-alert]').forEach(el=>{if(el.dataset.alert===a.id)el.classList.add('tour-highlight');});document.querySelectorAll('.risk-card').forEach(el=>{if(el.dataset.category===a.category)el.classList.add('tour-highlight');});}
function clearSpotlight(restoreView=false){spotlight.current=null;state.headwayScope=spotlight.baseHeadway.scope;state.headwayDirection=spotlight.baseHeadway.direction;$('headwayDirection').value=state.headwayDirection;networkMap.setSpotlight(null);$('activitySpotlight').hidden=true;if(state.data)renderHeadways(state.data);document.querySelectorAll('.tour-highlight').forEach(el=>el.classList.remove('tour-highlight'));if(restoreView){if(state.services.length)networkMap.focus(state.services[0]);else networkMap.fit();}}
function activityBanner(title,detail,category='all'){const banner=$('activitySpotlight');banner.hidden=false;banner.style.setProperty('--risk',COLORS[category]||COLORS.route);banner.innerHTML=`<strong>${esc(title)}</strong><span>${esc(detail)}</span>`;}
function showActivityBanner(a){const data=state.data;activityBanner((data.mode==='demo'?'DEMO ACTIVITY':a.first&&Date.now()/1000-a.first<=300?'NEW ACTIVITY':'ACTIVITY SPOTLIGHT')+' · '+a.svc+' / DIR '+a.dir,TITLES[a.category]+' · '+a.location+' · '+a.impact,a.category);}
function advanceSpotlight(force=false){
 if(!state.tv||!spotlight.enabled||!state.data||document.hidden||!$('modal').hidden||!force&&Date.now()<spotlight.pausedUntil)return;
 spotlight.nextAt=Date.now()+12000;spotlight.step=(spotlight.step+1)%6;
 const data=state.data,deck=spotlightDeck(data);if(!deck.length){clearSpotlight(false);setAnalysis(['headway','loading','bunching'][spotlight.step%3],true);activityBanner(data.mode==='live'&&Date.now()/1000-data.updated>150?'DATA DELAYED · LAST-KNOWN VIEW':'NETWORK OVERVIEW','No fresh matching alerts · showing available analytics');return;}
 if(spotlight.step===0||spotlight.step===5){clearSpotlight(true);setAnalysis(spotlight.step===5?'loading':spotlight.baseAnalysis,true);activityBanner('NETWORK OVERVIEW',data.mode==='demo'?'DEMO · simulated operating scenario':'Current filters retained · next activity in 12 seconds');return;}
 if(spotlight.step===3&&cameraRows.length){clearSpotlight(false);const camera=cameraRows[state.page%cameraRows.length];const panel=$('cameras').closest('.panel');panel?.classList.add('tour-highlight');activityBanner('TRAFFIC CAMERA · VISUAL CONTEXT',(camera.label||'Camera '+camera.id)+' · '+(camera.stale?'stale / capture time unverified':'captured '+timeString(Date.parse(camera.taken)/1000)+' SGT'));return;}
 const currentById=new Map(deck.map(a=>[a.id,a]));spotlight.deck=spotlight.deck.filter(a=>currentById.has(a.id)).map(a=>currentById.get(a.id));if(!spotlight.deck.length){spotlight.deck=deck;spotlight.cursor=0;}
 const a=spotlight.deck.shift();spotlight.current=a;state.headwayScope=a.svc;state.headwayDirection=String(a.dir);$('headwayDirection').value=state.headwayDirection;networkMap.setSpotlight(a);networkMap.focus(a.svc,a.dir,a.point);setAnalysis(a.category==='loading'?'loading':a.category==='bunching'?'bunching':'headway',true);
 showActivityBanner(a);alerts(data);highlightSpotlight();if(a.category==='traffic'&&a.point)loadCameras(a.point);
}
function tickSpotlight(){spotlightControl();if(spotlight.enabled&&Date.now()>=spotlight.nextAt&&Date.now()>=spotlight.pausedUntil)advanceSpotlight();}
function pauseForInteraction(event){if(!state.tv||!event.isTrusted||event.target.closest?.('#spotlightToggle'))return;spotlight.pausedUntil=Date.now()+30000;spotlight.nextAt=spotlight.pausedUntil;clearSpotlight(false);networkMap.map?.stop?.();spotlightControl();}
document.addEventListener('pointerdown',pauseForInteraction,{capture:true,passive:true});document.addEventListener('keydown',pauseForInteraction,{capture:true});
$('spotlightToggle').onclick=()=>{spotlight.enabled=!spotlight.enabled;spotlight.pausedUntil=0;spotlight.nextAt=Date.now()+1000;spotlightControl();};
function playback(on){state.playing=on;clearInterval(playTimer);$('play').textContent=on?'Ⅱ Pause':'▶ Play';if(on)playTimer=setInterval(()=>{state.h=HORIZONS[(HORIZONS.indexOf(state.h)+1)%HORIZONS.length];state.tick++;fetchState();},7000);}
$('headwayScope').onchange=()=>{state.headwayScope=$('headwayScope').value;if(state.data)renderHeadways(state.data);};
$('headwayDirection').onchange=()=>{state.headwayDirection=$('headwayDirection').value;if(state.data)renderHeadways(state.data);};
$('headwayInterval').onchange=()=>{state.headwayInterval=+$('headwayInterval').value;if(state.data)renderHeadways(state.data);};
$('expandAnalytics').onclick=expandAnalysis;
async function clearServices(){state.services=[];state.headwayScope='network';state.page=0;cameraPoint=null;cameraTime=0;serviceOptions();controls();await fetchState();networkMap.fit();}
$('clearService').onclick=clearServices;$('allServices').onclick=clearServices;
$('delayThreshold').onchange=()=>{const custom=$('delayThreshold').value==='custom';$('customDelayLabel').hidden=!custom;if(custom){$('customDelay').focus();return;}state.delayMin=+$('delayThreshold').value;state.page=0;fetchState();};
$('customDelay').onchange=()=>{const v=+$('customDelay').value;if(!$('customDelay').checkValidity()||!Number.isFinite(v))return;state.delayMin=v;state.page=0;fetchState();};
$('scenario').onchange=()=>{state.scenario=$('scenario').value;state.page=0;fetchState();};$('excludeCBS').onchange=()=>{state.exclude=$('excludeCBS').checked;cameraPoint=null;cameraTime=0;state.page=0;fetchState();};$('operator').onchange=()=>{state.operator=$('operator').value;cameraPoint=null;cameraTime=0;state.page=0;fetchState();};$('serviceSearch').oninput=serviceOptions;$('clearFilters').onclick=async()=>{Object.assign(state,resetFilters());state.h=0;state.headwayScope='network';state.headwayDirection='all';$('headwayDirection').value='all';$('delayThreshold').value='5';$('customDelayLabel').hidden=true;$('operator').value='';state.page=0;cameraPoint=null;cameraTime=0;lastFocus=null;serviceOptions();controls();await fetchState();networkMap.fit();};$('play').onclick=()=>playback(!state.playing);$('sources').onclick=sourcesModal;$('tv').onclick=()=>tv(true);$('exitTV').onclick=()=>tv(false);$('closeModal').onclick=closeModal;$('fitMap').onclick=()=>{$('clearFilters').click();};$('clearBuses').onclick=()=>{networkMap.setLayer('buses',!networkMap.flags.buses);$('clearBuses').textContent=networkMap.flags.buses?'Hide buses':'Show buses';controls();};$('map3D').onclick=()=>{$('map3D').classList.toggle('active',networkMap.toggle3D());};if($('alertSearch'))$('alertSearch').onclick=()=>openModal('Search operational alerts','<input id="searchAlerts" type="search" placeholder="Service, location or risk" style="width:100%;padding:12px;background:#102c40;border:1px solid #487089"><p>Search covers the selected horizon and filters. Select History to search the audit-backed queue.</p>');
$('modal').addEventListener('click',e=>{if(e.target===$('modal'))closeModal();});
document.addEventListener('input',e=>{if(e.target.id==='searchAlerts'){state.search=e.target.value;if(state.data)alerts(state.data);}});
document.addEventListener('change',e=>{if(e.target.dataset.pick){let svc=e.target.dataset.pick;state.services=state.services.filter(s=>s!==svc);if(e.target.checked)state.services.push(svc);state.page=0;lastFocus=e.target.checked?svc:state.services[0]||null;controls();cameraTime=0;fetchState();}if(e.target.dataset.layer)networkMap.setLayer(e.target.dataset.layer,e.target.checked);});
document.addEventListener('click',async e=>{let t=e.target.closest('button,[data-alert],[data-service]');if(!t)return;
 if(t.dataset.ack){ackCard(t.dataset.ack);return;}
 if(t.dataset.unack){ackCard(t.dataset.unack,true);return;}
 if(t.dataset.clearRoute){if(state.tv){spotlight.pausedUntil=Date.now()+30000;spotlight.nextAt=spotlight.pausedUntil;clearSpotlight(false);spotlightControl();}clearServices();return;}
 if(t.dataset.removeService){selectService(t.dataset.removeService);return;}
 if(t.dataset.camera!=null){let c=cameraRows[+t.dataset.camera];if(c){openModal(c.label||'Traffic camera '+c.id,`<div class="cam-wrap"><img class="camera-enlarged" src="${esc(c.image)}" alt="${esc(c.label)}" referrerpolicy="no-referrer"><div class="cam-ai" id="camAI" data-cam="${esc(c.id)}">${camAIHTML(c.id)}</div></div><p>${c.stale?'STALE / CAPTURE TIME UNVERIFIED':'SOURCE STILL IMAGE'} · Camera ${esc(c.id)} · ${esc(c.taken||'Capture timestamp unavailable')}</p><p>Coordinates ${c.lat}, ${c.lon}. Nearest-camera context does not confirm route impact.</p>`);analyseCamera(c.id);}return;}
 if(t.dataset.analysis){setAnalysis(t.dataset.analysis);return;}
 if(t.id==='headwayValues'){expandAnalysis();return;}
 if(t.dataset.mode){state.mode=t.dataset.mode;state.page=0;state.tab='active';playback(false);state.catalog=[];state.operator='';$('operator').value='';cameraTime=0;cameraPoint=null;cameraRows=[];cameraRequest++;await fetchState();loadCatalog();return;}
 if(t.dataset.h!=null){state.h=+t.dataset.h;state.page=0;fetchState();return;}
 if(t.dataset.category){state.category=state.category===t.dataset.category?'all':t.dataset.category;state.page=0;if(state.full)render(categoryView(state.full,state.category));else fetchState();return;}
 if(t.dataset.service){closeModal();selectService(t.dataset.service);return;}
 if(t.dataset.tab){state.tab=t.dataset.tab;state.page=0;if(state.data)alerts(state.data);return;}
 if(t.dataset.recommend!=null){recommendationDetail(+t.dataset.recommend);return;}
 if(t.dataset.transition){t.disabled=true;try{let r=await fetch('/api/forecast/centre/alerts',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({namespace:state.data.namespace,id:t.dataset.id,status:t.dataset.transition})});if(!r.ok)throw Error('Could not save alert status');closeModal();await fetchState();}catch(err){$('error').textContent=err.message;t.disabled=false;}return;}
 if(t.dataset.alert)alertDetail(t.dataset.alert);
});
// The Map layers menu closes when you click or tap anywhere outside it (map included), or press Esc.
document.addEventListener('pointerdown',e=>{document.querySelectorAll('details.layers[open]').forEach(d=>{if(!d.contains(e.target))d.open=false;});},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('details.layers[open]').forEach(d=>{d.open=false;});});
document.addEventListener('pointerover',e=>{let t=e.target.closest('[data-hour]');if(t)inspectHour(+t.dataset.hour);});
document.addEventListener('focusin',e=>{if(e.target.dataset.hour!=null)inspectHour(+e.target.dataset.hour);});
document.addEventListener('click',e=>{if(e.target.dataset.hour!=null)inspectHour(+e.target.dataset.hour);});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();if((e.key==='Enter'||e.key===' ')&&e.target.matches('[data-alert],[data-service]')){e.preventDefault();e.target.click();}if(e.key==='Tab'&&!$('modal').hidden){let focusable=$('modal').querySelectorAll('button,input,select,a[href]'),first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});

// ---------- TV operations board: loading / traffic / bunching & gaps ----------
const tvSeen=new Map();let tvPrimed=false;
function freshKey(key){const now=Date.now();if(!tvSeen.has(key))tvSeen.set(key,tvPrimed?now:0);const t=tvSeen.get(key);return t>0&&now-t<300000;}
const localAcks={};
function ackOf(key){return localAcks[key]||state.data?.acks?.[key]||null;}
function withAck(html,key){
 const a=ackOf(key),j=html.lastIndexOf('</article>');if(j<0)return html;
 if(a){const t=new Date(a.ts*1000).toLocaleTimeString('en-SG',{hour:'2-digit',minute:'2-digit',timeZone:'Asia/Singapore'});
  html=html.replace('class="tvb-card','class="tvb-card is-acked has-stamp'+(a.fresh?' is-acking':''));const k=html.lastIndexOf('</article>');
  return html.slice(0,k)+`<div class="tvb-stamp" title="Acknowledged by ${esc(a.who||'controller')} at ${t}"><b>✓ ACKNOWLEDGED</b><small>${esc(a.who||'controller')} · ${t}</small>${state.tv?'':`<button type="button" class="tvb-unack" data-unack="${esc(key)}" title="Undo acknowledgement" aria-label="Undo acknowledgement">↺</button>`}</div>`+html.slice(k);}
 if(state.tv)return html;
 html=html.replace('class="tvb-card','class="tvb-card has-ack');const k=html.lastIndexOf('</article>');
 return html.slice(0,k)+`<button type="button" class="tvb-ack" data-ack="${esc(key)}" title="Acknowledge for every controller" aria-label="Acknowledge">${icon('spark')}<span>ACK</span></button>`+html.slice(k);
}
async function ackCard(key,undo=false){
 if(undo){delete localAcks[key];if(state.data?.acks)delete state.data.acks[key];}
 else localAcks[key]={who:'You',ts:Date.now()/1000,fresh:true};
 if(state.data)renderBoard(state.data);
 try{const r=await fetch('/api/forecast/centre/ack',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({key,undo})});const j=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(j.error||'Could not save acknowledgement');
  if(!undo){localAcks[key]={...localAcks[key],who:j.who||'You'};if(state.data){state.data.acks=state.data.acks||{};state.data.acks[key]={who:j.who||'You',ts:j.ts||localAcks[key].ts};}if(state.data)renderBoard(state.data);}}
 catch(err){$('error').textContent=err.message+' · acknowledged on this screen only';}
 if(!undo)setTimeout(()=>{if(localAcks[key])localAcks[key].fresh=false;},1200);
}
function boardColumn(prefix,rows,card,empty){
 const list=$(prefix+'List');
 // Acknowledged cards stay listed with a small chop (bottom right) and move below the open ones; a card only
 // leaves the panel when the problem itself clears.
 rows=[...rows.filter(r=>!ackOf(r.key)),...rows.filter(r=>ackOf(r.key))];
 const nAck=rows.filter(r=>ackOf(r.key)).length,ackNote=nAck?` · ${nAck} acknowledged`:'';
 if(!rows.length){list.innerHTML=empty;$(prefix+'Page').textContent='';return;}
 list.innerHTML=rows.map(r=>withAck(card(r,freshKey(r.key)),r.key)).join('');
 // Normal view lists every case (the panel scrolls); only TV, which nobody can scroll, pages through them.
 if(!state.tv||typeof getComputedStyle!=='function'||!list.children){$(prefix+'Page').textContent=(rows.length>1?rows.length+' listed':'')+ackNote;return;}   // no layout engine (tests): show all
 const items=[...list.children],gap=parseFloat(getComputedStyle(list).rowGap)||0;
 const pad=parseFloat(getComputedStyle(list).paddingTop)+parseFloat(getComputedStyle(list).paddingBottom)||0;
 const pages=paginate(items.map(x=>x.offsetHeight),list.clientHeight-pad,gap),n=state.page%pages.length,keep=new Set(pages[n]);
 items.forEach((x,i)=>{x.hidden=!keep.has(i);});
 $(prefix+'Page').textContent=pages.length>1?`Page ${n+1}/${pages.length} · rotates 12 s`:'';
}
function renderBoard(d){
 if(!d)return;
 const m=boardModel(d),demo=d.mode==='demo',when=state.h===0?'Now':'Next '+state.h+' min',stale=d.mode==='live'&&Date.now()/1000-d.updated>150;
 const heavyBuses=m.loading.rows.reduce((n,r)=>n+r.heavy,0),bunch=m.headway.rows.filter(r=>r.type==='bunching').length,gaps=m.headway.rows.length-bunch;
 $('tvLoadCount').textContent=m.loading.available?m.loading.rows.length:'—';
 $('tvLoadSub').textContent=!m.loading.available?(d.mode==='live'&&state.h?'Observed load only at NOW':'Load readings unavailable'):demo?`${when} · simulated ≥85% occupancy`:`${when} · ${heavyBuses} bus${heavyBuses===1?'':'es'} at limited standing (LSD)`;
 $('tvLoadNote').textContent=demo?'DEMO · simulated':'Each bus counted in its own LTA band · never averaged';
 boardColumn('tvLoad',m.loading.rows,(r,isNew)=>loadingCard(r,isNew,demo),emptyState(m.loading.available,'heavy loading',d.mode==='live'&&state.h?'Load is observed, not forecast':'Waiting for LTA load readings'));
 $('tvTrafficCount').textContent=m.traffic.available?m.traffic.services:'—';
 $('tvTrafficSub').textContent=m.traffic.available?`${when} · ${m.traffic.rows.length} incident / roadwork / build-up${m.traffic.rows.length===1?'':'s'}`:'Traffic sources unavailable';
 $('tvTrafficNote').textContent=demo?'DEMO · simulated':`Route + direction matched · delay ≥ ${num(state.delayMin,1)} min`;
 boardColumn('tvTraffic',m.traffic.rows,trafficCard,emptyState(m.traffic.available,'incidents affecting services','Traffic feed not live'));
 $('tvHeadwayCount').textContent=m.headway.available?m.headway.rows.length:'—';
 $('tvHeadwaySub').textContent=m.headway.available?`${when} · ${bunch} three-bus bunch${bunch===1?'':'es'} · ${gaps} long gap${gaps===1?'':'s'}`:(d.mode==='live'&&state.h>30?'Headway forecast covers ≤30 min':'Headway data unavailable');
 $('tvHeadwayNote').textContent=demo?'DEMO · simulated':`Same rules as Bunching & Gap · 3BB < ${num(d.rules?.bunch_min??3,0)} min · gap ≥ scheduled + ${num(d.rules?.gap_add_min??10,0)} min · first ${d.rules?.gap_skip_stops??3} stops ignored`;
 boardColumn('tvHeadway',m.headway.rows,headwayCard,emptyState(m.headway.available,'bunching or long gaps',d.mode==='live'&&state.h>30?'Beyond 30-min coverage':'Waiting for headway collector'));
 document.querySelectorAll('.tvq').forEach(x=>x.classList.toggle('is-stale',stale));tvPrimed=true;
}
function refreshTVLayout(){networkMap.map?.resize();if(state.data){networkStatus(state.data);alerts(state.data);analytics(state.data);renderBoard(state.data);if(cameraRows.length)cameras(cameraRows,cameraMeta);}}
let tvResizeTimer;
function resizeTV(){clearTimeout(tvResizeTimer);if(state.tv)tvResizeTimer=setTimeout(refreshTVLayout,120);}
window.addEventListener('resize',resizeTV);document.addEventListener('fullscreenchange',resizeTV);
function measureShell(){document.documentElement.style.setProperty('--shell-height',Math.ceil(document.querySelector('.console').getBoundingClientRect().top)+'px');}
window.addEventListener('resize',measureShell);const shellObserver=new ResizeObserver(()=>{measureShell();resizeTV();});for(const selector of ['.topbar','.controlbar','.filterbar'])shellObserver.observe(document.querySelector(selector));measureShell();
document.addEventListener('visibilitychange',()=>{if(!document.hidden)fetchState();});window.addEventListener('resize',()=>networkMap.map?.resize());
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&e.target.closest('.camera')){let parent=e.target.closest('.camera');parent.innerHTML='<div class="camera-empty">'+icon('camera')+'</div><p>Image unavailable from source</p>';}},true);
document.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));clock();setInterval(clock,1000);controls();
networkMap.init().then(kind=>{$('mapEngine').textContent=kind==='ml'?'Vector map · OpenStreetMap':'Basemap unavailable · geographic fallback';if(kind==='schematic'){$('map3D').disabled=true;$('map3D').title='3D requires WebGL and vector tiles';}});
fetchState().then(loadCatalog);if(state.tv)tv(true);setInterval(()=>{if(!document.hidden&&!state.playing)fetchState();},60000);setInterval(()=>{if(state.data&&!document.hidden){state.page++;alerts(state.data);analytics(state.data);renderBoard(state.data);if(state.tv&&cameraRows.length)cameras(cameraRows,cameraMeta);}},12000);
window.SENTRY2026={state,map:networkMap,refresh:fetchState,tour:{state:spotlight,next:()=>advanceSpotlight(true)}};
