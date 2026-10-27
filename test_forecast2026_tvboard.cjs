// Unit checks for the TV operations board (no browser). Run: node test_forecast2026_tvboard.cjs
const assert=require('assert');
(async()=>{
 const m=await import('./forecast2026-tv.js');
 const stop=(code,name,sequence)=>({code,name,sequence});
 const data={mode:'live',coverage:{loading:true,traffic:true,bunching:true,gap:true},
  services:[
   {svc:'105',dir:1,location:'Novena',heavy_buses:2,load_counts:{SEA:1,SDA:1,LSD:2},load_details:[{bus:'B22',stop:stop('40249',"St. Joseph's Instn",15)},{bus:'B30',stop:stop('40249',"St. Joseph's Instn",15)}],
    three:1,bunching_details:[{bus_order:['B22','B30','B34'],stop:stop('40249',"St. Joseph's Instn",15),headways:[.1,1.1]}],gaps:0},
   {svc:'105',dir:2,blocked:true,heavy_buses:0,load_counts:{},traffic_details:[{event_id:'E1',kind:'road_closed',road:'Thomson Rd',stops:[stop('51101','Bef Thomson Flyover',43)]}]},
   {svc:'14',dir:1,location:'Bedok',heavy_buses:null,load_counts:{LSD:4},gaps:1,headway:23,scheduled:10,gap_details:[{stop:stop('84009','Bedok Int',1),headway:23,lead:'A',follow:'B'}]},
   {svc:'36',dir:2,location:'Tanjong Katong Rd',delay:3.2,traffic_eligible:true,building:true,traffic_details:[]}],
  events:[{id:'E1',kind:'road_closed',road:'Thomson Rd',delay:null,impacts:[{svc:'105',dir:2,stops:[stop('51101','Bef Thomson Flyover',43),stop('51109','Opp Thomson Plaza',45)]}]},
          {id:'E9',kind:'accident',road:'PIE',delay:5,impacts:[]}]};
 const b=m.boardModel(data);
 // 1. heavy loading: service, direction, stop; null readings never counted
 assert.deepStrictEqual(b.loading.rows.map(r=>[r.svc,r.dir,r.heavy,r.known,r.where.name]),[['105',1,2,4,"St. Joseph's Instn"]]);
 // 2. incidents: closure first, with affected service/direction/stretch; unmatched incident omitted; build-up without incident kept
 assert.strictEqual(b.traffic.rows.length,2);
 const [closure,buildup]=b.traffic.rows;
 assert.ok(closure.blocked&&closure.kind==='road_closed');
 assert.deepStrictEqual(closure.impacts.map(i=>[i.svc,i.dir,i.where.name]),[['105',2,'Bef Thomson Flyover → Opp Thomson Plaza']]);
 assert.deepStrictEqual([buildup.kind,buildup.road,buildup.delay,buildup.impacts[0].svc,buildup.impacts[0].dir],['jam','Tanjong Katong Rd',3.2,'36',2]);
 assert.strictEqual(b.traffic.services,2);
 // 3. bunching before gaps, with stop and figures
 assert.deepStrictEqual(b.headway.rows.map(r=>[r.type,r.svc,r.dir,r.where.name]),[['bunching','105',1,"St. Joseph's Instn"],['gap','14',1,'Bedok Int']]);
 assert.strictEqual(b.headway.rows[1].headway,23);
 // unavailable coverage yields no rows rather than stale/forecast-looking ones
 const off=m.boardModel({...data,coverage:{loading:false,traffic:true,bunching:false,gap:false}});
 assert.strictEqual(off.loading.rows.length,0);assert.strictEqual(off.headway.rows.length,0);assert.ok(!off.loading.available);
 // rendering escapes provider text
 const html=m.trafficCard({...closure,road:'<img src=x onerror=alert(1)>'});
 assert.ok(!html.includes('<img')&&html.includes('BLOCKED'));
 assert.ok(m.loadingCard(b.loading.rows[0]).includes('2<i>/4</i>'));
 assert.ok(m.headwayCard(b.headway.rows[1]).includes('23 min'));
 assert.ok(m.emptyState(false,'x','y').includes('not a clearance'));
 // pagination keeps whole cards and never yields an empty page list
 assert.deepStrictEqual(m.paginate([100,100,100],250,10),[[0,1],[2]]);
 assert.deepStrictEqual(m.paginate([400,50],300,10),[[0],[1]]);
 assert.deepStrictEqual(m.paginate([],300,10),[[]]);
 console.log('PASS: TV board answers loading / incident / bunching-gap with service, direction and stop; closure priority; unmatched incidents omitted; unavailable coverage not filled; escaping; whole-card pagination.');
})().catch(e=>{console.error(e);process.exit(1);});
