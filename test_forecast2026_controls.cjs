/* Executes the real UI module and event handlers with a minimal DOM fixture.
   Tests interaction wiring; viewport rendering is covered by the browser check. */
const fs=require('fs'),assert=require('assert/strict'),path=require('path');
(async()=>{
 const raw=JSON.parse(fs.readFileSync(path.join(__dirname,'test_forecast2026_demo.json'),'utf8'));raw.history=[];raw.coverage={loading:true,traffic:true,bunching:true,gap:true};
 class Element{
  constructor(id=''){this.id=id;this.dataset={};this.value='';this.style={setProperty(){}};this.hidden=id==='modal';this.clientHeight=350;this.innerHTML='';this.textContent='';this.handlers={};this.classList={add(){},remove(){},toggle(){}};}
  setAttribute(){} addEventListener(type,fn){this.handlers[type]=fn;}querySelectorAll(){return [];}getBoundingClientRect(){return {top:190};}focus(){}checkValidity(){return true;}
 }
 const ids=new Map(),el=id=>{if(!ids.has(id))ids.set(id,new Element(id));return ids.get(id);};
 const handlers={};global.document={getElementById:el,createElement:()=>new Element(),querySelector:s=>el(s),querySelectorAll:()=>[],addEventListener(t,f){(handlers[t]??=[]).push(f);},documentElement:{dataset:{defaultMode:'demo',defaultHorizon:'30'},style:{setProperty(){}}},body:{classList:{add(){},remove(){},toggle(){}}},hidden:false,activeElement:el('active')};
 global.window={addEventListener(){}};global.location={search:'',href:'http://local/forecast'};global.history={replaceState(){}};global.innerWidth=1920;global.ResizeObserver=class{observe(){}disconnect(){}};
 global.setInterval=()=>0;const nativeTimeout=setTimeout;global.setTimeout=(fn,ms)=>ms>10000?0:nativeTimeout(fn,ms);global.clearTimeout=()=>{};
 const requests=[];global.fetch=async input=>{const u=new URL(input,'http://local');requests.push(u);if(u.pathname.includes('/services'))return {json:async()=>({services:[]})};let data=structuredClone(raw);data.horizon=+u.searchParams.get('horizon');data.delay_filter.minimum=+u.searchParams.get('delay_min');const selected=(u.searchParams.get('services')||'').split(',').filter(Boolean);if(selected.length)data.services=data.services.filter(s=>selected.includes(s.svc));return {ok:true,json:async()=>data};};
 const dataURL=s=>'data:text/javascript;base64,'+Buffer.from(s).toString('base64'),core=dataURL(fs.readFileSync(path.join(__dirname,'forecast2026-core.js'),'utf8'));
 const map=dataURL(fs.readFileSync(path.join(__dirname,'forecast2026-map.js'),'utf8').replace(/from '\.\/forecast2026-core\.js\?[^']+'/,'from '+JSON.stringify(core)));
 const tvBoard=dataURL(fs.readFileSync(path.join(__dirname,'forecast2026-tv.js'),'utf8').replace(/from '\.\/forecast2026-core\.js\?[^']+'/,'from '+JSON.stringify(core)));
 let main=fs.readFileSync(path.join(__dirname,'forecast2026.js'),'utf8').replace(/from '\.\/forecast2026-core\.js\?[^']+'/,'from '+JSON.stringify(core)).replace(/from '\.\/forecast2026-map\.js\?[^']+'/,'from '+JSON.stringify(map)).replace(/from '\.\/forecast2026-tv\.js\?[^']+'/,'from '+JSON.stringify(tvBoard));
 await import(dataURL(main));const settle=()=>new Promise(r=>nativeTimeout(r,15));await settle();
 const state=window.SENTRY2026.state;assert.equal(state.mode,'demo');assert(el('riskCards').innerHTML.includes('risk-card'));assert(el('headwayChart').innerHTML.includes('hourly-chart'));
 const click=async dataset=>{let t={dataset,id:'',closest(){return this;}};for(const fn of handlers.click)await fn({target:t});await settle();};
 await click({service:'14'});assert.deepEqual(state.services,['14']);assert(el('selectedServices').innerHTML.includes('data-remove-service="14"'));
 await click({category:'traffic'});assert.deepEqual(state.services,['14']);await click({removeService:'14'});assert.deepEqual(state.services,[]);
 el('delayThreshold').value='custom';el('delayThreshold').onchange();assert.equal(el('customDelayLabel').hidden,false);el('customDelay').value='2.5';el('customDelay').onchange();await settle();assert.equal(state.delayMin,2.5);assert.equal(requests.at(-1).searchParams.get('delay_min'),'2.5');
 el('headwayInterval').value='15';el('headwayInterval').onchange();assert.equal((el('headwayChart').innerHTML.match(/data-hour=/g)||[]).length,96);
 await click({analysis:'loading'});assert.equal(el('loadingModule').hidden,false);assert.equal(el('headwayModule').hidden,true);
 state.services=['14','166'];state.operator='SBST';state.exclude=true;await el('clearFilters').onclick();await settle();assert.deepEqual(state.services,[]);assert.equal(state.operator,'');assert.equal(state.category,'all');assert.equal(state.delayMin,.8);assert.equal(state.h,0);assert(el('selectedServices').innerHTML.includes('FULL NETWORK'));
 const before=JSON.stringify({services:state.services,category:state.category,h:state.h,exclude:state.exclude,operator:state.operator});
 await el('tv').onclick();window.SENTRY2026.tour.next();assert.equal(window.SENTRY2026.tour.state.current,null);window.SENTRY2026.tour.next();assert(window.SENTRY2026.tour.state.current);assert(el('activitySpotlight').innerHTML.includes('DEMO ACTIVITY'));assert.equal(state.headwayScope,window.SENTRY2026.tour.state.current.svc);assert(el('routeSelection').textContent.includes('ROUTE'));
 assert.equal(JSON.stringify({services:state.services,category:state.category,h:state.h,exclude:state.exclude,operator:state.operator}),before);
 el('spotlightToggle').onclick();assert.equal(window.SENTRY2026.tour.state.enabled,false);el('spotlightToggle').onclick();assert.equal(window.SENTRY2026.tour.state.enabled,true);
 for(const handler of handlers.pointerdown)handler({isTrusted:true,target:{closest:()=>null}});assert(window.SENTRY2026.tour.state.pausedUntil>Date.now());assert.equal(window.SENTRY2026.tour.state.current,null);
 await el('exitTV').onclick();assert.equal(state.tv,false);
 console.log('PASS: real UI startup/render and event wiring for service chips/toggle, category persistence, custom delay, headway intervals, module tabs and reset.');
})().catch(e=>{console.error(e.stack?.slice(0,1800)||e);process.exitCode=1;});
