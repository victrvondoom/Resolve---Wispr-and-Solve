"""G3/G4 end-to-end: Playwright drives three separate stateful fixture apps; an independent oracle checks ground_truth."""
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'apps'))
try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

from resolve_core import Ledger
from web_fixtures import FixtureWorld
from web_mission import MissionError, WebMissionRunner

CMD = 'Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve.'


def oracle(root, app, kind):
    """Ground truth read straight from the app's hidden table; shares no code with WebVerifier."""
    db = sqlite3.connect(str(Path(root) / f'{app}.sqlite3'))
    try:
        return [json.loads(r[0]) for r in db.execute('SELECT detail FROM ground_truth WHERE kind=?', (kind,))]
    finally:
        db.close()


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    return urllib.request.urlopen(req)


@unittest.skipUnless(HAVE_PW, 'playwright not installed (portable browser tests need .venv)')
class WebMissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.world = FixtureWorld(self.root / 'fx')
        urls = {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url}
        self.ledger = Ledger(self.root / 'ledger.sqlite3')
        self.runner = WebMissionRunner(self.ledger, urls, self.root / 'fx')

    def tearDown(self):
        self.world.stop()
        self.tmp.cleanup()

    def bound_plan(self, pick='C-104'):
        m = self.runner.start(CMD)
        self.assertEqual(m['status'], 'NEEDS_CLARIFICATION')
        self.runner.bind(m['mission_id'], pick, 1)
        return m['mission_id'], self.runner.plan(m['mission_id'])

    def test_W3_ambiguous_name_blocks_planning(self):
        m = self.runner.start(CMD)
        self.assertEqual({c['id'] for c in m['candidates']}, {'C-104', 'C-208'})
        with self.assertRaises(MissionError) as ctx:
            self.runner.plan(m['mission_id'])
        self.assertEqual(ctx.exception.code, 'NEEDS_CLARIFICATION')
        self.assertEqual(oracle(self.root / 'fx', 'crm', 'task_created'), [])
        with self.assertRaises(MissionError):
            self.runner.bind(m['mission_id'], 'C-310', 1)  # not a candidate

    def test_W2_W10_full_mission_confirms_exactly_one_correct_effect(self):
        mid, plan = self.bound_plan()
        t = self.runner.run_task(plan['task_action'])
        self.assertEqual(t['status'], 'CONFIRMED')
        self.assertEqual([x['contact'] for x in oracle(self.root / 'fx', 'crm', 'task_created')], ['C-104'])
        with self.assertRaises(MissionError) as ctx:  # send needs approval
            self.runner.run_send(plan['send_action'])
        self.assertEqual(ctx.exception.code, 'NOT_AUTHORIZED')
        with self.assertRaises(MissionError) as ctx:  # wrong digest refused
            self.runner.approve(plan['send_action'], 'deadbeef')
        self.assertEqual(ctx.exception.code, 'DIGEST_MISMATCH')
        self.runner.approve(plan['send_action'], plan['approval_digest'])
        s = self.runner.run_send(plan['send_action'])
        self.assertEqual(s['status'], 'CONFIRMED')
        sent = oracle(self.root / 'fx', 'outbox', 'message_committed')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['to'], 'jordan.lee@ops.example.test')
        # the latest Ops report (v4, R-22), not v3 and not Sales
        self.assertEqual(plan['mission']['report']['id'], 'R-22')
        self.assertEqual(sent[0]['attachment_sha256'], plan['mission']['report']['sha256'])

    def test_W2_popup_and_delayed_render_are_handled(self):
        post(self.world.crm.url + '/_fault/popup', {})
        post(self.world.crm.url + '/_fault/delay_ms', {'value': 800})
        mid, plan = self.bound_plan()
        self.assertEqual(self.runner.run_task(plan['task_action'])['status'], 'CONFIRMED')
        self.assertEqual(len(oracle(self.root / 'fx', 'crm', 'task_created')), 1)

    def test_misleading_toast_is_caught_by_independent_verifier(self):
        post(self.world.crm.url + '/_fault/misleading_toast', {})
        mid, plan = self.bound_plan()
        t = self.runner.run_task(plan['task_action'])
        self.assertEqual(t['status'], 'FAILED')  # page said "Task created", DB has nothing
        self.assertIn('Task created', t['ui_toast'])
        self.assertEqual(oracle(self.root / 'fx', 'crm', 'task_created'), [])

    def test_W4_report_edited_after_approval_invalidates_send(self):
        mid, plan = self.bound_plan()
        self.runner.approve(plan['send_action'], plan['approval_digest'])
        self.world.docs.edit('R-22')
        with self.assertRaises(MissionError) as ctx:
            self.runner.run_send(plan['send_action'])
        self.assertEqual(ctx.exception.code, 'ATTACHMENT_CHANGED')
        self.assertEqual(self.ledger.action(plan['send_action'])['status'], 'SUPERSEDED')
        self.assertEqual(oracle(self.root / 'fx', 'outbox', 'message_committed'), [])
        # re-plan produces a new digest bound to the new hash; old approval cannot be reused
        replan = self.runner.plan(mid)
        self.assertNotEqual(replan['approval_digest'], plan['approval_digest'])
        with self.assertRaises(MissionError):
            self.runner.approve(replan['send_action'], plan['approval_digest'])
        self.runner.approve(replan['send_action'], replan['approval_digest'])
        self.assertEqual(self.runner.run_send(replan['send_action'])['status'], 'CONFIRMED')

    def test_W5_lost_ack_is_uncertain_then_reconciled_without_resend(self):
        mid, plan = self.bound_plan()
        self.runner.approve(plan['send_action'], plan['approval_digest'])
        post(self.world.outbox.url + '/_fault/drop_ack', {})
        res = self.runner.run_send(plan['send_action'])
        states = [e['state'] for e in reversed(self.ledger.events(plan['send_action']))]
        self.assertIn('UNCERTAIN', states)
        self.assertEqual(res['status'], 'CONFIRMED')  # found by op_id in the outbox DB
        self.assertEqual(len(oracle(self.root / 'fx', 'outbox', 'message_committed')), 1)

    def test_W7_correction_supersedes_old_actions_and_approvals(self):
        mid, plan = self.bound_plan('C-208')  # first (wrong) choice: Sales
        self.runner.approve(plan['send_action'], plan['approval_digest'])
        self.runner.correct(mid, 'C-104')  # "No, I mean Jordan from Operations"
        self.assertEqual(self.ledger.action(plan['send_action'])['status'], 'SUPERSEDED')
        with self.assertRaises(MissionError):
            self.runner.run_send(plan['send_action'])
        self.assertEqual(oracle(self.root / 'fx', 'outbox', 'message_committed'), [])
        new = self.runner.plan(mid)
        self.assertEqual(self.ledger.action(new['send_action'])['arguments']['to_addr'], 'jordan.lee@ops.example.test')

    def test_W12_restart_with_dispatching_action_is_uncertain_and_not_resent(self):
        mid, plan = self.bound_plan()
        self.runner.approve(plan['send_action'], plan['approval_digest'])
        a = plan['send_action']
        self.ledger.transition(a, 'READY', {})
        self.ledger.transition(a, 'DISPATCHING', {})  # crash right here, before/after the effect is unknown
        restarted = Ledger(self.root / 'ledger.sqlite3')
        self.assertEqual(restarted.action(a)['status'], 'UNCERTAIN')
        runner2 = WebMissionRunner(restarted, self.runner.urls, self.root / 'fx')
        res = runner2.reconcile(a)
        self.assertEqual(res['status'], 'UNCERTAIN')  # nothing in outbox: hold for a human, do NOT resend
        self.assertEqual(oracle(self.root / 'fx', 'outbox', 'message_committed'), [])

    def test_fault_endpoints_disabled_outside_test_mode(self):
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=False) as w:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post(w.crm.url + '/_fault/popup', {})
            self.assertEqual(ctx.exception.code, 403)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class ConcurrencyTests(unittest.TestCase):
    def test_G5_two_concurrent_missions_serialize_on_executor_lease(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ledger = Ledger(d / 'ledger.sqlite3')
            worlds = [FixtureWorld(d / f'fx{i}') for i in range(2)]
            try:
                plans, runners = [], []
                for i, w in enumerate(worlds):
                    r = WebMissionRunner(ledger, {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}, d / f'fx{i}')
                    m = r.start(CMD)
                    r.bind(m['mission_id'], 'C-104', 1)
                    p = r.plan(m['mission_id'])
                    r.approve(p['send_action'], p['approval_digest'])
                    runners.append(r)
                    plans.append(p)
                results = [None, None]

                def go(i):
                    results[i] = runners[i].run_send(plans[i]['send_action'])['status']
                ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
                [t.start() for t in ts]
                [t.join(120) for t in ts]
                self.assertEqual(results, ['CONFIRMED', 'CONFIRMED'])
                spans = []
                for p in plans:
                    ev = {e['state']: e['at'] for e in ledger.events(p['send_action'])}
                    spans.append((ev['DISPATCHING'], ev['CONFIRMED']))
                (a0, a1), (b0, b1) = sorted(spans)
                self.assertLessEqual(a1, b0, f'dispatch windows overlap: {spans}')  # exclusive execution, from the persisted ledger
                for i, w in enumerate(worlds):
                    self.assertEqual(len(oracle(d / f'fx{i}', 'outbox', 'message_committed')), 1)
            finally:
                [w.stop() for w in worlds]

    def test_G5_fenced_worker_cannot_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            ledger = Ledger(d / 'ledger.sqlite3')
            with FixtureWorld(d / 'fx') as w:
                r = WebMissionRunner(ledger, {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}, d / 'fx')
                m = r.start(CMD)
                r.bind(m['mission_id'], 'C-104', 1)
                p = r.plan(m['mission_id'])
                r.approve(p['send_action'], p['approval_digest'])
                r.hooks['before_dispatch'] = lambda: (r.leases.revoke(), r.leases.acquire('other-worker', 30, 1))  # crash/timeout + takeover
                with self.assertRaises(MissionError) as ctx:
                    r.run_send(p['send_action'])
                self.assertEqual(ctx.exception.code, 'LEASE_LOST')
                self.assertEqual(ledger.action(p['send_action'])['status'], 'CANCELLED')
                self.assertEqual(oracle(d / 'fx', 'outbox', 'message_committed'), [])


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class CompletedMissionTests(unittest.TestCase):
    def test_replanning_after_confirmed_send_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with FixtureWorld(d / 'fx') as w:
                r = WebMissionRunner(Ledger(d / 'l.sqlite3'), {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}, d / 'fx')
                m = r.start(CMD)
                r.bind(m['mission_id'], 'C-104', 1)
                p = r.plan(m['mission_id'])
                r.approve(p['send_action'], p['approval_digest'])
                self.assertEqual(r.run_send(p['send_action'])['status'], 'CONFIRMED')
                with self.assertRaises(MissionError) as ctx:
                    r.plan(m['mission_id'])
                self.assertEqual(ctx.exception.code, 'ALREADY_SENT')
