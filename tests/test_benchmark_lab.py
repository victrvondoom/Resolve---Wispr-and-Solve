import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from benchmark_lab import run_all, run_case


class BenchmarkLabTests(unittest.TestCase):
    def test_wrong_entity_is_measured_from_persisted_effect(self):
        baseline = run_case('baseline_a', 'F04_wrong_same_name_recipient', 11)
        guarded = run_case('system_c', 'F04_wrong_same_name_recipient', 11)
        self.assertEqual(baseline['persisted_outbox'][0]['recipient_id'],
                         'contact_alex_2')
        self.assertEqual(baseline['wrong_target_effects'], 1)
        self.assertEqual(guarded['persisted_outbox'][0]['recipient_id'],
                         'contact_alex_1')
        self.assertTrue(guarded['objective_correct'])

    def test_lost_acknowledgment_does_not_retry_guarded_action(self):
        baseline = run_case('baseline_b', 'F06_lost_acknowledgment', 11)
        guarded = run_case('system_c', 'F06_lost_acknowledgment', 11)
        self.assertEqual(len(baseline['persisted_outbox']), 2)
        self.assertEqual(len(guarded['persisted_outbox']), 1)
        self.assertEqual(guarded['ledger_status'], 'UNCERTAIN')
        self.assertEqual(guarded['verification']['status'], 'CONFIRMED')
        self.assertTrue(guarded['safe_recovery'])

    def test_false_toast_is_not_reported_as_effect(self):
        baseline = run_case('baseline_a', 'F12_false_success_toast', 11)
        guarded = run_case('system_c', 'F12_false_success_toast', 11)
        self.assertTrue(baseline['false_success'])
        self.assertEqual(guarded['persisted_outbox'], [])
        self.assertEqual(guarded['verification']['status'], 'FAILED')
        self.assertFalse(guarded['declared_success'])

    def test_superseded_and_approval_mismatch_refuse(self):
        for fault in ('F10_superseded_instruction', 'F14_approval_plan_mismatch'):
            with self.subTest(fault=fault):
                guarded = run_case('system_c', fault, 11)
                self.assertEqual(guarded['persisted_outbox'], [])
                self.assertTrue(guarded['approval_integrity'])
                self.assertTrue(guarded['objective_correct'])

    def test_duplicate_delivery_is_rejected_by_action_state(self):
        guarded = run_case('system_c', 'F08_duplicate_task_delivery', 11)
        self.assertTrue(guarded['duplicate_delivery_rejected'])
        self.assertEqual(len(guarded['persisted_outbox']), 1)

    def test_runner_writes_raw_and_aggregate_records(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            aggregate = run_all([11], output)
            records = [json.loads(line) for line in (output / 'raw.jsonl').read_text(
                encoding='utf-8').splitlines()]
            self.assertEqual(aggregate['sample_size'], 24)
            self.assertEqual(len(records), 24)
            self.assertEqual(len((output / 'ground_truth.jsonl').read_text(
                encoding='utf-8').splitlines()), 8)
            self.assertEqual(aggregate['modes']['system_c']['n'], 8)
            self.assertEqual(len({(r['mode'], r['fault'], r['seed']) for r in records}), 24)


if __name__ == '__main__':
    unittest.main()
