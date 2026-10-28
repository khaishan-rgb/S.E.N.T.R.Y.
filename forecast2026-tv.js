// TV operations board: answers three questions only.
//  1. Which service / direction / location has heavy passenger loading?
//  2. Which incident, roadwork or traffic build-up affects which service / direction / location?
//  3. Where is there three-bus bunching or a long headway gap (service / direction / location)?
import {esc,num,icon} from './forecast2026-core.js?v=20261028-hz6';

export const KINDS={
 road_closed:{label:'Closure reported',icon:'road_closed',rank:0},
 accident:{label:'Accident',icon:'traffic',rank:1},
 incident:{label:'Incident',icon:'traffic',rank:1},
 roadworks:{label:'Roadworks',icon:'roadworks',rank:2},
 jam:{label:'Traffic build-up',icon:'traffic',rank:3},
 congestion:{label:'Traffic build-up',icon:'traffic',rank:3},
 rain:{label:'Heavy rain',icon:'rain',rank:4},
 weather:{label:'Weather',icon:'rain',rank:4}
};
const kindOf=k=>KINDS[k]||{label:k?String(k).replace(/_/g,' '):'Traffic build-up',icon:'traffic',rank:3};
const finite=v=>typeof v==='number'&&Number.isFinite(v);

// Stop name first (what a controller recognises), code / sequence second.
export function place(stop,fallback){
 if(stop&&(stop.name||stop.code)){const meta=[stop.code?'BS '+stop.code:null,stop.sequence!=null?'Seq '+stop.sequence:null].filter(Boolean).join(' · ');return {name:stop.name||'BS '+stop.code,meta};}
 return {name:fallback||'Location not supplied',meta:''};
}
function uniqueStops(list){const seen=new Set(),out=[];for(const s of list){if(!s)continue;const k=s.code||s.name;if(!k||seen.has(k))continue;seen.add(k);out.push(s);}return out;}
function stretch(stops,fallback){
 const list=uniqueStops(stops||[]);
 if(!list.length)return place(null,fallback);
 const a=place(list[0]),b=list.length>1?place(list.at(-1)):null;
 return {name:b?a.name+' → '+b.name:a.name,meta:[a.meta,b?b.meta:null,list.length>2?list.length+' stops affected':null].filter(Boolean).join(' · '),count:list.length};
}

export function loadingRows(data){
 const rows=[];
 for(const s of data.services||[]){
  const counts=s.load_counts||{},heavy=s.heavy_buses;  // null = no observed reading at this horizon
  if(!(heavy>0))continue;
  const known=(counts.SEA||0)+(counts.SDA||0)+(counts.LSD||0);
  const details=s.load_details||[],stops=uniqueStops(details.map(x=>x.stop));
  const where=stops.length?place(stops[0]):place(null,details.find(x=>x.near)?.near||s.location);
  if(stops.length>1)where.meta=[where.meta,'+'+(stops.length-1)+' more stop'+(stops.length>2?'s':'')].filter(Boolean).join(' · ');
  rows.push({key:'load:'+s.svc+':'+s.dir,svc:s.svc,dir:s.dir,heavy,known:known||null,occupancy:finite(s.occupancy)?s.occupancy:null,where,buses:details.map(x=>x.bus).filter(Boolean)});
 }
 return rows.sort((a,b)=>b.heavy-a.heavy||(b.occupancy||0)-(a.occupancy||0)||String(a.svc).localeCompare(String(b.svc),undefined,{numeric:true}));
}

