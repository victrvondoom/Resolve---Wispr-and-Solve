"""G8: each IEEC mechanism, removed alone, lets its targeted fault through (ground-truth oracle)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'benchmarks'), str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'tests')]
from test_web_g3 import HAVE_PW

BAD = {'wrong_target_write', 'duplicate_effect', 'stale_or_unapproved_version_sent', 'false_success'}


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class AblationTests(unittest.TestCase):
    def check(self, variant):
        from ablations import one
        full, ablated = one(variant, False, 1), one(variant, True, 1)
        self.assertNotIn(full['verdict'], BAD, full)
        self.assertIn(ablated['verdict'], BAD, ablated)

    def test_temporal_guard(self):
        self.check('no_temporal_guard')

    def test_reconcile(self):
        self.check('no_reconcile_ledger')

    def test_verifier(self):
        self.check('no_independent_verifier')

    def test_approval_digest(self):
        self.check('no_approval_digest')

    def test_entity_binding_seed_where_first_match_is_wrong(self):
        from ablations import one
        self.assertEqual(one('no_entity_binding', True, 1)['verdict'], 'wrong_target_write')  # seed 1 lists Sales first
        self.assertEqual(one('no_entity_binding', False, 1)['verdict'], 'true_success')


if __name__ == '__main__':
    unittest.main()
