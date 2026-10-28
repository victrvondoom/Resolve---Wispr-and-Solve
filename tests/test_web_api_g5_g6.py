"""API-level E2E for utterances (W7), supervisor DAG (G5), skills (W8) and SSE task events, against a real server process."""
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import CMD, HAVE_PW, Server, free_port


def count(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class ApiFlowTests(unittest.TestCase):
    def test_voice_supervisor_skill_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            fx = Path(tmp) / 'fx'
            try:
                st, status = srv.req('GET', '/api/web/status')
                self.assertEqual(st, 200)
                self.assertFalse(status['windows_native']['available'])
                self.assertIn('WINDOWS_NATIVE_TESTS_NOT_RUN', status['windows_native']['reason'])
                self.assertEqual(srv.req('POST', '/api/web/utterances', {'text': CMD, 'partial': True})[0], 409)
                st, out = srv.req('POST', '/api/web/utterances', {'text': CMD})
                self.assertEqual((st, out['classification']['kind']), (201, 'NEW_GOAL'))
                mid = out['mission']['mission_id']
                self.assertIn(mid, [m['mission_id'] for m in srv.get('/api/web/missions')['missions']])
                self.assertEqual(len(srv.get('/api/web/agents')['agents']), 13)
                st, inv = srv.req('POST', f'/api/web/missions/{mid}/supervise', {})
                self.assertFalse(inv['planned'])  # two Jordan Lees: planner refuses
                st, out = srv.req('POST', f'/api/web/missions/{mid}/utterances', {'text': 'the one from Operations'})
                self.assertEqual(out['mission']['binding']['contact_id'], 'C-104')
                st, inv = srv.req('POST', f'/api/web/missions/{mid}/supervise', {})
                self.assertTrue(inv['planned'], inv)
                st, out = srv.req('POST', f'/api/web/missions/{mid}/utterances', {'text': 'approve'})
                self.assertEqual(out['approval']['status'], 'AUTHORIZED')
                st, ex = srv.req('POST', f'/api/web/missions/{mid}/supervise', {'phase': 'execute'})
                self.assertTrue(ex['verification']['all_agree'])
                self.assertEqual(count(fx / 'outbox.sqlite3', 'SELECT COUNT(*) FROM messages'), 1)
                tasks = srv.get(f'/api/web/missions/{mid}/tasks')['tasks']
                self.assertIn('IndependentVerifier', {t['payload'] and __import__('json').loads(t['payload']).get('agent_id') for t in tasks})
                text = urllib.request.urlopen(f'http://127.0.0.1:{srv.port}/api/stream?after=0&max_s=1.2', timeout=10).read().decode()
                self.assertIn('event: task.succeeded', text)
                self.assertIn('event: action.confirmed', text)
                # skills through the API
                st, sk = srv.req('POST', '/api/web/skills/record', {'contact_id': 'C-310', 'title': 'Recorded demo'})
                self.assertEqual(st, 201, sk)
                st, r = srv.req('POST', f"/api/web/skills/{sk['skill_id']}/replay", {'params': {'contact_id': 'C-104', 'title': 'Replayed'}})
                self.assertEqual(r['status'], 'REFUSED')  # not approved yet
                self.assertEqual(srv.req('POST', f"/api/web/skills/{sk['skill_id']}/approve", {'version': 1})[0], 200)
                st, r = srv.req('POST', f"/api/web/skills/{sk['skill_id']}/replay", {'params': {'contact_id': 'C-104', 'title': 'Replayed'}, 'fault': 'layout_shift'})
                self.assertEqual(r['status'], 'CONFIRMED', r)
                self.assertEqual(count(fx / 'crm.sqlite3', "SELECT COUNT(*) FROM tasks WHERE title='Replayed' AND contact_id='C-104'"), 1)
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