export function trafficGroups(data){
 const services=data.services||[],byKey=new Map(services.map(s=>[s.svc+':'+s.dir,s])),covered=new Set(),groups=[];
 for(const e of data.events||[]){
  let impacts=(e.impacts||[]).map(i=>({...i}));
  if(!impacts.length)for(const s of services)for(const t of s.traffic_details||[])if(t.event_id===e.id)impacts.push({svc:s.svc,dir:s.dir,stops:t.stops,delay:t.delay});
  if(!impacts.length&&e.svc&&byKey.size){const s=services.find(x=>x.svc===e.svc);if(s)impacts.push({svc:s.svc,dir:s.dir,stops:s.traffic_details?.[0]?.stops||[],delay:e.delay});}
  if(!impacts.length)continue;
  const seen=new Set();impacts=impacts.filter(i=>{const k=i.svc+':'+i.dir;if(seen.has(k))return false;seen.add(k);covered.add(e.id+'|'+k);return true;});
  const blocked=impacts.some(i=>byKey.get(i.svc+':'+i.dir)?.blocked);  // a reported closure alone is not 'BLOCKED' (buses may still pass)
  const building=!!e.building||impacts.some(i=>byKey.get(i.svc+':'+i.dir)?.building);
  groups.push({key:'event:'+e.id,kind:e.kind,road:e.road||'Road not supplied',delay:finite(e.delay)?e.delay:null,blocked,building,
   impacts:impacts.map(i=>({svc:i.svc,dir:i.dir,where:stretch(i.stops,e.road)})).sort((a,b)=>String(a.svc).localeCompare(String(b.svc),undefined,{numeric:true})||a.dir-b.dir)});
 }
 // Service delays without a matched incident: route-speed build-up.
 const coveredService=k=>[...covered].some(c=>c.endsWith('|'+k));
 for(const s of services){
  const k=s.svc+':'+s.dir;if(coveredService(k))continue;
  const delayed=s.blocked||s.building&&s.traffic_eligible!==false||finite(s.delay)&&s.delay>0;
  if(!delayed)continue;
  const t=s.traffic_details?.[0];
  groups.push({key:'svc:'+k,kind:s.blocked?'road_closed':t?.kind||'jam',road:t?.road||s.location||'Road not supplied',delay:finite(s.delay)?s.delay:null,blocked:!!s.blocked,building:!!s.building,
   impacts:[{svc:s.svc,dir:s.dir,where:stretch(t?.stops,s.location)}]});
 }
 return groups.sort((a,b)=>(b.blocked-a.blocked)||kindOf(a.kind).rank-kindOf(b.kind).rank||(b.delay||0)-(a.delay||0)||b.impacts.length-a.impacts.length);
}

export function headwayRows(data){
 const rows=[];
 for(const s of data.services||[]){
  if(s.three>0)(s.bunching_details?.length?s.bunching_details:[{location:s.location}]).forEach((g,i)=>rows.push({key:'3bb:'+s.svc+':'+s.dir+':'+(g.bus_order||[]).join('-')+':'+i,type:'bunching',svc:s.svc,dir:s.dir,where:place(g.stop,g.location||s.location),buses:g.bus_order||[],headways:(g.headways||[]).filter(finite),onset:finite(g.onset)&&g.onset>0?g.onset:null}));
  if(s.gaps>0)(s.gap_details?.length?s.gap_details:[{location:s.location}]).forEach((g,i)=>{const hw=finite(g.headway)?g.headway:finite(s.headway)?s.headway:null;rows.push({key:'gap:'+s.svc+':'+s.dir+':'+(g.lead||'')+':'+i,type:'gap',svc:s.svc,dir:s.dir,where:place(g.stop,g.location||s.location),headway:hw,scheduled:finite(s.scheduled)?s.scheduled:null,buses:[g.lead,g.follow].filter(Boolean)});});
 }
 return rows.sort((a,b)=>(a.type===b.type?0:a.type==='bunching'?-1:1)||(b.headway||0)-(a.headway||0)||String(a.svc).localeCompare(String(b.svc),undefined,{numeric:true}));
}

export function boardModel(data){
 const cov=data.coverage||{};
 const traffic=trafficGroups(data);
 return {
  loading:{rows:cov.loading?loadingRows(data):[],available:!!cov.loading},
  traffic:{rows:traffic,available:!!cov.traffic,services:new Set(traffic.flatMap(g=>g.impacts.map(i=>i.svc+':'+i.dir))).size},
  headway:{rows:cov.bunching||cov.gap?headwayRows(data):[],available:!!(cov.bunching||cov.gap)}
 };
}

// ---------- rendering ----------
const svcBadge=(svc,dir)=>`<div class="tvb-svc" data-service="${esc(svc)}" title="Show service ${esc(svc)} on the map"><b>${esc(svc)}</b><span>DIR ${esc(dir)}</span></div>`;
const whereHTML=w=>`<div class="tvb-where"><strong title="${esc(w.name)}">${esc(w.name)}</strong>${w.meta?`<small>${esc(w.meta)}</small>`:''}</div>`;
const newTag=isNew=>isNew?'<em class="tvb-new">NEW</em>':'';

