"""Real-browser TV operations-board regression, with explicitly synthetic API fixtures.
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
 if(visible(document.querySelector('.console')))issues.push('controller console still visible in TV mode');
 const columns=[...document.querySelectorAll('#tvBoard>.tvq')];
 if(columns.length!==3||!columns.every(visible))issues.push('expected three visible TV question columns');
 for(const c of columns)if(!inside(rect(c),viewport))issues.push('column outside viewport '+c.className);
 for(let i=0;i<columns.length;i++)for(let j=i+1;j<columns.length;j++){const a=rect(columns[i]),b=rect(columns[j]);if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)issues.push('column overlap');}
 for(const list of document.querySelectorAll('.tvq-list')){
  const box=rect(list);
  for(const card of [...list.children].filter(visible))if(rect(card).bottom>box.bottom+1||rect(card).right>box.right+1)issues.push('card cut off in '+list.id);
  if(list.scrollWidth>list.clientWidth+2)issues.push('content wider than '+list.id);
 }
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
    external=os.getenv('SENTRY_TV_TEST_EXTERNAL')  # set when a static server is already on :8098
    with tempfile.TemporaryDirectory() as tmp:
        env=dict(os.environ,SENTRY2026_DB=tmp+'/journal.db')
        proc=None if external else subprocess.Popen([sys.executable,'-m','uvicorn','forecast2026_server:app','--host','127.0.0.1','--port','8098'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(70):
                try:urllib.request.urlopen('http://127.0.0.1:8098/',timeout=1);break
                except OSError:time.sleep(.1)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page();writes=[];page.on('request',lambda request:writes.append(request.url) if request.method not in ('GET','HEAD') else None);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def route(request):
                    url=request.request.url
                    if not url.startswith('http://127.0.0.1:8098'):request.abort();return
                    path=urlsplit(url).path
                    if path=='/api/forecast/centre':
                        horizon=int(parse_qs(urlsplit(url).query).get('horizon',['0'])[0])
                        request.fulfill(content_type='application/json',body=json.dumps(fixture(horizon)))
                    elif path.startswith('/api/'):request.fulfill(content_type='application/json',body='{"cameras":[],"services":[]}')
                    else:request.continue_()
                page.route('**/*',route)
                page.goto('http://127.0.0.1:8098/?present=1&mode=live&h=0');page.wait_for_selector('#tvTrafficList .tvb-event');page.wait_for_timeout(300)
                # Question 2: road closure names the service, direction and affected stop.
                card=page.locator('#tvTrafficList .tvb-event').first.inner_text()
                assert 'ROAD CLOSED' in card.upper() and 'BLOCKED' in card and '985' in card and 'DIR 1' in card and 'Bef Thomson Flyover' in card,card
                # Question 1: the LSD bus is reported against its service and direction.
                load=page.locator('#tvLoadList').inner_text();assert '985' in load and 'DIR 1' in load,load
                # Question 3: no fabricated bunching for a sample without data.
                assert page.locator('#tvHeadwayList .tvb-card').count()==0
                for width,height in [(1280,720),(1366,768),(1920,1080),(2560,1440),(3840,2160)]:
                    page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(350)
                    issues=page.evaluate(CHECK)
                    assert not issues,(width,height,issues)
                    page.screenshot(path=str(root/f'tv-fit-{width}.png'))
                    page.click('[data-h="15"]');page.wait_for_timeout(250);assert not page.evaluate(CHECK)
                    # Observed load is never presented as a forecast.
                    assert 'unavailable' in page.locator('#tvLoadList').inner_text().lower()
                    page.click('[data-h="0"]');page.wait_for_timeout(250)
                assert not writes,writes
                page.set_viewport_size({'width':1366,'height':768});page.wait_for_timeout(250)
                page.click('#exitTV');page.wait_for_timeout(200)
                assert not page.evaluate('document.body.classList.contains("tv")')
                assert page.locator('.console').is_visible() and not page.locator('#tvBoard').is_visible()
                page.click('#tv');page.wait_for_timeout(300);assert not page.evaluate(CHECK)
                assert not errors,errors
                browser.close()
                print('PASS: TV board at five viewports (720p, 1366x768, 1080p, 1440p, 4K): three question columns, no page scrolling, overlap or cut-off cards; closure lists service/direction/stop; observed load not forecast; read-only; exit and re-entry.')
        finally:
            if proc:proc.terminate();proc.wait(timeout=10)


if __name__=='__main__':main()
