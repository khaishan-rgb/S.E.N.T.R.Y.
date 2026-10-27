const fs=require('fs'),assert=require('assert/strict'),path=require('path');
(async()=>{
 const url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64');
 const coreURL=url(fs.readFileSync(path.join(__dirname,'forecast2026-core.js'),'utf8')),core=await import(coreURL);
 assert.deepEqual(core.toggleService(['14','166'],'14'),['166']);assert.deepEqual(core.toggleService(['14'],'166'),['14','166']);assert.deepEqual(core.toggleService(['14'],'14'),[]);
 assert.deepEqual(core.resetFilters(),{services:[],operator:'',exclude:false,category:'all',delayMin:.8});
 assert.equal(new URL(core.apiURL({...core.resetFilters(),mode:'live',h:0,scenario:'combined',tick:0,delayMin:3}),'http://local').searchParams.get('delay_min'),'3');
 for(const [d,c] of [[.79,'#39e4a2'],[.8,'#f2df5e'],[1.99,'#f2df5e'],[2,'#ff9b43'],[4.99,'#ff9b43'],[5,'#ff4d63']])assert.equal(core.delayColor(d),c);
 const now=Date.parse('2026-10-27T06:00:00Z')/1000;
 const data={mode:'live',generated:now,services:[{svc:'14',dir:1,scheduled:10},{svc:'14',dir:2,scheduled:12},{svc:'166',dir:1,scheduled:8}],service_series:[{h:0,services:[{svc:'14',dir:1,focus_headway:5}]},{h:15,services:[{svc:'14',dir:1,focus_headway:18,confidence:.7}]},{h:30,services:[{svc:'14',dir:1,focus_headway:20}]},{h:60,services:[{svc:'14',dir:1,focus_headway:88}]}],headway_history:[{svc:'14',dir:1,at:now-7200,headway:50,kind:'eta',verified:true},{svc:'14',dir:1,at:now-7200,headway:22,kind:'actual',verified:false}]};
 let chart=core.hourlyHeadway(data,{scope:'14',direction:'1',interval:60});assert.equal(chart.actualAvailable,false);assert.equal(chart.rows.length,24);assert.equal(chart.rows[0].label,'05:00');assert.equal(chart.rows.at(-1).label,'04:00');assert.equal(chart.rows.find(r=>r.forecast!=null).forecast,19);assert(chart.rows.every(r=>r.forecast!==88));assert.equal(chart.rows.filter(r=>r.scheduled!=null).length,1);
 data.headway_history.push({svc:'14',dir:1,at:now-7200,headway:18,kind:'actual',verified:true});
 data.headway_schedule=[{svc:'14',dir:1,at:now-7200,headway:10,verified:true}];
 chart=core.hourlyHeadway(data,{scope:'14',direction:'1',interval:15});assert.equal(chart.rows.length,96);assert(chart.actualAvailable);let actual=chart.rows.find(r=>r.actual!=null);assert.equal(actual.actual,18);assert.equal(actual.difference,8);assert(actual.abnormal);assert.equal(core.hourlyHeadway(data,{scope:'14',direction:'2'}).actualAvailable,false);
 assert(core.hourlyChart(chart).includes('NOW · FORECAST'));assert(core.hourlyChart(chart).includes('data-hour='));assert(!core.hourlyChart(chart).includes('NaN'));
 assert(core.hourlyHeadway({...data,mode:'demo'},{scope:'14',direction:'1'}).demo);
 const elements=[];global.document={createElement(){let el={style:{setProperty(){}},setAttribute(){},addEventListener(){}};elements.push(el);return el;}};
 let created=0,removed=0;
 class Marker{constructor({element}){this.el=element;created++;}setLngLat(p){this.point=p;return this;}setPopup(p){this.popup=p;return this;}getPopup(){return this.popup;}addTo(){return this;}setOffset(o){this.offset=o;return this;}remove(){removed++;}}
 class Popup{setHTML(h){this.html=h;return this;}}
 global.maplibregl={Marker,Popup};
 const mapCode=fs.readFileSync(path.join(__dirname,'forecast2026-map.js'),'utf8').replace(/from '\.\/forecast2026-core\.js\?[^']+'/,'from '+JSON.stringify(coreURL));
 const {ForecastMap}=await import(url(mapCode)),map=new ForecastMap({},()=>{}),sources={};
 let fitted=0;map.kind='ml';map.map={resize(){},project(p){return {x:p[0]*100,y:p[1]*100}},getSource(id){return {setData(v){sources[id]=v;}}},fitBounds(){fitted++}};
 const snap={mode:'live',horizon:0,updated:now,delay_filter:{yellow:.8,orange:2,red:5},routes:[{svc:'14',dir:1,line:[[103.8,1.3],[103.81,1.31]]}],context_routes:[{svc:'166',dir:1,line:[[103.82,1.3],[103.83,1.31]]}],events:[{id:'e',kind:'jam',road:'Test Road',services:['14'],service_dirs:[['14',1]],point:[103.8,1.3],line:[[103.8,1.3],[103.81,1.31]],delay:2,congestion_eligible:true}],alerts:[{id:'a',category:'gap',point:[103.8,1.3],svc:'14',dir:1,location:'Test stop',impact:'20 min',evidence:'estimate'}],buses:[],stops:[]};
 map.update(snap);assert.equal(created,2);assert.equal(fitted,0);assert.equal(sources['sentry-flow'].features[0].properties.color,'#ff9b43');assert.equal(sources['sentry-routes'].features.find(f=>f.properties.active===false).properties.opacity,.18);
 const marker=map.markers.get('event:e').marker;map.update({...snap,delay_filter:{...snap.delay_filter,minimum:3}});assert.equal(created,2);assert.equal(removed,0);assert.equal(map.markers.get('event:e').marker,marker);assert.equal(fitted,0);assert.notDeepEqual([...map.markers.values()][0].marker.offset,[...map.markers.values()][1].marker.offset);
 map.update({...snap,events:[]});assert.equal(removed,1);assert.equal(map.markers.size,1);
 console.log('PASS: removable service filters/reset, shared delay query and exact severity boundaries, verified hourly data / direction / intervals, chart tooltips and keyed map updates without viewport reset.');
})().catch(e=>{console.error(e);process.exitCode=1;});
