"""G1/G3 dashboard E2E: a real browser clicks the Mission Control UI; effects are checked in the fixture DBs."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import HAVE_PW, Server, free_port

CHROMIUM = '/opt/pw-browsers/chromium'


def rows(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class DashboardE2E(unittest.TestCase):
    def test_ui_drives_full_mission_and_survives_reload(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            errors = []
            try:
                with sync_playwright() as p:
                    b = p.chromium.launch(executable_path=CHROMIUM) if Path(CHROMIUM).exists() else p.chromium.launch()
                    page = b.new_page()
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(f'http://127.0.0.1:{srv.port}/')
                    page.get_by_role('button', name='Start mission').click()
                    page.locator('#wmcands button', has_text='C-104').click()
                    page.get_by_role('button', name='Plan actions').click()
                    page.wait_for_function("!document.getElementById('wmtask').disabled")
                    page.get_by_role('button', name='Run CRM task').click()
                    page.wait_for_function("document.getElementById('wmstate').textContent.includes('\"verification\": \"CONFIRMED\"')")
                    page.get_by_role('button', name='Approve exact send').click()
                    page.wait_for_function("!document.getElementById('wmsend').disabled")
                    page.get_by_role('button', name='Arm lost-acknowledgement fault').click()
                    page.get_by_role('button', name='Run send').click()
                    page.wait_for_function("document.getElementById('wmmsg').textContent.startsWith('Result')", timeout=60000)
                    self.assertIn('CONFIRMED', page.locator('#wmmsg').inner_text())
                    page.wait_for_function("document.getElementById('wmstream').textContent.includes('UNCERTAIN'.toLowerCase()) || document.getElementById('wmstream').textContent.includes('UNCERTAIN')")
                    before = page.locator('#wmstate').inner_text()
                    page.reload()
                    page.wait_for_function("document.getElementById('wmstate').textContent.includes('CONFIRMED')")
                    self.assertEqual(json.loads(page.locator('#wmstate').inner_text())['actions'], json.loads(before)['actions'])
                    b.close()
                self.assertEqual(rows(Path(tmp) / 'fx' / 'outbox.sqlite3', 'SELECT to_addr FROM messages'), [('jordan.lee@ops.example.test',)])
                self.assertEqual(rows(Path(tmp) / 'fx' / 'crm.sqlite3', 'SELECT contact_id FROM tasks'), [('C-104',)])
                self.assertEqual(errors, [])
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
