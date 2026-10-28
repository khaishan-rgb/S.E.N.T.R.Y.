"""Real-browser TV containment regression, with explicitly synthetic API fixtures.
Run: python test_forecast2026_tv.py (Playwright + Chromium required).
No provider credentials, external traffic photos or live observations are used.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import forecast2026


CHECK = r'''()=>{
 const issues=[],rect=x=>x.getBoundingClientRect(),visible=x=>getComputedStyle(x).display!=='none'&&rect(x).width>0&&rect(x).height>0;
 if(document.documentElement.scrollHeight>innerHeight+1)issues.push('page vertical overflow');
 if(document.documentElement.scrollWidth>innerWidth+1)issues.push('page horizontal overflow');
 const viewport={left:0,top:0,right:innerWidth,bottom:innerHeight};
 const inside=(a,b)=>a.left>=b.left-1&&a.top>=b.top-1&&a.right<=b.right+1&&a.bottom<=b.bottom+1;
 for(const element of document.querySelectorAll('.console,.risk-card,.main-grid .panel,.bottom-grid>.panel')){
  if(visible(element)&&!inside(rect(element),viewport))issues.push('outside viewport '+element.className);
 }
 for(const parent of document.querySelectorAll('.console,.main-grid,.bottom-grid,.left-rail,.right-rail')){
  const children=[...parent.children].filter(visible);
  for(let i=0;i<children.length;i++)for(let j=i+1;j<children.length;j++){
   const a=rect(children[i]),b=rect(children[j]);
   if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)issues.push('sibling overlap in '+parent.className);
  }
 }
 if(document.body.classList.contains('tv')){
  const panels=[...document.querySelectorAll('.bottom-grid>.tvq')];
  if(panels.length!==3||!panels.every(visible))issues.push('expected three TV bottom panels');
  for(const old of document.querySelectorAll('.bottom-grid>.panel:not(.tvq)'))if(visible(old))issues.push('old bottom panel visible '+old.className);
  for(const list of document.querySelectorAll('.tvq-list'))for(const card of [...list.children].filter(visible))if(rect(card).bottom>rect(list).bottom+1)issues.push('card cut off in '+list.id);
 }
 for(const id of ['networkStatus','riskOutlook','headwayChart','serviceWatch','recommendations','cameras','loadingChart','bunchingPanel','tvLoadList','tvTrafficList','tvHeadwayList']){
  const element=document.getElementById(id);if(!visible(element))continue;
  if(element.scrollHeight>element.clientHeight+2)issues.push('content cropped in '+id+' '+element.scrollHeight+'/'+element.clientHeight);
  if(element.scrollWidth>element.clientWidth+2)issues.push('content wider than '+id);
 }
 const cameras=[...document.querySelectorAll('.camera')];
 if(cameras.length!==4)issues.push('expected four camera slots');
 for(const camera of cameras)if(!inside(rect(camera),rect(document.getElementById('cameras'))))issues.push('camera overlaps its panel');
 for(const element of document.querySelectorAll('.topbar .brand h1,.topbar .clock strong'))if(visible(element)&&!inside(rect(element),rect(document.querySelector('.topbar'))))issues.push('header text clipped');
 return issues;
}'''


def fixture(horizon):
    now=time.time()
    raw={'age_s':0,'services':[{'svc':'985','dir':1,'blocked':True,'extra':{15:{'mid':0},30:{'mid':0},60:{'mid':0}}}],
         'events':[{'id':'TEST-closure','kind':'road_closed','status':'confirmed','road':'Near Bukit Timah Expressway / Thomson Road (test fixture)','pt':[103.79,1.36],'delay_s':None,'services':[{'svc':'985','dir':1,'a_m':1000,'b_m':1100}]}],
         'sources':{'tomtom_incidents':{'status':'live','detail':'TEST FIXTURE, not a provider observation'}},
         'route_context':{'985:1':[{'code':'51101','seq':31,'name':'Bef Thomson Flyover','road':'Thomson Road','lat':1.36,'lon':103.79,'route_m':1050}]}}
    samples=[{'service':'985','direction':1,'at':now,'issue':'No data','observed_buses':[{'id':'TEST-BUS','lat':1.36,'lon':103.79,'load':'LSD'}]}]
    data=forecast2026.build_centre('live',horizon,raw=raw,samples=samples,delay_min=3)
    data['routes']=[{'svc':'985','dir':1,'line':[[103.73,1.33],[103.79,1.36],[103.86,1.39]]}]
    data['context_routes']=list(data['routes'])
    data.update(history=[],namespace=f'live:network:{horizon}',audit=[])
    # Several advisory entries exercise automatic in-panel pagination.
    data['recommendations']*=3
    return data


def main():
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        env=dict(os.environ,SENTRY2026_DB=tmp+'/journal.db')
        proc=None if os.getenv('SENTRY_TV_TEST_EXTERNAL') else subprocess.Popen([sys.executable,'-m','uvicorn','forecast2026_server:app','--host','127.0.0.1','--port','8098'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(70):
                try:urllib.request.urlopen('http://127.0.0.1:8098/',timeout=1);break
                except OSError:time.sleep(.1)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page();page.clock.install();writes=[];page.on('request',lambda request:writes.append(request.url) if request.method not in ('GET','HEAD') else None);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def route(request):
                    url=request.request.url
                    if not url.startswith('http://127.0.0.1:8098'):request.abort();return
                    path=urlsplit(url).path
                    if path=='/api/forecast/centre':
                        horizon=int(parse_qs(urlsplit(url).query).get('horizon',['15'])[0])
                        request.fulfill(content_type='application/json',body=json.dumps(fixture(horizon)))
                    elif path=='/api/forecast/centre/cameras':
                        request.fulfill(content_type='application/json',body=json.dumps({'cameras':[{'id':str(i),'label':f'TEST CAMERA {i}: long location description for layout validation','image':f'/test-camera.svg?id={i}','taken':'2026-10-27T08:00:00Z','stale':True,'lat':1.36,'lon':103.79} for i in range(4)],'fetched_epoch':time.time(),'context':'Nearest available TEST cameras'}))
                    elif path=='/test-camera.svg':
                        request.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="180"><rect width="400" height="180" fill="#153e52"/><text x="20" y="90" fill="white" font-size="22">TEST IMAGE · layout fixture</text></svg>')
                    else:request.continue_()
                page.route('**/*',route)
                page.goto('http://127.0.0.1:8098/?present=1&mode=live&h=15');page.wait_for_selector('.camera');page.wait_for_timeout(300)
                # Bottom row answers: closure -> service / direction / stop; observed load is not shown as a forecast.
                card=page.locator('#tvTrafficList .tvb-event').first.inner_text()
                assert 'BLOCKED' in card and '985' in card and 'DIR 1' in card and 'Bef Thomson Flyover' in card,card
                assert 'unavailable' in page.locator('#tvLoadList').inner_text().lower()
                for width,height in [(1280,720),(1366,768),(1920,1080),(2560,1440),(3840,2160)]:
                    page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(350)
                    for _ in range(6):
                        page.evaluate('SENTRY2026.tour.next()')
                        if page.evaluate('!!SENTRY2026.tour.state.current'):break
                    assert page.locator('#routeSelection').is_visible()
                    assert page.evaluate('SENTRY2026.state.headwayScope===SENTRY2026.tour.state.current.svc')
                    assert page.locator('.selected-route-svg').count()==1
                    issues=page.evaluate(CHECK)
                    assert not issues,(width,height,issues)
                    page.screenshot(path=str(root/f'tv-fit-{width}.png'))
                    # Different horizons preserve the same fixed layout.
                    page.click('[data-h="30"]');page.wait_for_timeout(200);assert not page.evaluate(CHECK)
                before=page.evaluate('JSON.stringify({services:SENTRY2026.state.services,h:SENTRY2026.state.h,category:SENTRY2026.state.category,exclude:SENTRY2026.state.exclude,operator:SENTRY2026.state.operator})')
                for _ in range(14):page.evaluate('SENTRY2026.tour.next()')
                assert page.evaluate('JSON.stringify({services:SENTRY2026.state.services,h:SENTRY2026.state.h,category:SENTRY2026.state.category,exclude:SENTRY2026.state.exclude,operator:SENTRY2026.state.operator})')==before
                page.click('#spotlightToggle');assert not page.evaluate('SENTRY2026.tour.state.enabled')
                frozen=page.evaluate('SENTRY2026.tour.state.step');page.evaluate('SENTRY2026.tour.next()');assert page.evaluate('SENTRY2026.tour.state.step')==frozen
                page.click('#spotlightToggle');assert page.evaluate('SENTRY2026.tour.state.enabled')
                page.click('#date');assert page.evaluate('SENTRY2026.tour.state.pausedUntil>Date.now()+28000')
                assert page.evaluate('SENTRY2026.tour.state.current===null')
                paused_step=page.evaluate('SENTRY2026.tour.state.step');page.clock.fast_forward(31000);assert page.evaluate('SENTRY2026.tour.state.pausedUntil<=Date.now()');assert page.evaluate('SENTRY2026.tour.state.step')!=paused_step
                assert not writes,writes
                # Returning from fullscreen restores the ordinary scrollable controller layout.
                page.set_viewport_size({'width':1366,'height':768});page.wait_for_timeout(250)
                page.click('#exitTV');page.wait_for_timeout(200)
                assert not page.evaluate('document.body.classList.contains("tv")')
                # "Top services to watch" is retired; select the service from its alert ("Focus service on map").
                assert page.locator('#alerts').count()==0          # Operational alerts panel retired
                page.locator('.tvb-svc[data-service="985"]').first.click()
                page.wait_for_function('SENTRY2026.state.services.includes("985")')
                assert page.locator('#routeSelection').is_visible();assert page.locator('.selected-route-svg').count()==1
                page.click('#clearService');page.wait_for_function('SENTRY2026.state.services.length===0')
                page.click('#tv');page.wait_for_timeout(300);assert not page.evaluate(CHECK)
                for module in ('loading','bunching','headway'):
                    page.evaluate('(key)=>document.querySelector(`[data-analysis="${key}"]`).click()',module)
                    page.wait_for_timeout(150)
                    assert not page.evaluate(CHECK),(module,page.evaluate(CHECK))
                assert not errors,errors
                browser.close()
                print('PASS: bottom row shows loading / incident-traffic / bunching-gap panels; five TV viewports (720p, 1366x768, 1080p, 1440p, 4K), no page scrolling, no panel overlap or cropped panel bodies, populated camera containment, horizon changes, selected-route visibility, repeated read-only spotlight cycles, preserved filters, manual pause/resume and TV re-entry.')
        finally:
            if proc:proc.terminate();proc.wait(timeout=10)


if __name__=='__main__':main()
