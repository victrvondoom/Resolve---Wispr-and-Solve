import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from resolve_core import Action, Ledger, digest, reconcile_uncertain_local, resolve_candidates, seed_demo, uid, verify_outbox


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        self.ledger = Ledger(self.db)
        seed_demo(self.ledger)

    def tearDown(self): self.tmp.cleanup()

    def test_duplicate_name_is_ambiguous(self):
        found = resolve_candidates(self.ledger, 'contact', 'Alex Morgan')
        self.assertEqual(len(found), 2)
        self.assertEqual({x['stable_id'] for x in found}, {'crm:contact:104','crm:contact:208'})

    def test_latest_modified_report_is_explicit(self):
        found = resolve_candidates(self.ledger, 'document', context='Northwind')
        self.assertEqual(max(found, key=lambda e:e['modified_at'])['entity_id'], 'report_latest')

    def make_action(self):
        a=Action(uid('act'),'mission_demo',1,'simulated_send','contact_alex_1','test-agent','local-outbox',None,{'attachment_id':'report_latest'},None,{'attachment_id':'doc:report:22'},'NON_IDEMPOTENT','consequential')
        self.ledger.create_action(a); self.ledger.transition(a.action_id,'BOUND'); self.ledger.transition(a.action_id,'NEEDS_APPROVAL')
        return a

    def test_ledger_rejects_invalid_transition(self):
        a=self.make_action()
        with self.assertRaisesRegex(ValueError,'illegal action transition'):
            self.ledger.transition(a.action_id,'CONFIRMED')

    def test_restart_marks_dispatching_uncertain(self):
        a=self.make_action()
        for state in ('AUTHORIZED','READY','DISPATCHING'): self.ledger.transition(a.action_id,state)
        recovered=Ledger(self.db)
        self.assertEqual(recovered.action(a.action_id)['status'],'UNCERTAIN')
        self.assertEqual(recovered.events(a.action_id)[0]['state'],'UNCERTAIN')

    def test_verifier_checks_effect_independently(self):
        self.assertEqual(verify_outbox(self.ledger,'contact_alex_1','report_latest')['status'],'FAILED')
        self.ledger.add_outbox('contact_alex_1','report_latest','body')
        result=verify_outbox(self.ledger,'contact_alex_1','report_latest')
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['evidence']['source'],'local SQLite outbox')

    def test_followup_effect_is_part_of_verification(self):
        message=self.ledger.add_outbox('contact_alex_1','report_latest','body')
        self.ledger.set_followup('followup_northwind','contact_alex_1','contacted',message)
        result=verify_outbox(self.ledger,'contact_alex_1','report_latest','followup_northwind')
        self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual(result['evidence']['followup_last_message_id'],message)
        self.ledger.set_followup('followup_northwind','contact_alex_1','pending')
        self.assertEqual(verify_outbox(self.ledger,'contact_alex_1','report_latest','followup_northwind')['status'],'UNCERTAIN')

    def test_duplicate_effect_is_uncertain(self):
        self.ledger.add_outbox('contact_alex_1','report_latest','body')
        self.ledger.add_outbox('contact_alex_1','report_latest','body')
        self.assertEqual(verify_outbox(self.ledger,'contact_alex_1','report_latest')['status'],'UNCERTAIN')

    def test_action_digest_binds_exact_payload(self):
        a=self.make_action(); before=digest(self.ledger.action(a.action_id))
        action=self.ledger.action(a.action_id); action['arguments']['attachment_id']='report_old'
        self.assertNotEqual(before,digest(action))

    def test_revision_history_is_durable_and_compare_and_swap(self):
        revised=self.ledger.revise_mission('mission_demo',1,'Send only after approval.')
        self.assertEqual(revised['intent_revision'],2)
        self.assertEqual(revised['status'],'NEEDS_CLARIFICATION')
        with self.assertRaisesRegex(ValueError,'stale mission revision'):
            self.ledger.revise_mission('mission_demo',1,'old instruction')
        history=Ledger(self.db).mission_history('mission_demo')
        self.assertEqual([entry['revision'] for entry in history],[1,2])
        self.assertIn('original_request',history[0]['contract'])

    def test_changed_instruction_cannot_keep_old_send_permission(self):
        paused=self.ledger.revise_mission('mission_demo',1,'Do not send the report.')
        self.assertEqual(paused['status'],'PAUSED')
        self.assertNotIn('simulated_send_after_approval',paused['permitted_actions'])
        resumed=self.ledger.revise_mission('mission_demo',2,'Send the latest report after approval.')
        self.assertEqual(resumed['status'],'ACTIVE')
        self.assertIn('simulated_send_after_approval',resumed['permitted_actions'])

    def test_action_scoped_verification_allows_later_separate_send(self):
        first=self.ledger.commit_simulated_send('action_one','contact_alex_1','report_latest','body','followup_northwind')
        second=self.ledger.commit_simulated_send('action_two','contact_alex_1','report_latest','body','followup_northwind')
        self.assertNotEqual(first,second)
        self.assertEqual(verify_outbox(self.ledger,'contact_alex_1','report_latest','followup_northwind','action_two')['status'],'CONFIRMED')
        with self.assertRaises(Exception):
            self.ledger.commit_simulated_send('action_two','contact_alex_1','report_latest','body','followup_northwind')

    def test_recovery_reconciles_known_local_effect_without_retry(self):
        action=Action(uid('act'),'mission_demo',1,'simulated_send_and_update_followup','contact_alex_1','test-agent','local-outbox',None,{'attachment_id':'report_latest','followup_id':'followup_northwind'},None,{'attachment_id':'doc:report:22'},'NON_IDEMPOTENT','consequential')
        self.ledger.create_action(action)
        for state in ('BOUND','AUTHORIZED','READY','DISPATCHING'):
            self.ledger.transition(action.action_id,state)
        message=self.ledger.commit_simulated_send(action.action_id,'contact_alex_1','report_latest','body','followup_northwind')
        restarted=Ledger(self.db)
        self.assertEqual(restarted.action(action.action_id)['status'],'UNCERTAIN')
        result=reconcile_uncertain_local(restarted)
        self.assertEqual(result[0]['status'],'CONFIRMED')
        self.assertEqual(restarted.action(action.action_id)['status'],'CONFIRMED')
        self.assertEqual(restarted.outbox_for_action(action.action_id)[0]['id'],message)


if __name__ == '__main__': unittest.main()
