export const HORIZONS=[0,15,30,60,120];
export const COLORS={loading:'#ffb64a',traffic:'#ff695b',bunching:'#f755b5',gap:'#61c8ff',normal:'#39e4a2',route:'#47ddff'};
export const TITLES={loading:'Heavy passenger loading',traffic:'Traffic buildup',bunching:'Three-bus bunching',gap:'Long headway gaps'};
export const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const num=(v,d=0)=>v==null?'—':Number(v).toFixed(d);
export function apiURL(state){const p=new URLSearchParams({mode:state.mode,horizon:state.h,scenario:state.scenario,tick:state.tick,services:state.services.join(','),exclude_cbs:state.exclude?'1':'0',category:state.category,delay_min:state.delayMin??.8});if(state.routesV)p.set('routes_v',state.routesV);return '/api/forecast/centre?'+p;}
export function quality(mode,updated,now=Date.now()/1000,error=false){if(mode==='demo')return {label:'DEMO · SIMULATED',kind:'demo'};if(!updated)return {label:'OFFLINE',kind:'offline'};if(now-updated>150)return {label:'DATA DELAYED',kind:'delayed'};return {label:error?'FORECAST DEGRADED':'LIVE · ESTIMATED FORECAST',kind:error?'delayed':'live'};}
export function riskServices(data){return [...(data.services||[])].map(s=>({...s,risk:(s.blocked?100:0)+(s.three||0)*30+(s.gaps||0)*25+(s.heavy_buses||0)*5+(s.delay||0)})).filter(s=>s.risk>0).sort((a,b)=>b.risk-a.risk);}
export function slicePage(rows,page,size){const pages=Math.max(1,Math.ceil(rows.length/size));return {rows:rows.slice((page%pages)*size,(page%pages+1)*size),page:page%pages+1,pages};}
export function badge(category){return `<span class="risk-dot" style="--risk:${COLORS[category]||COLORS.normal}"></span>`;}
export function icon(kind){const paths={road_closed:'<circle cx="12" cy="12" r="9"/><path d="M5 12h14"/>',bus:'<rect x="5" y="3" width="14" height="16" rx="3"/><path d="M5 10h14M8 19v2m8-2v2M8 15h1m6 0h1"/>',loading:'<circle cx="8" cy="6" r="3"/><circle cx="17" cy="7" r="2"/><path d="M3 21v-7a5 5 0 0 1 10 0v7m2-9a4 4 0 0 1 6 3v6M8 13v8"/>',traffic:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3v1"/>',roadworks:'<path d="m9 3-6 18h18L15 3ZM6 12h12M5 16h14"/>',bunching:'<rect x="1" y="6" width="6" height="12" rx="1"/><rect x="9" y="6" width="6" height="12" rx="1"/><rect x="17" y="6" width="6" height="12" rx="1"/>',gap:'<path d="M2 8h4v8H2m16-8h4v8h-4M8 12h8m-5-3-3 3 3 3m2-6 3 3-3 3"/>',rain:'<path d="M6 14a5 5 0 1 1 9-6 4 4 0 0 1 3 8H6m1 2-1 3m6-3-1 3m6-3-1 3"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',camera:'<rect x="2" y="6" width="20" height="15" rx="2"/><circle cx="12" cy="13" r="4"/><path d="m6 6 2-3h8l2 3"/>',spark:'<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5Z"/>'};return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[kind]||paths.traffic}</svg>`;}
export function chartSVG(series,{color='#49d5ff',labels=[],max=null,bars=false,unit=''}={}){
 const w=400,h=118,p=18,good=series.filter(x=>x!=null),top=max||Math.max(1,...good)*1.15;
 const x=i=>p+i*(w-p*2)/Math.max(1,series.length-1),y=v=>h-24-(v/top)*(h-38);
 let svg=`<svg class="mini-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(unit)} forecast chart">`;
 for(let j=0;j<3;j++){let yy=y(top*j/2);svg+=`<path d="M${p} ${yy}H${w-p}" stroke="#254055" stroke-dasharray="3 5"/><text x="2" y="${yy+3}">${num(top*j/2)}${esc(unit)}</text>`;}
 let segments=[],current=[];series.forEach((v,i)=>{if(v==null){if(current.length)segments.push(current);current=[];}else current.push([x(i),y(v)]);});if(current.length)segments.push(current);
 if(bars)series.forEach((v,i)=>{if(v!=null)svg+=`<rect x="${x(i)-8}" y="${y(v)}" width="16" height="${h-24-y(v)}" rx="2" fill="${v>=85?'#ff6479':color}"/>`;});
 else segments.forEach(points=>{svg+=`<polyline points="${points.map(p=>p.join(',')).join(' ')}" stroke="${color}" fill="none" stroke-width="2.5"/>`;points.forEach(([xx,yy])=>svg+=`<circle cx="${xx}" cy="${yy}" r="3" fill="${color}"/>`);});
 labels.forEach((l,i)=>svg+=`<text x="${x(i)}" y="${h-4}" text-anchor="middle">${esc(l)}</text>`);
 return svg+'</svg>';
}

export function operationalQueue(data,history=false){if(history)return data.history||[];const current=new Set((data.alerts||[]).map(a=>a.id));const retained=(data.history||[]).filter(a=>a.status!=='resolved'&&!current.has(a.id)).map(a=>({...a,evidence:'stale'}));return [...(data.alerts||[]),...retained];}

export function initialView(query,defaults={mode:'live',horizon:0}){const mode=query.has('mode')?query.get('mode'):defaults.mode;const h=query.has('h')?Number(query.get('h')):Number(defaults.horizon);return {mode:mode==='demo'?'demo':'live',h:HORIZONS.includes(h)?h:Number(defaults.horizon)||0};}

export function loadSummary(data){const counts={SEA:0,SDA:0,LSD:0,UNKNOWN:0};for(const s of data.services||[])for(const c of Object.keys(counts))counts[c]+=s.load_counts?.[c]||0;return {...counts,known:counts.SEA+counts.SDA+counts.LSD,total:Object.values(counts).reduce((a,b)=>a+b,0)};}
export function stopText(stop){if(!stop)return 'Bus stop / sequence unavailable';return [stop.sequence!=null?'Seq '+stop.sequence:null,stop.code?'BS '+stop.code:null,stop.name].filter(Boolean).join(' · ')||'Bus stop / sequence unavailable';}
export function watchLocation(s,category){if(category==='traffic'){let t=s.traffic_details?.[0];if(t)return (t.stops?.length?t.stops.map(stopText).join(' → '):'Bus stop / sequence unavailable')+' · '+t.road;}if(category==='bunching'){let b=s.bunching_details?.[0];if(b)return stopText(b.stop)+' · '+(b.location||s.location);}if(category==='gap'){let g=s.gap_details?.[0];if(g)return stopText(g.stop)+' · '+(g.location||s.location);}return s.location||'Location not supplied';}
export function headwayView(data,scope='selected'){let selected=data.services?.find(s=>s.gaps)||data.services?.find(s=>s.three)||data.services?.find(s=>s.focus_headway!=null)||data.services?.[0];const number=x=>typeof x==='number'&&Number.isFinite(x);let counts=[];let values=(data.service_series||[]).map(t=>{let rows=scope==='network'?t.services:t.services.filter(s=>s.svc===selected?.svc&&s.dir===selected?.dir);let available=rows.map(s=>s.focus_headway).filter(number);counts.push(available.length);return available.length?available.reduce((a,b)=>a+b,0)/available.length:null;});return {selected,values,counts,scope};}

export const toggleService=(selected,svc)=>selected.includes(svc)?selected.filter(s=>s!==svc):[...selected,svc];
export function spotlightDeck(data,random=Math.random,now=Date.now()/1000){
 if(!data||data.mode==='live'&&(!data.updated||now-data.updated>150))return [];
 const allowed=new Set((data.services||[]).map(s=>s.svc+':'+s.dir)),seen=new Set();
 const rows=(data.alerts||[]).filter(a=>a.status!=='resolved'&&a.evidence!=='stale'&&allowed.has(a.svc+':'+a.dir)&&!seen.has(a.id)&&seen.add(a.id));
 rows.sort((a,b)=>(b.first||b.updated||b.last||data.updated)-(a.first||a.updated||a.last||data.updated)||(b.score||0)-(a.score||0));
 const latest=rows.shift();for(let i=rows.length-1;i>0;i--){const j=Math.floor(random()*(i+1));[rows[i],rows[j]]=[rows[j],rows[i]];}
 return latest?[latest,...rows]:[];
}
export const resetFilters=()=>({services:[],operator:'',exclude:false,category:'all',delayMin:.8});
export const delayColor=(delay,thresholds={yellow:.8,orange:2,red:5})=>delay==null?'#7d9bab':delay>=thresholds.red?'#ff4d63':delay>=thresholds.orange?'#ff9b43':delay>=thresholds.yellow?'#f2df5e':'#39e4a2';
export function hourlyHeadway(data,{interval=60,scope='network',direction='all'}={}){
 const now=Number(data.generated)||Date.now()/1000,day=Math.floor((now+8*3600)/86400)*86400-8*3600;
 const start=day+(now<day+5*3600?-19:5)*3600,end=start+24*3600;
 const selected=(data.services||[]).filter(s=>(scope==='network'||s.svc===scope)&&(direction==='all'||String(s.dir)===String(direction)));
 const keys=new Set(selected.map(s=>s.svc+':'+s.dir)),rows=Array.from({length:24*60/interval},(_,i)=>({at:start+i*interval*60,label:new Date((start+i*interval*60)*1000).toLocaleTimeString('en-SG',{timeZone:'Asia/Singapore',hour:'2-digit',minute:'2-digit',hour12:false}),scheduled:null,actual:null,forecast:null,confidence:null,n:0}));
 const buckets=rows.map(()=>({scheduled:[],actual:[],forecast:[],confidence:[],actualTimes:[],forecastTimes:[]}));
 const add=(at,kind,v,confidence)=>{let i=Math.floor((at-start)/(interval*60));if(i<0||i>=rows.length||!Number.isFinite(v)||v<=0)return;buckets[i][kind].push(v);if(kind!=='scheduled')buckets[i][kind+'Times'].push(at);if(kind==='forecast'&&Number.isFinite(confidence))buckets[i].confidence.push(confidence);};
 const avg=xs=>xs.length?xs.reduce((n,v)=>n+v,0)/xs.length:null;
 if(data.mode==='demo'){
  rows.forEach((r,i)=>{let hour=((r.at+8*3600)%86400)/3600,scheduled=avg(selected.map(s=>s.scheduled).filter(Number.isFinite));if(scheduled==null)return;add(r.at,'scheduled',scheduled);if(r.at+interval*60/2<now)add(r.at,'actual',scheduled+Math.max(-2,Math.sin(hour*1.5)*2.5+Math.max(0,...selected.map(s=>s.delay||0))*.25));});
 }else{
  // Only records explicitly marked verified actual arrivals enter the actual line.
  for(const r of data.headway_history||[])if(r.verified===true&&r.kind==='actual'&&r.at<=now&&keys.has(r.svc+':'+r.dir))add(r.at,'actual',r.headway);
  for(const r of data.headway_schedule||[])if(r.verified===true&&keys.has(r.svc+':'+r.dir))add(r.at,'scheduled',r.headway);
  if(!(data.headway_schedule||[]).length)for(const s of selected)add(now,'scheduled',s.scheduled);
 }
 for(const t of data.service_series||[])for(const s of t.services||[])if(keys.has(s.svc+':'+s.dir)&&t.h>0&&(data.mode==='demo'||t.h<=30))add(now+t.h*60,'forecast',s.focus_headway,s.confidence);
 rows.forEach((r,i)=>{const b=buckets[i];for(const k of ['scheduled','actual','forecast','confidence'])r[k]=avg(b[k]);r.actual_at=avg(b.actualTimes);r.forecast_at=avg(b.forecastTimes);r.n=b.actual.length;r.difference=r.actual!=null&&r.scheduled!=null?r.actual-r.scheduled:null;r.abnormal=r.scheduled!=null&&((r.actual!=null&&r.actual-r.scheduled>=5)||(r.forecast!=null&&r.forecast-r.scheduled>=5));});
 return {rows,start,end,now,interval,demo:data.mode==='demo',directions:selected.length,actualAvailable:rows.some(r=>r.actual!=null),forecastAvailable:rows.some(r=>r.forecast!=null),scheduleAvailable:rows.some(r=>r.scheduled!=null)};
}
export function hourlyChart(view,width=900,height=200,fontSize=13){
 const w=Math.max(300,width),h=Math.max(90,height),p={l:40,r:18,t:22,b:28},x=t=>p.l+(t-view.start)/(view.end-view.start)*(w-p.l-p.r),max=Math.max(10,...view.rows.flatMap(r=>[r.actual,r.scheduled,r.forecast]).filter(Number.isFinite))*1.18,y=v=>h-p.b-v/max*(h-p.t-p.b),boundary=x(view.now);
 let svg=`<svg class="hourly-chart" style="--chart-label-size:${fontSize}px" viewBox="0 0 ${w} ${h}" role="img" aria-label="Hourly scheduled, actual and forecast headway; missing data is not plotted"><rect x="${boundary}" y="${p.t}" width="${Math.max(0,w-p.r-boundary)}" height="${h-p.b-p.t}" fill="#27648a" opacity=".12"/>`;
 for(let i=0;i<=3;i++){let v=max*i/3;svg+=`<path d="M${p.l} ${y(v)}H${w-p.r}" stroke="#29475a" stroke-dasharray="3 5"/><text x="${p.l-8}" y="${y(v)+5}" text-anchor="end">${num(v)}</text>`;}
 for(const [kind,color,dash] of [['scheduled','#92a8bd','7 5'],['actual','#40ddb5',''],['forecast','#61c8ff','5 5']]){
  let previous=null;view.rows.forEach((r,i)=>{if(r[kind]==null){previous=null;return;}const at=kind==='scheduled'?r.at+view.interval*30:r[kind+'_at']??r.at+view.interval*30;let pt=[x(at),y(r[kind])];if(previous)svg+=`<path d="M${previous.join(' ')}L${pt.join(' ')}" stroke="${color}" stroke-width="3" stroke-dasharray="${dash}"/>`;svg+=`<circle cx="${pt[0]}" cy="${pt[1]}" r="4" fill="${color}"/>`;previous=pt;});
 }
 view.rows.forEach((r,i)=>{const xx=x(r.at);if(i%Math.max(1,Math.round((Math.ceil(1440/Math.max(3,Math.floor(width/75))/60)*60)/view.interval))===0)svg+=`<text x="${xx}" y="${h-13}" text-anchor="middle">${r.label}</text>`;if(r.abnormal)svg+=`<rect x="${xx}" y="${p.t}" width="${(w-p.l-p.r)*view.interval/1440}" height="${h-p.b-p.t}" fill="#ff5865" opacity=".13"/>`;svg+=`<rect data-hour="${i}" tabindex="0" role="button" aria-label="${esc(r.label)} headway values" x="${xx}" y="${p.t}" width="${(w-p.l-p.r)*view.interval/1440}" height="${h-p.b-p.t}" fill="transparent"><title>${r.label} SGT · Scheduled ${num(r.scheduled,1)} · Actual ${num(r.actual,1)} · Forecast ${num(r.forecast,1)} min</title></rect>`;});
 svg+=`<path d="M${boundary} ${p.t}V${h-p.b}" stroke="#f1cb7c" stroke-dasharray="4 4"/><text x="${Math.max(100,Math.min(w-95,boundary))}" y="15" text-anchor="middle" fill="#f1cb7c">NOW · FORECAST →</text></svg>`;
 return svg;
}
// Card selection is applied in the browser to the full snapshot, so selecting / clearing a card is instant.
// Mirrors build_centre(category=...) in forecast2026.py.
export function categoryView(data,category='all'){
 if(!data||!category||category==='all'||!TITLES[category])return data;
 const alerts=(data.alerts||[]).filter(a=>a.category===category),ids=new Set(alerts.map(a=>a.svc));
 let events=[];
 if(category==='traffic'){events=data.events||[];for(const e of events)for(const s of e.svc?[e.svc]:(e.services||[]))ids.add(s);}
 const keep=rows=>(rows||[]).filter(x=>ids.has(x.svc));
 let services=keep(data.services);if(category!=='loading')services=services.map(s=>({...s,load_details:[]}));
 const alertIds=new Set(alerts.map(a=>a.id));
 return {...data,category,alerts,events,services,buses:keep(data.buses),routes:keep(data.routes),stops:keep(data.stops),
  recommendations:(data.recommendations||[]).filter(r=>alertIds.has(r.id)),history:(data.history||[]).filter(a=>a.category===category)};
}
