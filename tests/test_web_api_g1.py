"""G1: real server process, HTTP missions, SQLite persistence across restart, SSE of persisted events."""
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
try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tmp, port):
        self.tmp, self.port, self.proc, self.token = tmp, port, None, None

    def start(self):
        env = dict(os.environ, RESOLVE_DB=str(Path(self.tmp) / 'r.sqlite3'), RESOLVE_PORT=str(self.port), RESOLVE_FIXTURE_DIR=str(Path(self.tmp) / 'fx'))
        self.proc = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                self.token = self.get('/api/session')['token']
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError('server did not start')

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=data, method=method,
                                   headers={'Content-Type': 'application/json', 'X-Resolve-Token': self.token or ''})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        return self.req('GET', path)[1]


CMD = 'Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve.'


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class WebApiTests(unittest.TestCase):
    def test_http_mission_persists_across_restart_and_streams_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            try:
                st, world = srv.req('POST', '/api/web/world', {})
                self.assertEqual(st, 200)
                st, m = srv.req('POST', '/api/web/missions', {'command': CMD})
                self.assertEqual((st, m['status']), (201, 'NEEDS_CLARIFICATION'))
                mid = m['mission_id']
                self.assertEqual(srv.req('POST', f'/api/web/missions/{mid}/plan', {})[0], 409)  # refuses to guess
                self.assertEqual(srv.req('POST', f'/api/web/missions/{mid}/bind', {'contact_id': 'C-104', 'revision': 1})[0], 200)
                st, plan = srv.req('POST', f'/api/web/missions/{mid}/plan', {})
                self.assertEqual(st, 200)
                st, r = srv.req('POST', f"/api/web/actions/{plan['task_action']}/run", {})
                self.assertEqual((st, r['status']), (200, 'CONFIRMED'))
                st, r = srv.req('POST', f"/api/web/actions/{plan['send_action']}/approve", {'digest': 'wrong'})
                self.assertEqual((st, r['code']), (409, 'DIGEST_MISMATCH'))
                self.assertEqual(srv.req('POST', f"/api/web/actions/{plan['send_action']}/approve", {'digest': plan['approval_digest']})[0], 200)
                st, r = srv.req('POST', '/api/web/faults', {'app': 'outbox', 'name': 'drop_ack'})
                self.assertEqual(st, 200)
                st, r = srv.req('POST', f"/api/web/actions/{plan['send_action']}/run", {})
                self.assertEqual((st, r['status']), (200, 'CONFIRMED'))
                events = srv.get(f'/api/web/missions/{mid}/events')['events']
                self.assertIn('UNCERTAIN', [e['state'] for e in events])
                # SSE reads the same persisted events
                r = urllib.request.urlopen(f'http://127.0.0.1:{srv.port}/api/stream?after=0&max_s=1.5', timeout=10)
                text = r.read().decode()
                self.assertIn('event: action.uncertain', text)
                self.assertIn('event: action.confirmed', text)
                srv.stop()
                srv = Server(tmp, srv.port).start()  # restart: state comes from SQLite
                again = srv.get(f'/api/web/missions/{mid}')
                self.assertEqual(again['binding']['contact_id'], 'C-104')
                self.assertEqual([a['status'] for a in again['actions_detail']], ['CONFIRMED', 'CONFIRMED'])
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
