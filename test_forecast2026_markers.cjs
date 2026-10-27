// Map icons: stable positions (no re-spreading on pan / zoom); only icons on the same spot fan out.
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const url=code=>'data:text/javascript;base64,'+Buffer.from(code).toString('base64'),coreURL=url(fs.readFileSync(path.join(__dirname,'forecast2026-core.js'),'utf8'));
 global.document={getElementById:()=>({hidden:false,style:{setProperty(){}},classList:{toggle(){}}}),createElement:()=>({})};global.window={};
 const src=fs.readFileSync(path.join(__dirname,'forecast2026-map.js'),'utf8');
 assert.ok(!src.includes("'moveend'"),'icons must not be re-laid-out on every map move');
 const {ForecastMap}=await import(url(src.replace(/from '\.\/forecast2026-core\.js\?[^']+'/,'from '+JSON.stringify(coreURL))));
 const calls=[],item=(point)=>({point,marker:{setOffset(o){calls.push(o);this.offset=o;}}});
 const fake={kind:'ml',map:{},markers:new Map([['alert:a',item([103.8,1.3])],['alert:b',item([103.80001,1.30001])],['alert:c',item([103.8,1.3])],['event:far',item([103.9,1.35])]])};
 ForecastMap.prototype.layoutMarkers.call(fake);
 const off=k=>fake.markers.get(k).marker.offset;
 assert.deepEqual(off('event:far'),[0,0]);                                  // alone: sits exactly on its point
 const trio=['alert:a','alert:b','alert:c'].map(off);
 assert.equal(new Set(trio.map(String)).size,3);                              // same spot: fanned out, none hidden
 const n=calls.length;ForecastMap.prototype.layoutMarkers.call(fake);ForecastMap.prototype.layoutMarkers.call(fake);
 assert.equal(calls.length,n);                                                // repeated updates do not move icons
 console.log('PASS: map icons are stable across updates and pans; co-located icons fan out in a fixed pattern; single icons sit on their point.');
})().catch(e=>{console.error(e);process.exit(1);});