export function loadingCard(r,isNew=false,demo=false){
 const metric=demo&&r.occupancy!=null?`${num(r.occupancy)}%`:`${r.heavy}${r.known?'<i>/'+r.known+'</i>':''}`;
 const label=demo&&r.occupancy!=null?'simulated load':'buses heavy';
 return `<article class="tvb-card" data-key="${esc(r.key)}">${svcBadge(r.svc,r.dir)}${whereHTML(r.where)}<div class="tvb-metric"><b>${metric}</b><small>${label}</small></div>${r.buses.length?`<p class="tvb-foot">${newTag(isNew)}Buses ${r.buses.slice(0,4).map(b=>'#'+esc(b)).join(' · ')}${r.buses.length>4?' +'+(r.buses.length-4):''}</p>`:isNew?`<p class="tvb-foot">${newTag(true)}</p>`:''}</article>`;
}
export function trafficCard(g,isNew=false){
 const k=kindOf(g.kind),impact=g.blocked?'BLOCKED':g.delay!=null&&g.delay>=0.05?'+'+num(g.delay,1)+' min':g.delay!=null?'No delay yet':'Delay n/a';
 return `<article class="tvb-card tvb-event ${g.blocked?'is-blocked':''}" data-key="${esc(g.key)}"><header class="tvb-event-head"><span class="tvb-kind">${icon(k.icon)}${esc(k.label)}${g.building?'<em class="tvb-building">BUILDING</em>':''}${newTag(isNew)}</span><b class="tvb-impact">${impact}</b></header><p class="tvb-road" title="${esc(g.road)}">${esc(g.road)}</p><ul class="tvb-impacts">${g.impacts.slice(0,6).map(i=>`<li>${svcBadge(i.svc,i.dir)}${whereHTML(i.where)}</li>`).join('')}${g.impacts.length>6?`<li class="tvb-more">+${g.impacts.length-6} more service-directions</li>`:''}</ul></article>`;
}
export function headwayCard(r,isNew=false){
 const bunch=r.type==='bunching';
 const metric=bunch?(r.headways.length?r.headways.map(v=>num(v,1)).join(' + '):'3 buses'):(r.headway!=null?num(r.headway)+' min':'Gap');
 const sub=bunch?(r.headways.length?'min apart':'together'):(r.scheduled!=null?'sched '+num(r.scheduled)+' · +'+num((r.headway||0)-r.scheduled)+' min':'headway');
 const foot=bunch?(r.buses.length?r.buses.map(b=>'#'+esc(b)).join(' → '):'Bus order unavailable')+(r.onset?' · forms in '+num(r.onset)+' min':''):(r.buses.length?'Between '+r.buses.map(b=>'#'+esc(b)).join(' and '):'');
 return `<article class="tvb-card tvb-${r.type}" data-key="${esc(r.key)}">${svcBadge(r.svc,r.dir)}<div class="tvb-where"><span class="tvb-type">${bunch?icon('bunching')+'3-bus bunching':icon('gap')+'Long headway gap'}</span><strong title="${esc(r.where.name)}">${esc(r.where.name)}</strong>${r.where.meta?`<small>${esc(r.where.meta)}</small>`:''}</div><div class="tvb-metric"><b>${metric}</b><small>${esc(sub)}</small></div>${foot||isNew?`<p class="tvb-foot">${newTag(isNew)}${foot}</p>`:''}</article>`;
}

export function emptyState(available,what,unavailable){
 return available?`<div class="tvb-empty"><b>${icon('spark')}</b><strong>No ${esc(what)}</strong><small>Within current data and filters</small></div>`
  :`<div class="tvb-empty is-unavailable"><b>—</b><strong>Data unavailable</strong><small>${esc(unavailable)} · not a clearance</small></div>`;
}

// Greedy pagination from measured card heights, so whole cards are never cut off.
export function paginate(heights,available,gap=0){
 const pages=[];let page=[],used=0;
 heights.forEach((h,i)=>{const need=(page.length?gap:0)+h;if(page.length&&used+need>available){pages.push(page);page=[];used=0;}page.push(i);used+=page.length>1?gap+h:h;});
 if(page.length)pages.push(page);
 return pages.length?pages:[[]];
}
