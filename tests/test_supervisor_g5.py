"""G5: tool-bounded roles, parallel read-only fan-out, entity gate, separate verifier, retry, supersede."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'tests')]
from test_web_g3 import CMD, HAVE_PW, oracle


def by_agent(out):
    return {t['agent_id']: t for t in out['tasks']}


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class SupervisorTests(unittest.TestCase):
    def setUp(self):
        from resolve_core import Ledger
        from supervisor import Supervisor
        from web_fixtures import FixtureWorld
        from web_mission import WebMissionRunner
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.world = FixtureWorld(self.root / 'fx')
        self.ledger = Ledger(self.root / 'l.sqlite3')
        self.runner = WebMissionRunner(self.ledger, {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url}, self.root / 'fx')
        self.sup = Supervisor(self.runner, self.root / 'fx')

    def tearDown(self):
        self.world.stop()
        self.tmp.cleanup()

    def test_parallel_fanout_and_entity_gate_blocks_planning(self):
        for role in ('EntityResolver', 'Researcher', 'FileWorker', 'OSObserver'):
            self.sup.hooks[role] = lambda: time.sleep(0.4)  # injected latency makes scheduling observable
        m = self.runner.start(CMD)
        out = self.sup.investigate(m['mission_id'])
        t = by_agent(out)
        reads = [t[r] for r in ('EntityResolver', 'Researcher', 'FileWorker', 'OSObserver')]
        self.assertTrue(all(r['status'] == 'SUCCEEDED' for r in reads))
        start, end = max(r['started_at'] for r in reads), min(r['finished_at'] for r in reads)
        self.assertLess(start, end, 'read-only roles did not overlap in time')
        self.assertLess(max(r['finished_at'] for r in reads) - min(r['started_at'] for r in reads), 1.2)  # 4×0.4s serial would be ≥1.6s
        self.assertEqual(t['Planner']['status'], 'FAILED')
        self.assertIn('NEEDS_CLARIFICATION', t['Planner']['error'])
        self.assertEqual(t['SafetyApproval']['status'], 'BLOCKED')
        self.assertEqual(self.runner.get(m['mission_id'])['actions'], [])
        self.assertFalse(t['OSObserver']['result']['available'])  # honest on Linux
        self.assertEqual(t['EntityResolver']['result']['tools_granted'], ['crm.read'])

    def test_full_pipeline_with_separate_verifier(self):
        m = self.runner.start(CMD)
        self.runner.utter(m['mission_id'], 'the one from Operations')
        inv = self.sup.investigate(m['mission_id'])
        self.assertTrue(inv['planned'])
        verdicts = {v['risk']: v['verdict'] for v in inv['safety']['verdicts']}
        self.assertEqual(verdicts, {'LOCAL_REVERSIBLE': 'AUTO_ALLOWED', 'EXTERNAL_COMMUNICATION': 'REQUIRES_HUMAN_APPROVAL'})
        first = self.sup.execute(m['mission_id'])  # send not approved: executor must skip it
        skipped = [e for e in by_agent(first)['BrowserExecutor']['result']['executed'] if e.get('skipped')]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(oracle(self.root / 'fx', 'outbox', 'message_committed'), [])
        self.runner.utter(m['mission_id'], 'approve')
        out = self.sup.execute(m['mission_id'])
        claims = out['verification']['claims']
        self.assertEqual({c['result'] for c in claims}, {'CONFIRMED'})
        self.assertTrue(out['verification']['all_agree'])
        self.assertTrue(all(c['verifier_id'] != c['executor_id'] for c in claims))
        self.assertEqual(len(oracle(self.root / 'fx', 'crm', 'task_created')), 1)
        self.assertEqual(len(oracle(self.root / 'fx', 'outbox', 'message_committed')), 1)
        perf = by_agent(out)['PerformanceOptimizer']['result']
        self.assertGreaterEqual(perf['tasks_measured'], 3)
        self.assertEqual(perf['llm_calls'], 0)

    def test_capability_boundary_is_enforced(self):
        from agent_runtime import TaskSpec
        m = self.runner.start(CMD)
        bad = TaskSpec('x1', m['mission_id'], 1, 'EntityResolver', 'entities', {'deps': ()}, (), frozenset({'docs.read'}), True, None, 5, 10)
        res = self.sup.scheduler.run([bad])['x1']
        self.assertEqual(res.status, 'FAILED')
        self.assertIn('capability denied: crm.read', res.error)

    def test_intent_revision_mid_research_supersedes_and_stops_planning(self):
        m = self.runner.start(CMD)
        self.runner.bind(m['mission_id'], 'C-104', 1)
        self.sup.hooks['Researcher'] = lambda: self.runner.correct(m['mission_id'], 'C-208')
        out = self.sup.investigate(m['mission_id'])
        self.assertFalse(out['planned'])
        self.assertIn('SUPERSEDED', {t['status'] for t in out['tasks']})
        self.assertEqual(self.runner.get(m['mission_id'])['actions'], [])

    def test_timed_out_read_is_retried_once(self):
        m = self.runner.start(CMD)
        self.runner.bind(m['mission_id'], 'C-104', 1)
        calls = []

        def slow_once():
            calls.append(1)
            if len(calls) == 1:
                time.sleep(0.6)
        self.sup.hooks['FileWorker'] = slow_once
        out = self.sup.investigate(m['mission_id'], budgets={'FileWorker': 0.3})
        self.assertEqual([r['status'] for r in out['retried']], ['SUCCEEDED'])
        self.assertIn('TIMED_OUT', {t['status'] for t in out['tasks'] if t['agent_id'] == 'FileWorker'})
        self.assertTrue(out['planned'])

    def test_disk_and_app_disagree_blocks_plan(self):
        m = self.runner.start(CMD)
        self.runner.bind(m['mission_id'], 'C-104', 1)
        f = self.root / 'fx' / 'files' / 'R-22.txt'
        f.write_bytes(f.read_bytes() + b'tampered on disk only\n')
        out = self.sup.investigate(m['mission_id'])
        planner = by_agent(out)['Planner']
        self.assertEqual(planner['status'], 'FAILED')
        self.assertIn('VERSION_CONFLICT', planner['error'])

    def test_desktop_executor_refuses_honestly_without_windows(self):
        import windows_adapter
        if windows_adapter.IS_WINDOWS:
            self.skipTest('Windows host: native path covered by smoke-windows')
        m = self.runner.start(CMD)
        spec = self.sup._spec(m, 'DesktopExecutor', 'desktop', (), 5.0)
        res = self.sup.scheduler.run([spec])[spec.task_id]
        self.assertEqual(res.status, 'FAILED')
        self.assertIn('WINDOWS_NATIVE_TESTS_NOT_RUN', res.error)


if __name__ == '__main__':
    unittest.main()
