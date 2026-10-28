"""W8: recorded reusable skills. Real Playwright demonstration + replay against the CRM fixture.

Every effect assertion reads the fixture's crm.sqlite3 (tasks / ground_truth) directly, never the toast.
Browser actions are DOM-level (Playwright), not physical cursor movement.
"""
import copy
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

from web_fixtures import FixtureWorld
from web_skills import SkillError, SkillLibrary, skill_digest


def crm_rows(root, sql, args=()):
    db = sqlite3.connect(str(Path(root) / 'crm.sqlite3'))
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def truth_tasks(root):
    return [json.loads(r[0]) for r in crm_rows(root, "SELECT detail FROM ground_truth WHERE kind='task_created'")]


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    return urllib.request.urlopen(req)


def reseal(export: dict) -> dict:
    """Test helper: recompute the digest after deliberately editing an export."""
    e = copy.deepcopy(export)
    e['digest'] = skill_digest(e['skill'])
    e['skill']['digest'] = e['digest']
    return e


@unittest.skipUnless(HAVE_PW, 'playwright not installed (portable browser tests need .venv)')
class WebSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.lib = SkillLibrary(base / 'skills.sqlite3')
        cls.root_a = base / 'fixture_a'
        with FixtureWorld(cls.root_a) as w:
            cls.skill = cls.lib.record({'crm': w.crm.url}, cls.root_a, 'C-104', 'Send Q3 ops report')
        cls.sid = cls.skill['skill_id']

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fresh(self, name):
        root = Path(self.tmp.name) / name
        return root, FixtureWorld(root)

    def test_1_record_compiles_parameterized_pending_skill(self):
        s = self.skill
        # the demonstration's effect really exists in fixture A's DB
        self.assertEqual(crm_rows(self.root_a, 'SELECT contact_id,title FROM tasks'), [('C-104', 'Send Q3 ops report')])
        self.assertEqual(s['approval_status'], 'PENDING_REVIEW')
        self.assertEqual(s['version'], 1)
        self.assertEqual(s['parameters_schema'], {'contact_id': 'str', 'title': 'str'})
        self.assertEqual(s['preconditions'], [{'kind': 'contact_exists', 'param': 'contact_id'}])
        self.assertGreaterEqual(s['source_provenance']['event_count'], 4)
        self.assertEqual(len(s['source_provenance']['events_digest']), 64)
        fills = [st for st in s['steps'] if st['action'] == 'fill']
        self.assertEqual([(f['locator'], f['value']) for f in fills],
                         [({'by': 'label', 'value': 'Contact ID'}, '{contact_id}'), ({'by': 'label', 'value': 'Task title'}, '{title}')])
        self.assertIn({'by': 'name', 'value': 'title'}, fills[1]['fallbacks'])
        click = s['steps'][-1]
        self.assertEqual(click['locator'], {'by': 'role', 'role': 'button', 'name': 'Create task'})
        self.assertIn({'by': 'css', 'value': '#f [type=submit]'}, click['fallbacks'])
        self.assertNotIn('C-104', json.dumps(s['steps']))
        self.assertNotIn('Send Q3 ops report', json.dumps(s['steps']))
        self.assertEqual(s['risk_class'], 'LOCAL_REVERSIBLE')
        self.assertEqual(s['permissions'], ['crm.write'])
        # replay refused before approval; no task written in a fresh fixture
        root, w = self.fresh('unapproved')
        with w:
            r = self.lib.replay(self.sid, {'contact_id': 'C-310', 'title': 'x'}, {'crm': w.crm.url}, root, version=1)
        self.assertEqual(r['status'], 'REFUSED')
        self.assertEqual(r['refusal']['code'], 'NOT_APPROVED')
        self.assertEqual(crm_rows(root, 'SELECT COUNT(*) FROM tasks')[0][0], 0)

    def test_2_approved_replay_on_fresh_fixture_with_new_params(self):
        self._approve_v1()
        root, w = self.fresh('fresh_b')
        with w:
            r = self.lib.replay(self.sid, {'contact_id': 'C-310', 'title': 'Check in with Priya'}, {'crm': w.crm.url}, root)
        self.assertEqual(r['status'], 'CONFIRMED', r)
        self.assertTrue(all(st['strategy_used'] == 'primary' for st in r['steps']))
        self.assertEqual(crm_rows(root, 'SELECT contact_id,title,op_id FROM tasks'), [('C-310', 'Check in with Priya', r['op_id'])])
        gt = truth_tasks(root)
        self.assertEqual(len(gt), 1)
        self.assertEqual((gt[0]['contact'], gt[0]['op_id']), ('C-310', r['op_id']))
        # bad params refused before any browser action
        r2 = self.lib.replay(self.sid, {'contact_id': 'C-310'}, {'crm': 'http://127.0.0.1:9'}, root)
        self.assertEqual(r2['refusal']['code'], 'INVALID_PARAMS')

    def test_3_precondition_unknown_contact_refused(self):
        self._approve_v1()
        root, w = self.fresh('fresh_c')
        with w:
            r = self.lib.replay(self.sid, {'contact_id': 'C-999', 'title': 'Ghost'}, {'crm': w.crm.url}, root)
        self.assertEqual(r['status'], 'REFUSED')
        self.assertEqual(r['refusal']['code'], 'PRECONDITION_FAILED')
        self.assertEqual(crm_rows(root, 'SELECT COUNT(*) FROM tasks')[0][0], 0)
        self.assertEqual(truth_tasks(root), [])

    def test_4a_layout_shift_handled_by_fallbacks(self):
        self._approve_v1()
        root, w = self.fresh('shift_fallback')
        with w:
            post(w.crm.url + '/_fault/layout_shift', {})
            r = self.lib.replay(self.sid, {'contact_id': 'C-208', 'title': 'Renewal call'}, {'crm': w.crm.url}, root)
        self.assertEqual(r['status'], 'CONFIRMED', r)
        by_action = {(st['action'], st['index']): st['strategy_used'] for st in r['steps']}
        steps = self.lib.get(self.sid, 1)['steps']
        title_i = next(i for i, s in enumerate(steps) if s.get('value') == '{title}')
        click_i = len(steps) - 1
        self.assertEqual(by_action[('fill', 0)], 'primary')  # Contact ID label unchanged
        self.assertTrue(by_action[('fill', title_i)].startswith('fallback'), by_action)
        self.assertTrue(by_action[('click', click_i)].startswith('fallback') and by_action[('click', click_i)].endswith(':css'), by_action)
        self.assertEqual(crm_rows(root, 'SELECT contact_id,title,op_id FROM tasks'), [('C-208', 'Renewal call', r['op_id'])])

    def test_4b_layout_shift_without_fallbacks_proposes_repair(self):
        lib = SkillLibrary(Path(self.tmp.name) / 'skills_nofallback.sqlite3')
        exp = self.lib.export(self.sid, 1)
        edited = copy.deepcopy(exp)
        for st in edited['skill']['steps']:
            if st['action'] in ('fill', 'click'):
                st['fallbacks'] = []
        with self.assertRaises(SkillError):  # edited but not resealed: rejected
            lib.import_skill(edited)
        imported = lib.import_skill(reseal(edited))
        self.assertEqual(imported['approval_status'], 'PENDING_REVIEW')
        v1 = lib.approve(self.sid, 1)
        root, w = self.fresh('shift_repair')
        with w:
            post(w.crm.url + '/_fault/layout_shift', {})
            r = lib.replay(self.sid, {'contact_id': 'C-104', 'title': 'Post-update follow-up'}, {'crm': w.crm.url}, root)
            self.assertEqual(r['status'], 'FAILED', r)
            self.assertFalse(r['side_effect_dispatched'])
            self.assertEqual(crm_rows(root, 'SELECT COUNT(*) FROM tasks')[0][0], 0)
            prop = r['repair_proposal']
            self.assertIsNotNone(prop)
            self.assertEqual((prop['version'], prop['approval_status']), (2, 'PENDING_REVIEW'))
            self.assertEqual(prop['steps'][r['failed_step']]['locator'], {'by': 'label', 'value': 'Follow-up title'})
            self.assertEqual(prop['steps'][-1]['locator'], {'by': 'role', 'role': 'button', 'name': 'Save follow-up'})
            self.assertEqual(len(prop['learning_history']), 1)
            # approved v1 untouched
            after = lib.get(self.sid, 1)
            self.assertEqual((after['digest'], after['approval_status'], after['steps']), (v1['digest'], 'APPROVED', v1['steps']))
            with self.assertRaises(sqlite3.DatabaseError):  # storage-level immutability
                db = sqlite3.connect(str(lib.db_path))
                try:
                    with db:
                        db.execute("UPDATE web_skills SET body='{}' WHERE version=1")
                finally:
                    db.close()
            # v2 not usable until approved
            r_pending = lib.replay(self.sid, {'contact_id': 'C-104', 'title': 'x'}, {'crm': w.crm.url}, root, version=2)
            self.assertEqual(r_pending['refusal']['code'], 'NOT_APPROVED')
            lib.approve(self.sid, 2)
            r2 = lib.replay(self.sid, {'contact_id': 'C-104', 'title': 'Post-update follow-up'}, {'crm': w.crm.url}, root)
        self.assertEqual((r2['status'], r2['version']), ('CONFIRMED', 2), r2)
        self.assertTrue(all(st['strategy_used'] == 'primary' for st in r2['steps']))
        self.assertEqual(crm_rows(root, 'SELECT contact_id,title,op_id FROM tasks'), [('C-104', 'Post-update follow-up', r2['op_id'])])
        self.assertEqual(len(truth_tasks(root)), 1)

    def test_5_export_import_roundtrip_and_tamper(self):
        exp = self.lib.export(self.sid, 1)
        json.dumps(exp)  # JSON-able
        other = SkillLibrary(Path(self.tmp.name) / 'skills_other.sqlite3')
        imp = other.import_skill(json.loads(json.dumps(exp)))
        self.assertEqual(imp['approval_status'], 'PENDING_REVIEW')
        self.assertEqual(imp['digest'], exp['digest'])
        self.assertEqual(imp['steps'], self.skill['steps'])
        self.assertEqual(other.import_skill(exp)['digest'], exp['digest'])  # idempotent
        tampered = copy.deepcopy(exp)
        tampered['skill']['permissions'] = ['crm.write', 'outbox.send']
        third = SkillLibrary(Path(self.tmp.name) / 'skills_third.sqlite3')
        with self.assertRaises(SkillError) as cm:
            third.import_skill(tampered)
        self.assertEqual(cm.exception.code, 'DIGEST_MISMATCH')
        self.assertEqual(third.list(), [])

    def _approve_v1(self):
        if self.lib.get(self.sid, 1)['approval_status'] != 'APPROVED':
            self.lib.approve(self.sid, 1)


if __name__ == '__main__':
    unittest.main()
