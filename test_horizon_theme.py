"""HORIZON theme guard: one token source, brand assets present, every engine page on the shared theme and shell."""
import glob
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHELL_PAGES = ['index.html', 'occ_live.html', 'control.html', 'bunching.html', 'halfway_planner.html', 'hplanner.html', 'halfway.html',
               'recovery_guide.html', 'insight.html', 'cameras.html', 'traffic.html', 'login.html', 'forecast.html', 'command.html', 'settings.html',
               'diversion.html', 'diversion_classic.html', 'diversion_history.html']


class HorizonTheme(unittest.TestCase):
    def test_json_tokens_match_css(self):
        css = (ROOT / 'horizon-tokens.css').read_text()
        from_css = {k: v.strip() for k, v in re.findall(r'--hz-([a-z0-9-]+):([^;]+);', css)}
        from_json = json.loads((ROOT / 'horizon-tokens.json').read_text())['tokens']
        self.assertEqual(from_css, from_json, 'regenerate horizon-tokens.json from horizon-tokens.css')

    def test_design_system_takes_its_palette_from_the_tokens(self):
        ds = (ROOT / 'design_system.css').read_text()
        self.assertTrue(ds.lstrip().split('*/', 1)[1].lstrip().startswith('@import url("/horizon-tokens.css")'))
        for name in ('void', 'cyan', 'blue', 'text', 'font'):
            self.assertRegex(ds, rf'--ds-{name}:var\(--hz-')

    def test_brand_assets_exist_and_are_referenced(self):
        css = (ROOT / 'horizon-tokens.css').read_text()
        for url in re.findall(r'url\("(/brand/[^"]+)"\)', css):
            self.assertTrue((ROOT / url.lstrip('/')).is_file(), url)
        shell = (ROOT / 'app_shell.js').read_text()
        for asset in re.findall(r'/brand/[a-z0-9-]+\.(?:webp|svg)', shell):
            self.assertTrue((ROOT / asset.lstrip('/')).is_file(), asset)

    def test_every_engine_page_uses_the_shared_theme_and_shell(self):
        for page in SHELL_PAGES:
            html = (ROOT / page).read_text(encoding='utf-8')
            self.assertIn('/design-system.css', html, page)
            self.assertIn('/app-shell.js', html, page)
        tv = (ROOT / 'diversion_tv.html').read_text(encoding='utf-8')
        self.assertIn('/horizon-tokens.css', tv)

    def test_old_product_name_is_gone_from_pages(self):
        for page in glob.glob(str(ROOT / '*.html')):
            text = Path(page).read_text(encoding='utf-8')
            self.assertNotRegex(text, r'SG Transport Pulse|SG TRANSPORT PULSE|S\.E\.N\.T\.R\.Y', Path(page).name)

    def test_quick_launch_is_wired(self):
        shell = (ROOT / 'app_shell.js').read_text()
        self.assertIn('data-hz-qk', shell)
        self.assertRegex(shell, r'e\.ctrlKey \|\| e\.metaKey\) && !e\.shiftKey && !e\.altKey && \(e\.key === "k"')
        self.assertIn('window.HORIZON', shell)

    def test_brand_route_rejects_path_tricks(self):
        app = (ROOT / 'app.py').read_text()
        self.assertIn(r're.fullmatch(r"[a-z0-9-]+\.(webp|svg|png)", name)', app)
        self.assertIn('"/brand/"', app)


if __name__ == '__main__':
    unittest.main()
