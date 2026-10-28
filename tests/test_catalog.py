"""G7 catalogue: honest statuses; every TESTED claim must point at a test that exists."""
import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from catalog import load_catalog, summary, validate


def node_exists(node_id: str) -> bool:
    """Parse the test file with ast and check the Class::test path exists (no imports executed)."""
    file_part, *names = node_id.split('::')
    path = ROOT / file_part
    if not path.is_file() or not names:
        return False
    scope = ast.parse(path.read_text(encoding='utf-8')).body
    for i, name in enumerate(names):
        last = i == len(names) - 1
        kinds = (ast.FunctionDef, ast.AsyncFunctionDef) if last else (ast.ClassDef,)
        match = next((n for n in scope if isinstance(n, kinds) and n.name == name), None)
        if match is None:
            return False
        scope = getattr(match, 'body', [])
    return names[-1].startswith('test')


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = load_catalog()

    def test_validates_with_zero_errors(self):
        self.assertEqual(validate(self.entries), [])

    def test_exact_44_computer_use_and_36_gtm(self):
        s = summary(self.entries)
        self.assertEqual(s['total'], 80)
        self.assertEqual(s['by_source'], {'agentic_gtm': 36, 'computer_use': 44})

    def test_names_match_registry(self):
        registry = {r['name'] for r in json.loads((ROOT / 'capabilities/registry.json').read_text())}
        for e in self.entries:
            self.assertIn(e.get('registry_name', e['name']), registry, e['id'])

    def test_every_tested_entry_points_at_an_existing_test(self):
        tested = [e for e in self.entries if e['status'] == 'TESTED']
        self.assertTrue(tested)
        for e in tested:
            self.assertTrue(node_exists(e['acceptance_test']), f"{e['id']}: {e['acceptance_test']}")

    def test_nothing_verified_and_no_code_reused(self):
        for e in self.entries:
            self.assertNotEqual(e['status'], 'VERIFIED', e['id'])
            self.assertIsNone(e['source_license_if_reused'], e['id'])

    def test_windows_dependent_entries_are_blocked_with_marker(self):
        for e in self.entries:
            if e['adapter'] == 'windows_adapter' and e['status'] == 'BLOCKED':
                self.assertIn('WINDOWS_NATIVE_TESTS_NOT_RUN', e['evidence'], e['id'])
            if e['adapter'] == 'windows_adapter':
                self.assertNotIn(e['status'], {'TESTED', 'WIRED', 'BACKEND_IMPLEMENTED'}, e['id'])

    def test_validator_rejects_false_claims(self):
        bad = [dict(e) for e in self.entries]
        bad[0] = dict(bad[0], status='TESTED', acceptance_test='tests/test_does_not_exist.py::T::test_x')
        bad[1] = dict(bad[1], status='BLOCKED', evidence='')
        bad[2] = dict(bad[2], status='DONE')
        bad[3] = dict(bad[3], id=bad[4]['id'])
        bad[5] = dict(bad[5], status='VERIFIED')
        errors = '\n'.join(validate(bad))
        for needle in ('acceptance_test file missing', 'BLOCKED requires evidence', 'invalid status',
                       'duplicate id', 'VERIFIED needs'):
            self.assertIn(needle, errors)
        self.assertTrue(validate(self.entries[:-1]))  # wrong count
        self.assertFalse(node_exists('tests/test_core.py::KernelTests::test_not_a_real_test'))


if __name__ == '__main__':
    unittest.main()
