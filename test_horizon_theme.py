"""HORIZON theme guard: one token source, brand assets present, every engine page on the shared theme and shell."""
import glob
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHELL_PAGES = ['index.html', 'occ_live.html', 'control.html', 'bunching.html', 'halfway_planner.html', 'hplanner.html', 'halfway.html',
               'recovery_guide.html', 'insight.html', 'cameras.html', 'traffic.html', 'login.html', 'forecast.html', 'command.html', 'settings.html',
               'diversion.html', 'diversion_classic.html', 'diversion_history.html', 'stop_rt.html']



def brand_file(name):
    """Logos may sit in brand/ or at the top level (GitHub web upload cannot create folders)."""
    return ROOT / 'brand' / name if (ROOT / 'brand' / name).is_file() else ROOT / name

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
            self.assertTrue(brand_file(url.rsplit('/', 1)[-1]).is_file() if url.startswith('/brand/') else (ROOT / url.lstrip('/')).is_file(), url)
        shell = (ROOT / 'app_shell.js').read_text()
        for asset in re.findall(r'/brand/[a-z0-9-]+\.(?:webp|svg)', shell):
            self.assertTrue(brand_file(asset.rsplit('/', 1)[-1]).is_file() if asset.startswith('/brand/') else (ROOT / asset.lstrip('/')).is_file(), asset)

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

    def test_logos_never_show_as_broken_images(self):
        shell = (ROOT / 'app_shell.js').read_text()
        self.assertIn('function logoFallback', shell)
        self.assertIn('document.addEventListener("error"', shell)       # capture phase: catches images that fail before the shell mounts
        self.assertIn('.hz-tl-hz', (ROOT / 'design_system.css').read_text())
        app = (ROOT / 'app.py').read_text()
        self.assertIn('(HERE / name, "top-level file")', app)
        self.assertEqual((ROOT / 'diversion_tv.html').read_text().count('onerror='), 2)

    def test_logos_are_built_into_app_py_and_served_without_any_files(self):
        import ast, base64, tempfile
        src = (ROOT / 'app.py').read_text()
        tree = ast.parse(src)
        emb = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == '_BRAND_EMBEDDED')
        embedded = ast.literal_eval(emb.value)
        for name, b64 in embedded.items():                                         # built-in copies equal the brand files
            self.assertEqual(base64.b64decode(b64), brand_file(name).read_bytes(), name)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_brand_bytes')
        for layout in ('none', 'top', 'folder'):
            with tempfile.TemporaryDirectory() as tmp:
                here = Path(tmp)
                if layout == 'top':
                    (here / 'sbs-transit.webp').write_bytes(b'TOP')
                if layout == 'folder':
                    (here / 'brand').mkdir(); (here / 'brand' / 'sbs-transit.webp').write_bytes(b'FOLDER')
                scope = {'HERE': here, '_BRAND_EMBEDDED': embedded, 'base64': base64}
                exec(compile(ast.Module([fn], []), 'app.py', 'exec'), scope)
                data, where = scope['_brand_bytes']('sbs-transit.webp')
                expected = {'none': ('built into app.py', base64.b64decode(embedded['sbs-transit.webp'])), 'top': ('top-level file', b'TOP'), 'folder': ('brand folder', b'FOLDER')}[layout]
                self.assertEqual((where, data), expected, layout)
                self.assertEqual(scope['_brand_bytes']('nope.webp'), (None, 'missing'))

    def test_brand_route_rejects_path_tricks(self):
        app = (ROOT / 'app.py').read_text()
        self.assertIn(r're.fullmatch(r"[a-z0-9-]+\.(webp|svg|png)", name)', app)
        self.assertIn('"/brand/"', app)



class HideablePages(unittest.TestCase):
    def test_every_page_in_the_menu_can_be_hidden_in_settings(self):
        import re
        ids = set(re.findall(r'\{id:"([a-z0-9]+)",\s*name:', (ROOT / 'app_shell.js').read_text(encoding='utf-8'))) - {'settings'}
        m = re.search(r'^SITE_PAGE_IDS = \(([^)]*)\)', (ROOT / 'app.py').read_text(encoding='utf-8'), re.M)
        allowed = set(re.findall(r'"([a-z0-9]+)"', m.group(1)))
        self.assertTrue(ids)
        self.assertEqual(ids - allowed, set(), 'pages in the menu that Settings cannot hide')

if __name__ == '__main__':
    unittest.main()
