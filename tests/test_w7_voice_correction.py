"""W7: typed-transcript utterances drive the same intent-revision protocol a microphone would."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'tests')]
from intent import classify
from test_web_g3 import CMD, HAVE_PW, oracle

CANDS = [{'id': 'C-104', 'name': 'Jordan Lee', 'email': 'jordan.lee@ops.example.test', 'org': 'Northwind', 'dept': 'Operations'},
         {'id': 'C-208', 'name': 'Jordan Lee', 'email': 'jordan.lee@sales.example.test', 'org': 'Contoso', 'dept': 'Sales'}]
M = {'candidates': CANDS, 'status': 'ACTIVE'}


class ClassifierTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify('Prepare a follow-up for Jordan Lee', None).kind, 'NEW_GOAL')
        self.assertEqual(classify('cancel that', M).kind, 'CANCEL')
        self.assertEqual(classify("don't send anything", M).kind, 'CANCEL')
        self.assertEqual(classify('yes, send it', M).kind, 'APPROVE')
        self.assertEqual(classify('which report are you sending?', M).kind, 'QUESTION')
        self.assertEqual(classify('', M).kind, 'QUESTION')

    def test_correction_uses_attributes_and_negation(self):
        c = classify('No, I mean Jordan from Operations, not Jordan from Sales', M)
        self.assertEqual(c.kind, 'CORRECTION')
        self.assertEqual(c.matched_candidates, ['C-104'])
        self.assertEqual(c.negated_terms, ['C-208'])
        c = classify('the Contoso one', M)
        self.assertEqual((c.kind, c.matched_candidates), ('REFINEMENT', ['C-208']))

    def test_name_alone_never_disambiguates(self):
        c = classify('No, I mean Jordan Lee', M)
        self.assertEqual(c.kind, 'CORRECTION')
        self.assertEqual(c.matched_candidates, [])


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class VoiceCorrectionE2E(unittest.TestCase):
    def setUp(self):
        from resolve_core import Ledger
        from web_fixtures import FixtureWorld
        from web_mission import WebMissionRunner
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.world = FixtureWorld(self.root / 'fx')
        self.ledger = Ledger(self.root / 'l.sqlite3')
        self.r = WebMissionRunner(self.ledger, {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url}, self.root / 'fx')

    def tearDown(self):
        self.world.stop()
        self.tmp.cleanup()

    def test_correction_after_spoken_approval_kills_old_recipient(self):
        from web_mission import MissionError
        m = self.r.utter(None, CMD)['mission']
        mid = m['mission_id']
        self.assertEqual(m['status'], 'NEEDS_CLARIFICATION')
        out = self.r.utter(mid, 'the one from Sales')
        self.assertEqual(out['mission']['binding']['contact_id'], 'C-208')
        old = self.r.plan(mid)
        out = self.r.utter(mid, 'yes, send it')
        self.assertEqual(out['approval']['status'], 'AUTHORIZED')
        self.assertIn('sales', out['approved_summary'])
        out = self.r.utter(mid, 'No, I mean Jordan from Operations, not Jordan from Sales')
        self.assertEqual(out['classification']['kind'], 'CORRECTION')
        self.assertEqual(out['mission']['intent_revision'], 2)
        self.assertEqual(out['mission']['binding']['contact_id'], 'C-104')
        self.assertEqual(self.ledger.action(old['send_action'])['status'], 'SUPERSEDED')
        with self.assertRaises(MissionError):
            self.r.run_send(old['send_action'])  # the stale, already-authorized send cannot fire
        with self.assertRaises(MissionError) as ctx:
            self.r.utter(mid, 'yes')  # nothing presented at revision 2 yet
        self.assertEqual(ctx.exception.code, 'NOTHING_PRESENTED')
        new = self.r.plan(mid)
        self.r.utter(mid, 'approve')
        self.assertEqual(self.r.run_send(new['send_action'])['status'], 'CONFIRMED')
        sent = oracle(self.root / 'fx', 'outbox', 'message_committed')
        self.assertEqual([s['to'] for s in sent], ['jordan.lee@ops.example.test'])
        kinds = [u['kind'] for u in self.r.get(mid)['utterances']]
        self.assertEqual(kinds, ['REFINEMENT', 'APPROVE', 'CORRECTION', 'APPROVE'])

    def test_correction_during_dispatch_reports_in_flight_action(self):
        m = self.r.start(CMD)
        self.r.bind(m['mission_id'], 'C-208', 1)
        p = self.r.plan(m['mission_id'])
        self.r.approve(p['send_action'], p['approval_digest'])
        self.ledger.transition(p['send_action'], 'READY', {})
        self.ledger.transition(p['send_action'], 'DISPATCHING', {})
        out = self.r.utter(m['mission_id'], 'No, I mean Jordan from Operations')
        flight = out['mission']['last_correction']['in_flight']
        self.assertEqual(flight, [{'action_id': p['send_action'], 'status': 'DISPATCHING'}])
        self.assertEqual(self.ledger.action(p['send_action'])['status'], 'DISPATCHING')  # honest: not pretended cancelled

    def test_cancel_supersedes_and_blocks_planning(self):
        from web_mission import MissionError
        m = self.r.start(CMD)
        self.r.bind(m['mission_id'], 'C-104', 1)
        p = self.r.plan(m['mission_id'])
        out = self.r.utter(m['mission_id'], 'cancel, do not send')
        self.assertEqual(out['mission']['status'], 'CANCELLED')
        self.assertEqual({self.ledger.action(a)['status'] for a in (p['task_action'], p['send_action'])}, {'SUPERSEDED'})
        with self.assertRaises(MissionError):
            self.r.plan(m['mission_id'])
        self.assertEqual(oracle(self.root / 'fx', 'crm', 'task_created'), [])


if __name__ == '__main__':
    unittest.main()
