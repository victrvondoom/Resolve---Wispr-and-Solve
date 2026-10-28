"""Landing page (/) and Mission Control (/app) render in a real Chromium without errors or horizontal overflow.

These checks are DOM/visual only (Playwright); they do not exercise native Windows input.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import HAVE_PW, Server, free_port

CHROMIUM = '/opt/pw-browsers/chromium'
OVERFLOW_JS = 'document.scrollingElement.scrollWidth <= window.innerWidth'


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class LandingAndAppUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = Server(cls.tmp.name, free_port()).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROMIUM) if Path(CHROMIUM).exists() else cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.stop()
        cls.tmp.cleanup()

    def open(self, path, width, height=900):
        ctx = self.browser.new_context(viewport={'width': width, 'height': height})
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: errors.append(f'console.{m.type}: {m.text} @ {m.location.get("url")}') if m.type == 'error' else None)
        page.goto(f'http://127.0.0.1:{self.srv.port}{path}')
        page.wait_for_load_state('networkidle')
        self.addCleanup(ctx.close)
        return page, errors

    def test_landing_desktop_renders_scene_benchmark_and_cta(self):
        page, errors = self.open('/', 1440)
        page.wait_for_function("['webgl','fallback'].includes(document.getElementById('scene').dataset.renderer)")
        renderer = page.evaluate("document.getElementById('scene').dataset.renderer")
        if renderer == 'webgl':
            page.wait_for_function("Number(document.getElementById('scene').dataset.frames || 0) >= 1")
        else:  # failure mode: no WebGL -> the static gradient fallback must be applied
            self.assertTrue(page.evaluate("document.querySelector('.hero').classList.contains('no-webgl')"))
        # benchmark: either the API's numbers or an explicit "unavailable"
        page.wait_for_function("['loaded','unavailable'].includes(document.getElementById('bench-table').dataset.state)")
        state = page.evaluate("document.getElementById('bench-table').dataset.state")
        body = page.locator('#bench-body').inner_text()
        status, api = self.srv.req('GET', '/api/web/benchmark')
        if state == 'loaded':
            self.assertEqual(status, 200)
            for name, p in api['per_policy'].items():
                row = page.locator('#bench-body tr', has_text=name).inner_text()
                self.assertIn(f"{p['unsafe_or_false']} / {p['runs']}", row)
        else:
            self.assertNotEqual(status, 200)
            self.assertIn('unavailable', body.lower())
        # honesty strings are present
        text = page.locator('body').inner_text()
        for needle in ('WINDOWS_NATIVE_TESTS_NOT_RUN', 'scripts/smoke-windows.ps1', 'SIMULATED DELIVERY', 'no Wispr SDK integration'.lower()):
            self.assertIn(needle.lower(), text.lower())
        self.assertTrue(page.evaluate(OVERFLOW_JS))
        page.get_by_role('link', name='Open Mission Control').first.click()
        page.wait_for_url('**/app')
        page.wait_for_selector('#wmstart')
        self.assertEqual(errors, [])

    def test_landing_mobile_has_no_horizontal_overflow(self):
        page, errors = self.open('/', 390, 844)
        page.wait_for_function("['loaded','unavailable'].includes(document.getElementById('bench-table').dataset.state)")
        self.assertTrue(page.evaluate(OVERFLOW_JS), page.evaluate('[document.scrollingElement.scrollWidth, innerWidth]'))
        self.assertEqual(errors, [])

    def test_landing_reduced_motion_stops_animation(self):
        ctx = self.browser.new_context(viewport={'width': 1280, 'height': 800}, reduced_motion='reduce')
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.goto(f'http://127.0.0.1:{self.srv.port}/')
        page.wait_for_function("['webgl','fallback'].includes(document.getElementById('scene').dataset.renderer)")
        if page.evaluate("document.getElementById('scene').dataset.renderer") == 'webgl':
            page.wait_for_timeout(400)
            first = page.evaluate("Number(document.getElementById('scene').dataset.frames)")
            page.wait_for_timeout(600)
            self.assertEqual(page.evaluate("Number(document.getElementById('scene').dataset.frames)"), first)
            self.assertEqual(page.evaluate("document.getElementById('scene').dataset.motion"), 'reduced')

    def test_app_desktop_and_mobile_clean(self):
        for width, height in ((1280, 900), (390, 844)):
            with self.subTest(width=width):
                page, errors = self.open('/app', width, height)
                page.wait_for_function("document.getElementById('health').textContent.startsWith('healthy')")
                page.wait_for_function("!document.getElementById('statusStrip').textContent.includes('Loading')")
                self.assertTrue(page.evaluate(OVERFLOW_JS), page.evaluate('[document.scrollingElement.scrollWidth, innerWidth]'))
                # every view fits the viewport too
                for view in ('agents', 'skills', 'files', 'bench', 'sim', 'windows', 'ledger', 'mission'):
                    page.locator(f'.sidebar a[data-view="{view}"]').click()
                    self.assertTrue(page.locator(f'#view-{view}').is_visible())
                    self.assertTrue(page.evaluate(OVERFLOW_JS), (view, page.evaluate('[document.scrollingElement.scrollWidth, innerWidth]')))
                # no dead buttons: every disabled button explains itself
                unexplained = page.evaluate("[...document.querySelectorAll('button:disabled')].filter(b => !b.title).map(b => b.id || b.textContent)")
                self.assertEqual(unexplained, [])
                self.assertEqual(errors, [])

    def test_app_keeps_existing_element_ids(self):
        page, errors = self.open('/app', 1280)
        ids = ['health', 'intent', 'revise', 'mission', 'contacts', 'reports', 'draft', 'approve', 'windows', 'hwnd', 'move', 'click', 'type',
               'ledger', 'discover', 'estop', 'resume', 'agentlog', 'benchlog', 'voiceStart', 'voiceStop', 'wmcmd', 'wmstart', 'wmplan',
               'wmapprove', 'wmtask', 'wmsend', 'wmfault', 'wmcands', 'wmmsg', 'wmstate', 'wmstream', 'reload', 'revmsg', 'voicebadge',
               'draftbox', 'actionmsg', 'evidence', 'px', 'py', 'typevalue', 'winlog', 'workspace', 'workspaceCount', 'workspacelog',
               'skillPropose', 'skillActivate', 'skillList', 'skillReplay', 'skilllog', 'wmbadge', 'webmission', 'narration', 'capture', 'uiaInspect']
        missing = page.evaluate('ids => ids.filter(i => !document.getElementById(i))', ids)
        self.assertEqual(missing, [])
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
