import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_runtime import CursorLeaseManager, LeaseLost, MissionScheduler, TaskSpec, register_demo_readers
from resolve_core import Ledger, seed_demo


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'mission.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, task_id, agent_id='reader', dependencies=(), read_only=True, resource=None, capabilities=frozenset({'entities.read'}), seconds=2):
        return TaskSpec(task_id, 'mission_demo', 1, agent_id, 'read', {}, dependencies, capabilities, read_only, resource, 3, seconds)

    def test_two_readers_really_overlap_and_dependencies_wait(self):
        scheduler = MissionScheduler(self.db, max_workers=3)
        barrier = threading.Barrier(2)
        sequence = []
        sequence_lock = threading.Lock()
        def reader(message, context):
            context.use('entities.read')
            with sequence_lock:
                sequence.append('start:' + message.task_id)
            barrier.wait(timeout=1)
            time.sleep(0.03)
            with sequence_lock:
                sequence.append('end:' + message.task_id)
            return {'observed': message.task_id}
        def joiner(message, context):
            with sequence_lock:
                sequence.append('join')
            return {'ready': True}
        scheduler.register('reader', reader)
        scheduler.register('joiner', joiner)
        tasks = [self.spec('contact'), self.spec('report'), self.spec('join', agent_id='joiner', dependencies=('contact','report'))]
        result = scheduler.run(tasks)
        self.assertEqual({r.status for r in result.values()}, {'SUCCEEDED'})
        self.assertLess(max(sequence.index('start:contact'), sequence.index('start:report')), min(sequence.index('end:contact'), sequence.index('end:report')))
        self.assertEqual(sequence[-1], 'join')
        self.assertEqual([e['status'] for e in scheduler.events('mission_demo')].count('SUCCEEDED'), 3)

    def test_dependency_failure_blocks_and_cycle_is_rejected(self):
        scheduler = MissionScheduler(self.db)
        scheduler.register('reader', lambda msg, ctx: (_ for _ in ()).throw(RuntimeError('broken read')))
        result = scheduler.run([self.spec('bad'), self.spec('dependent', dependencies=('bad',))])
        self.assertEqual(result['bad'].status, 'FAILED')
        self.assertEqual(result['dependent'].status, 'BLOCKED')
        self.assertEqual(result['dependent'].blockers, ('bad',))
        with self.assertRaisesRegex(ValueError, 'cycle'):
            scheduler.run([self.spec('a', dependencies=('b',)), self.spec('b', dependencies=('a',))])

    def test_cross_manager_cursor_lease_serializes_and_fences(self):
        first = CursorLeaseManager(self.db)
        second = CursorLeaseManager(self.db)
        lease1 = first.acquire('task-one', duration_seconds=1, wait_seconds=0)
        with self.assertRaises(TimeoutError):
            second.acquire('task-two', duration_seconds=1, wait_seconds=0.01)
        self.assertTrue(first.release(lease1))
        lease2 = second.acquire('task-two', duration_seconds=1, wait_seconds=0)
        self.assertGreater(lease2.generation, lease1.generation)
        with self.assertRaises(LeaseLost):
            first.validate(lease1)
        second.revoke()
        with self.assertRaises(LeaseLost):
            second.validate(lease2)

    def test_cursor_specialists_serialize_through_lease(self):
        scheduler = MissionScheduler(self.db, max_workers=2)
        active = 0
        max_active = 0
        lock = threading.Lock()
        def actor(message, context):
            nonlocal active, max_active
            context.use('desktop.write')
            context.cursor_token()
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            context.check_active()
            with lock:
                active -= 1
            return {'sent': True}
        scheduler.register('actor', actor)
        a = self.spec('a','actor',read_only=False,resource='cursor',capabilities=frozenset({'desktop.write'}))
        b = self.spec('b','actor',read_only=False,resource='cursor',capabilities=frozenset({'desktop.write'}))
        result = scheduler.run([a,b])
        self.assertEqual({r.status for r in result.values()}, {'SUCCEEDED'})
        self.assertEqual(max_active, 1)

    def test_cancellation_revokes_cursor_and_blocks_dependents(self):
        scheduler = MissionScheduler(self.db, max_workers=2)
        started = threading.Event()
        def actor(message, context):
            context.use('desktop.write')
            started.set()
            while True:
                context.check_active()
                time.sleep(0.005)
        scheduler.register('actor', actor)
        scheduler.register('reader', lambda msg, ctx: {'done': True})
        result_holder = {}
        tasks = [self.spec('cursor','actor',read_only=False,resource='cursor',capabilities=frozenset({'desktop.write'})), self.spec('after',dependencies=('cursor',))]
        worker = threading.Thread(target=lambda: result_holder.update(scheduler.run(tasks)))
        worker.start()
        self.assertTrue(started.wait(1))
        scheduler.cancel()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result_holder['cursor'].status, 'CANCELLED')
        self.assertEqual(result_holder['after'].status, 'BLOCKED')

    def test_stale_revision_and_permission_denial(self):
        scheduler = MissionScheduler(self.db, revision_check=lambda mission, revision: revision == 2)
        scheduler.register('reader', lambda msg, ctx: {'should_not_run': True})
        self.assertEqual(scheduler.run([self.spec('stale')])['stale'].status, 'SUPERSEDED')
        scheduler.revision_check = None
        scheduler.register('reader', lambda msg, ctx: (ctx.use('desktop.write'), {'done': True})[1])
        self.assertEqual(scheduler.run([self.spec('denied')])['denied'].status, 'FAILED')

    def test_registered_demo_readers_use_actual_local_state(self):
        ledger = Ledger(self.db)
        seed_demo(ledger)
        scheduler = MissionScheduler(self.db)
        register_demo_readers(scheduler, ledger)
        contact = self.spec('contact', agent_id='contact-resolver')
        report = TaskSpec('report','mission_demo',1,'report-resolver','read',{'context':'Northwind account'},(),frozenset({'entities.read'}),True,None,3,2)
        result = scheduler.run([contact, report])
        self.assertTrue(result['contact'].result['ambiguous'])
        self.assertEqual(result['report'].result['latest']['entity_id'], 'report_latest')


if __name__ == '__main__':
    unittest.main()
