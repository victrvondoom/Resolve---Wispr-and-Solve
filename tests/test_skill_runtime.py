import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from resolve_core import Action, Ledger, asdict, seed_demo, uid, verify_outbox
from skill_runtime import AmbiguousEntity, SkillError, SkillStore, candidate_from_confirmed


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'skills.sqlite3'
        self.ledger = Ledger(self.database)
        seed_demo(self.ledger)
        self.store = SkillStore(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def confirmed_action(self):
        action = Action(
            uid('act'), 'mission_demo', 1, 'simulated_send_and_update_followup',
            'contact_alex_1', 'local-demo-agent', 'local-outbox', None,
            {'attachment_id':'report_latest','body':'Approved report','followup_id':'followup_northwind'},
            None, {'recipient_id':'crm:contact:104','attachment_id':'doc:report:22','followup_status':'contacted'},
            'NON_IDEMPOTENT','consequential',
        )
        self.ledger.create_action(action)
        for state in ('BOUND','NEEDS_APPROVAL','AUTHORIZED','READY','DISPATCHING'):
            self.ledger.transition(action.action_id,state)
        message = self.ledger.add_outbox('contact_alex_1','report_latest','Approved report')
        self.ledger.set_followup('followup_northwind','contact_alex_1','contacted',message)
        self.ledger.transition(action.action_id,'DISPATCHED',{'message_id':message})
        self.ledger.transition(action.action_id,'OBSERVING')
        verified = verify_outbox(self.ledger,'contact_alex_1','report_latest','followup_northwind')
        self.assertEqual(verified['status'],'CONFIRMED')
        self.ledger.transition(action.action_id,'CONFIRMED',{'verification':verified,'evidence_references':[message]})
        return action.action_id

    def activate_candidate(self):
        action_id = self.confirmed_action()
        candidate = candidate_from_confirmed(self.ledger,action_id)
        review = self.store.propose(candidate)
        self.store.activate(candidate.skill_id,candidate.version,review)
        return candidate

    def test_only_confirmed_action_can_be_compiled(self):
        with self.assertRaisesRegex(SkillError,'only independently confirmed'):
            candidate_from_confirmed(self.ledger,'missing')
        action_id = self.confirmed_action()
        candidate = candidate_from_confirmed(self.ledger,action_id)
        self.assertEqual(candidate.provenance['source_action_id'],action_id)
        self.assertTrue(candidate.actions[0].approval_required)
        self.assertEqual(candidate.entity_constraints['version_policy'],'latest_modified_timestamp')

    def test_save_requires_digest_approval_before_activation(self):
        action_id = self.confirmed_action()
        candidate = candidate_from_confirmed(self.ledger,action_id)
        review = self.store.propose(candidate)
        self.assertEqual(self.store.get(candidate.skill_id,1)[1],'PENDING_APPROVAL')
        with self.assertRaisesRegex(SkillError,'approval digest mismatch'):
            self.store.activate(candidate.skill_id,1,'wrong')
        self.assertEqual(self.store.get(candidate.skill_id,1)[1],'PENDING_APPROVAL')
        self.store.activate(candidate.skill_id,1,review)
        self.assertEqual(self.store.get(candidate.skill_id,1)[1],'ACTIVE')
        self.assertEqual([row['status'] for row in self.store.events(candidate.skill_id)],['PENDING_APPROVAL','ACTIVE'])

    def test_replay_requires_contact_selection_and_never_dispatches(self):
        candidate = self.activate_candidate()
        before = len(self.ledger.outbox())
        with self.assertRaises(AmbiguousEntity) as raised:
            self.store.prepare_replay(candidate.skill_id,self.ledger,'mission_demo',1,{'context':'Northwind account'})
        self.assertEqual(raised.exception.entity_kind,'contact')
        plan = self.store.prepare_replay(candidate.skill_id,self.ledger,'mission_demo',1,{'context':'Northwind account','contact_id':'contact_alex_1'})
        self.assertEqual(plan.status,'NEEDS_APPROVAL')
        self.assertTrue(plan.approval_required)
        self.assertEqual(plan.report['entity_id'],'report_latest')
        self.assertEqual(len(self.ledger.outbox()),before)

    def test_replay_rejects_old_version_and_superseded_intent(self):
        candidate = self.activate_candidate()
        with self.assertRaisesRegex(SkillError,'not current'):
            self.store.prepare_replay(candidate.skill_id,self.ledger,'mission_demo',1,{'context':'Northwind account','contact_id':'contact_alex_1','report_id':'report_old'})
        mission = self.ledger.get_json('missions','mission_demo')
        mission['intent_revision']=2
        self.ledger.write_json('missions','mission_demo',mission)
        with self.assertRaisesRegex(SkillError,'revision changed'):
            self.store.prepare_replay(candidate.skill_id,self.ledger,'mission_demo',1,{'context':'Northwind account','contact_id':'contact_alex_1'})

    def test_active_skill_tamper_is_detected(self):
        candidate = self.activate_candidate()
        with self.store._connect() as db:
            row = db.execute('SELECT data FROM skills WHERE skill_id=? AND version=1',(candidate.skill_id,)).fetchone()
            raw = json.loads(row['data'])
            raw['description']='Changed after approval'
            db.execute('UPDATE skills SET data=? WHERE skill_id=? AND version=1',(json.dumps(raw),candidate.skill_id))
        with self.assertRaisesRegex(SkillError,'no longer matches reviewed content'):
            self.store.active(candidate.skill_id)

    def test_new_approved_version_deactivates_previous(self):
        candidate = self.activate_candidate()
        second = replace(candidate,version=2,description='Second reviewed version')
        review = self.store.propose(second)
        self.store.activate(second.skill_id,2,review)
        self.assertEqual(self.store.get(candidate.skill_id,1)[1],'INACTIVE')
        self.assertEqual(self.store.active(candidate.skill_id).version,2)


if __name__ == '__main__':
    unittest.main()
