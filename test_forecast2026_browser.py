"""Optional real viewport/interaction checks. Run separately; not part of unit suite.
python test_forecast2026_browser.py
Requires: pip install playwright && playwright install chromium
"""
from pathlib import Path
import os
import subprocess
import shutil
import sys
import tempfile
import time
import urllib.request


def main():
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        env=dict(os.environ,SENTRY2026_DB=tmp+'/test.db')
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','forecast2026_server:app','--host','127.0.0.1','--port','8097'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen('http://127.0.0.1:8097/',timeout=1);break
                except Exception:time.sleep(.15)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium'),headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                page=browser.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('**/*',lambda route:route.continue_() if route.request.url.startswith('http://127.0.0.1:8097') else route.abort())
                page.goto('http://127.0.0.1:8097/?present=1');page.wait_for_selector('.risk-card')
                for w,h in [(1366,768),(1920,1080),(3840,2160),(390,844)]:
                    page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(200)
                    issues=page.evaluate('''()=>{let out=[];if(innerWidth>=1200){let panels=[...document.querySelectorAll('.risk-strip,.main-grid,.bottom-grid')].map(x=>x.getBoundingClientRect());for(let i=1;i<panels.length;i++)if(panels[i].top<panels[i-1].bottom)out.push('section overlap');if(document.documentElement.scrollWidth>innerWidth+1)out.push('horizontal overflow');let map=document.getElementById('networkMap').getBoundingClientRect();if(map.height<250||map.width<250)out.push('map collapsed');for(let p of document.querySelectorAll('.risk-card'))if(p.scrollHeight>p.clientHeight+3)out.push('risk card clipping');}return out;}''')
                    assert not issues,(w,h,issues)
                    page.screenshot(path=str(root/f'forecast2026-{w}.png'),full_page=w<1200)
                page.set_viewport_size({'width':1920,'height':1080})
                page.click('#exitTV');page.wait_for_timeout(100)
                page.click('#serviceLabel');page.fill('#serviceSearch','14');page.check('[data-pick="14"]');page.wait_for_function('SENTRY2026.state.services.includes("14")');page.click('#serviceLabel')
                page.wait_for_selector('[data-remove-service="14"]');page.click('[data-remove-service="14"]');page.wait_for_function('SENTRY2026.state.services.length===0')
                page.select_option('#delayThreshold','5');page.wait_for_function('SENTRY2026.state.data.delay_filter.minimum===5')
                page.select_option('#delayThreshold','custom');page.fill('#customDelay','2.5');page.locator('#customDelay').dispatch_event('change');page.wait_for_function('SENTRY2026.state.data.delay_filter.minimum===2.5')
                page.click('#clearFilters');page.wait_for_function('SENTRY2026.state.data.delay_filter.minimum===0.8 && SENTRY2026.state.data.horizon===0')
                page.select_option('#headwayInterval','15');assert page.locator('[data-hour]').count()==96
                page.locator('[data-hour="0"]').focus();assert 'Scheduled' in page.locator('#headwayInspect').inner_text()
                page.click('#headwayValues');assert page.locator('.headway-table tbody tr').count()==96;page.click('#closeModal')
                page.click('[data-analysis="bunching"]');assert page.locator('#bunchingModule').is_visible();page.click('[data-analysis="headway"]')
                page.click('[data-h="120"]');page.wait_for_function('SENTRY2026.state.data.horizon===120')
                page.click('[data-category="bunching"]');page.wait_for_function('SENTRY2026.state.data.alerts.every(a=>a.category==="bunching")')
                page.locator('.alert').first.click();assert page.locator('#modal').is_visible()
                page.click('#closeModal');page.click('[data-mode=live]');page.wait_for_function('SENTRY2026.state.data.mode==="live"');assert not page.locator('#mapWatermark').is_visible();page.click('#clearBuses');assert not page.evaluate('SENTRY2026.map.flags.buses')
                assert not errors,errors
                browser.close()
                print('PASS: four viewports, chips/reset/search, delay thresholds, headway intervals/table/tabs, horizon/category consistency, alert modal and bus layers. Screenshots saved.')
        finally:
            proc.terminate();proc.wait(timeout=10)


if __name__=='__main__':main()
