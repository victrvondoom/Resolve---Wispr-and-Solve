"""G1/G3 dashboard E2E: a real browser clicks the Mission Control UI; effects are checked in the fixture DBs.

Browser actions here are Playwright DOM-level clicks, not physical cursor movement. The outbox is SIMULATED DELIVERY.
"""
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


def watch(page, errors):
    page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    page.on('console', lambda m: errors.append(f'console.error: {m.text} @ {m.location.get("url")}') if m.type == 'error' else None)


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
                    watch(page, errors)
                    page.goto(f'http://127.0.0.1:{srv.port}/app')
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
                    page.wait_for_function("document.getElementById('wmstream').textContent.includes('UNCERTAIN')")
                    # the action cards render backend state: both actions CONFIRMED with independent verification
                    page.wait_for_function("document.querySelectorAll('#wmactions .action').length === 2")
                    for op in ('crm_create_task', 'outbox_send'):
                        card = page.locator('#wmactions .action', has_text=op).inner_text()
                        self.assertIn('CONFIRMED', card)
                        self.assertIn('verify: CONFIRMED', card)
                    mid = page.evaluate("localStorage.getItem('wm_mid')")
                    self.assertIn(mid, [m['mission_id'] for m in srv.get('/api/web/missions')['missions']])
                    before = page.locator('#wmstate').inner_text()
                    page.reload()
                    page.wait_for_function("document.getElementById('wmstate').textContent.includes('CONFIRMED')")
                    self.assertEqual(json.loads(page.locator('#wmstate').inner_text())['actions'], json.loads(before)['actions'])
                    # the session list shows the persisted mission and marks it selected
                    page.wait_for_selector(f'#missionList button[data-mid="{mid}"][aria-pressed="true"]')
                    b.close()
                self.assertEqual(rows(Path(tmp) / 'fx' / 'outbox.sqlite3', 'SELECT to_addr FROM messages'), [('jordan.lee@ops.example.test',)])
                self.assertEqual(rows(Path(tmp) / 'fx' / 'crm.sqlite3', 'SELECT contact_id FROM tasks'), [('C-104',)])
                self.assertEqual(errors, [])
            finally:
                srv.stop()

    def test_ui_panels_utterance_agents_skills_files(self):
        """New panels are wired to the real backend; effects are checked outside the page (SQLite / filesystem)."""
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            errors = []
            try:
                with sync_playwright() as p:
                    b = p.chromium.launch(executable_path=CHROMIUM) if Path(CHROMIUM).exists() else p.chromium.launch()
                    page = b.new_page(viewport={'width': 1280, 'height': 900})
                    watch(page, errors)
                    page.goto(f'http://127.0.0.1:{srv.port}/app')
                    self.assertTrue(page.locator('#utterSubmit').is_disabled())  # no mission yet -> cannot submit
                    page.get_by_role('button', name='Start mission').click()
                    page.wait_for_selector('#wmcands button')
                    mid = page.evaluate("localStorage.getItem('wm_mid')")
                    # utterance: typed text is classified by the server and recorded on the mission
                    page.fill('#utterText', 'Use the Jordan Lee in Operations.')
                    page.click('#utterSubmit')
                    page.wait_for_function("document.getElementById('utterResult').textContent.startsWith('Classified as')")
                    kind = page.locator('#utterResult .chip').first.inner_text()
                    self.assertIn(kind, {'NEW_GOAL', 'REFINEMENT', 'CORRECTION', 'CANCEL', 'APPROVE', 'QUESTION'})
                    self.assertIn('Use the Jordan Lee in Operations.', json.dumps(srv.get(f'/api/web/missions/{mid}').get('utterances', [])))
                    # agents: supervisor timeline uses real timestamps from the server
                    page.click('.sidebar a[data-view="agents"]')
                    page.wait_for_selector('#agentRoster li b')
                    page.click('#supervise')
                    page.wait_for_selector('#taskTimeline .tl-row', timeout=60000)
                    self.assertGreaterEqual(page.locator('#taskTimeline .tl-row').count(), 2)
                    self.assertGreaterEqual(page.locator('#taskTimeline .tl-bar').count(), 1)
                    self.assertNotIn('NaN', page.locator('#taskTimeline').inner_text())
                    # skills: record -> approve exact version -> replay under a layout shift
                    page.click('.sidebar a[data-view="skills"]')
                    page.click('#skillRecord')
                    page.wait_for_function("document.getElementById('webSkillLog').textContent.startsWith('Recorded')", timeout=90000)
                    page.wait_for_selector('#webSkills button[data-act="approve"]:not([disabled])')
                    self.assertTrue(page.locator('#webSkills button[data-act="replay"]').first.is_disabled())  # unapproved
                    page.locator('#webSkills button[data-act="approve"]').first.click()
                    page.wait_for_function("document.getElementById('webSkillLog').textContent.startsWith('Approved')")
                    page.wait_for_selector('#webSkills button[data-act="replay"]:not([disabled])')
                    tasks_before = len(rows(Path(tmp) / 'fx' / 'crm.sqlite3', 'SELECT id FROM tasks'))
                    page.check('#skillFault')
                    page.locator('#webSkills button[data-act="replay"]').first.click()
                    page.wait_for_function("document.getElementById('webSkillLog').textContent.startsWith('Replay')", timeout=90000)
                    self.assertIn('"status": "CONFIRMED"', page.locator('#webSkillLog').inner_text())
                    self.assertEqual(len(rows(Path(tmp) / 'fx' / 'crm.sqlite3', 'SELECT id FROM tasks')), tasks_before + 1)
                    # files sandbox: seed -> plan -> apply moves the duplicate into quarantine -> undo restores it
                    sandbox = Path(tmp) / 'fx' / 'sandbox'
                    dup = sandbox / 'reports' / 'q3-ops-final (copy).txt'
                    page.click('.sidebar a[data-view="files"]')
                    self.assertTrue(page.locator('#filesApply').is_disabled())
                    for bid, label in (('filesSeed', 'Seed demo files'), ('filesPlan', 'Plan')):
                        page.click('#' + bid)
                        page.wait_for_function(f"document.getElementById('filesMsg').textContent.startsWith('{label}: done')")
                    self.assertTrue(dup.exists())
                    page.click('#filesApply')
                    page.wait_for_function("document.getElementById('filesMsg').textContent.startsWith('Apply: done')")
                    self.assertFalse(dup.exists())
                    page.click('#filesUndo')
                    page.wait_for_function("document.getElementById('filesMsg').textContent.startsWith('Undo: done')")
                    self.assertTrue(dup.exists())
                    b.close()
                self.assertEqual(errors, [])
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
