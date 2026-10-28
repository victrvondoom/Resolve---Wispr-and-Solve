"""Live HTTP integration checks against an isolated RESOLVE process and database."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        env = os.environ.copy()
        env['RESOLVE_DB'] = str(Path(self.tmp.name) / 'state.sqlite3')
        env['RESOLVE_PORT'] = str(self.port)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(50):
            try:
                code, _ = self.get('/api/health')
                if code == 200:
                    break
            except Exception:
                time.sleep(.1)
        else:
            self.fail('local HTTP server did not start')
        _, session = self.get('/api/session')
        self.token = session['token']

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, json.loads(response.read())

    def post(self, path, data, *, token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Resolve-Token'] = self.token
        if origin:
            headers['Origin'] = origin
        request = urllib.request.Request(self.base + path, data=json.dumps(data).encode(), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_guarded_simulator_workflow_and_local_session(self):
        self.assertEqual(self.post('/api/agents/discover', {}, token=False)[0], 403)
        self.assertEqual(self.post('/api/agents/discover', {}, origin='https://example.invalid')[0], 403)
        self.assertEqual(self.get('/api/workspaces')[1]['total_concepts'], 80)
        self.assertIn('available', self.get('/api/uia/availability')[1])
        self.assertEqual(self.get('/api/benchmark')[1]['sample_size'], 24)
        status, discovery = self.post('/api/agents/discover', {})
        self.assertEqual(status, 200)
        self.assertEqual(len(discovery['tasks']), 2)
        self.assertEqual({task['status'] for task in discovery['tasks']}, {'SUCCEEDED'})
        self.assertEqual(self.post('/api/action/draft', {'contact_id': 'contact_alex_2', 'report_id': 'report_latest'})[0], 409)
        self.assertEqual(self.post('/api/action/draft', {'contact_id': 'contact_alex_1', 'report_id': 'report_old'})[0], 409)
        _, first_draft = self.post('/api/action/draft', {'contact_id': 'contact_alex_1', 'report_id': 'report_latest'})
        status, paused = self.post('/api/mission/revise', {'expected_revision': 1, 'original_request': 'Do not send the report.'})
        self.assertEqual((status, paused['status']), (200, 'PAUSED'))
        self.assertEqual(self.post('/api/action/approve-send', {'action_id': first_draft['action']['action_id'], 'digest': first_draft['digest']})[0], 409)
        _, active = self.post('/api/mission/revise', {'expected_revision': 2, 'original_request': 'Send the latest report after approval.'})
        self.assertEqual(active['status'], 'ACTIVE')
        _, draft = self.post('/api/action/draft', {'contact_id': 'contact_alex_1', 'report_id': 'report_latest'})
        status, result = self.post('/api/action/approve-send', {'action_id': draft['action']['action_id'], 'digest': draft['digest']})
        self.assertEqual((status, result['verification']['status']), (200, 'CONFIRMED'))
        self.assertEqual(result['verification']['evidence']['action_id'], draft['action']['action_id'])
        self.assertEqual(result['verification']['evidence']['followup_last_message_id'], result['verification']['evidence']['message_id'])
        self.assertEqual(self.post('/api/action/approve-send', {'action_id': draft['action']['action_id'], 'digest': draft['digest']})[0], 409)
        status, proposed = self.post('/api/skills/propose', {'action_id': draft['action']['action_id']})
        self.assertEqual((status, proposed['status']), (201, 'PENDING_APPROVAL'))
        self.assertEqual(self.post('/api/skills/activate', {'skill_id': proposed['skill']['skill_id'], 'version': 1, 'review_digest': 'wrong'})[0], 409)
        status, activated = self.post('/api/skills/activate', {'skill_id': proposed['skill']['skill_id'], 'version': 1, 'review_digest': proposed['review_digest']})
        self.assertEqual((status, activated['status']), (200, 'ACTIVE'))
        self.assertEqual(self.get('/api/skills')[1]['skills'][0]['status'], 'ACTIVE')
        status, replay = self.post('/api/skills/replay', {'skill_id': proposed['skill']['skill_id'], 'mission_id': 'mission_demo', 'expected_revision': 3, 'inputs': {'context': 'Northwind account', 'contact_id': 'contact_alex_1'}})
        self.assertEqual((status, replay['plan']['status'], replay['draft']['action']['status']), (201, 'NEEDS_APPROVAL', 'NEEDS_APPROVAL'))
        self.assertEqual(self.post('/api/action/approve-send', {'action_id': replay['draft']['action']['action_id'], 'digest': replay['draft']['digest']})[1]['verification']['status'], 'CONFIRMED')
        _, history = self.get('/api/mission/history')
        self.assertEqual([item['revision'] for item in history['revisions']], [1, 2, 3])
        self.assertEqual(self.post('/api/windows/click', {'mission_id': 'mission_demo', 'intent_revision': 3, 'hwnd': 1, 'x': 1, 'y': 1})[0], 400)
        self.assertEqual(self.post('/api/emergency-stop', {})[1]['status'], 'STOPPED')
        self.assertEqual(self.post('/api/resume', {})[1]['status'], 'READY')


if __name__ == '__main__':
    unittest.main()
