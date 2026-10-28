"""W12: SIGKILL the backend while a send is DISPATCHING, restart, and reconcile from SQLite without resending."""
import json
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import CMD, HAVE_PW, Server, free_port


def rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


@unittest.skipUnless(HAVE_PW and os.name != 'nt', 'needs playwright and POSIX signals')
class CrashRestartTests(unittest.TestCase):
    def test_kill_during_dispatch_then_reconcile_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            fx, db = Path(tmp) / 'fx', Path(tmp) / 'r.sqlite3'
            srv.req('POST', '/api/web/world', {})
            m = srv.req('POST', '/api/web/missions', {'command': CMD})[1]
            mid = m['mission_id']
            srv.req('POST', f'/api/web/missions/{mid}/bind', {'contact_id': 'C-104', 'revision': 1})
            plan = srv.req('POST', f'/api/web/missions/{mid}/plan', {})[1]
            srv.req('POST', f"/api/web/actions/{plan['send_action']}/approve", {'digest': plan['approval_digest']})
            srv.req('POST', '/api/web/faults', {'app': 'outbox', 'name': 'hang_after_commit_ms', 'value': 60000})
            def fire():
                try:
                    srv.req('POST', f"/api/web/actions/{plan['send_action']}/run", {})
                except Exception:
                    pass  # expected: the server is killed mid-request
            threading.Thread(target=fire, daemon=True).start()
            for _ in range(200):  # wait until the effect is committed AND the ledger says DISPATCHING
                committed = (fx / 'outbox.sqlite3').exists() and rows(fx / 'outbox.sqlite3', 'SELECT COUNT(*) FROM messages')[0][0] == 1
                state = rows(db, f"SELECT state FROM actions WHERE id='{plan['send_action']}'")[0][0]
                if committed and state == 'DISPATCHING':
                    break
                time.sleep(0.1)
            self.assertEqual((committed, state), (True, 'DISPATCHING'))
            srv.proc.send_signal(signal.SIGKILL)  # no cleanup, no ack
            srv.proc.wait(10)
            srv = Server(tmp, srv.port).start()
            try:
                again = srv.get(f'/api/web/missions/{mid}')
                send = next(a for a in again['actions_detail'] if a['action_id'] == plan['send_action'])
                self.assertEqual(send['status'], 'UNCERTAIN')  # crash window classified honestly on reload
                st, r = srv.req('POST', f"/api/web/actions/{plan['send_action']}/reconcile", {})
                self.assertEqual((st, r['status']), (200, 'CONFIRMED'))
                st, r = srv.req('POST', f"/api/web/actions/{plan['send_action']}/run", {})
                self.assertEqual(st, 409)  # terminal: cannot be dispatched again
                self.assertEqual(rows(fx / 'outbox.sqlite3', 'SELECT COUNT(*) FROM messages')[0][0], 1)
                self.assertEqual(len(rows(fx / 'outbox.sqlite3', "SELECT * FROM ground_truth WHERE kind='message_committed'")), 1)
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
