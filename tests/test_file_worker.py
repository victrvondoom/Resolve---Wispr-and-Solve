"""W9: sandboxed file worker. Real filesystem effects in a temp sandbox, checked from disk."""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from file_worker import QUARANTINE, FileWorker, FileWorkerError


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict:
    """Independent disk oracle: rel path -> bytes for every regular file (no FileWorker code)."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            if not p.is_symlink():
                out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


class FileWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.sb = base / 'sandbox'
        self.outside = base / 'outside'
        self.sb.mkdir()
        self.outside.mkdir()
        (self.outside / 'secret.txt').write_bytes(b'do not touch')
        self.db = base / 'state' / 'fw.sqlite3'
        self.write('a/report.txt', b'quarterly report', mtime=1_000)
        self.write('b/report_copy.txt', b'quarterly report', mtime=2_000)
        self.write('c/zz.txt', b'quarterly report', mtime=2_000)
        self.write('a/notes.txt', b'notes one', mtime=1_500)
        self.write('b/notes.txt', b'notes two', mtime=1_500)  # same name, different content
        self.fw = FileWorker(self.sb, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data, mtime=None):
        p = self.sb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_inventory_lists_files_with_correct_hashes(self):
        inv = self.fw.inventory()
        self.assertEqual(inv['total_files'], 5)
        disk = snapshot(self.sb)
        self.assertEqual({f['path'] for f in inv['files']}, set(disk))
        for f in inv['files']:
            self.assertEqual(f['sha256'], sha(disk[f['path']]))
            self.assertEqual(f['size'], len(disk[f['path']]))
        self.assertEqual(inv['total_bytes'], sum(len(v) for v in disk.values()))

    def test_inventory_is_bounded(self):
        fw = FileWorker(self.sb, self.db, max_files=2)
        inv = fw.inventory()
        self.assertEqual(inv['total_files'], 2)
        self.assertTrue(inv['truncated'])
        self.assertEqual(len(inv['skipped']), 3)

    def test_duplicates_by_content_not_name(self):
        groups = self.fw.duplicates()['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['sha256'], sha(b'quarterly report'))
        self.assertEqual(groups[0]['paths'], ['a/report.txt', 'b/report_copy.txt', 'c/zz.txt'])
        flat = [p for g in groups for p in g['paths']]
        self.assertNotIn('a/notes.txt', flat)
        self.assertNotIn('b/notes.txt', flat)

    def test_plan_is_dry_run_and_keeps_oldest(self):
        before = snapshot(self.sb)
        plan = self.fw.plan_dedupe()
        self.assertTrue(plan['dry_run'])
        self.assertEqual(snapshot(self.sb), before)
        self.assertEqual([m['src'] for m in plan['moves']], ['b/report_copy.txt', 'c/zz.txt'])
        for m in plan['moves']:
            self.assertTrue(m['dst'].startswith(f"{QUARANTINE}/{plan['plan_id']}/"))

    def test_apply_moves_and_independently_verifies(self):
        plan = self.fw.plan_dedupe()
        res = self.fw.apply(plan['plan_id'])
        self.assertEqual(res['verification']['status'], 'CONFIRMED')
        self.assertEqual(len(res['moved']), 2)
        disk = snapshot(self.sb)
        self.assertEqual(disk['a/report.txt'], b'quarterly report')
        self.assertNotIn('b/report_copy.txt', disk)
        self.assertNotIn('c/zz.txt', disk)
        for m in plan['moves']:
            self.assertEqual(disk[m['dst']], b'quarterly report')
        with self.assertRaises(FileWorkerError):  # a plan is applied at most once
            self.fw.apply(plan['plan_id'])

    def test_file_modified_between_plan_and_apply_is_stale(self):
        plan = self.fw.plan_dedupe()
        self.write('c/zz.txt', b'edited after planning')
        before = snapshot(self.sb)
        res = self.fw.apply(plan['plan_id'])
        self.assertEqual(res['verification']['status'], 'STALE_PLAN')
        self.assertEqual(res['moved'], [])
        self.assertIsNone(res['op_id'])
        self.assertEqual(snapshot(self.sb), before)  # zero moves, including the unchanged source

    def test_undo_restores_exact_bytes(self):
        before = snapshot(self.sb)
        res = self.fw.apply(self.fw.plan_dedupe()['plan_id'])
        undo = self.fw.undo(res['op_id'])
        self.assertEqual(undo['verification']['status'], 'CONFIRMED')
        self.assertEqual(sorted(undo['restored']), ['b/report_copy.txt', 'c/zz.txt'])
        self.assertEqual(snapshot(self.sb), before)
        with self.assertRaises(FileWorkerError):
            self.fw.undo(res['op_id'])

    def test_undo_survives_restart(self):
        before = snapshot(self.sb)
        res = self.fw.apply(self.fw.plan_dedupe()['plan_id'])
        del self.fw
        fresh = FileWorker(self.sb, self.db)  # new instance, same journal
        undo = fresh.undo(res['op_id'])
        self.assertEqual(undo['verification']['status'], 'CONFIRMED')
        self.assertEqual(snapshot(self.sb), before)

    def test_undo_refuses_changed_quarantine_or_occupied_original(self):
        plan = self.fw.plan_dedupe()
        res = self.fw.apply(plan['plan_id'])
        self.write('b/report_copy.txt', b'new file at the original path')
        refused = self.fw.undo(res['op_id'])
        self.assertEqual(refused['verification']['status'], 'REFUSED')
        self.assertEqual(refused['verification']['reason'], 'ORIGINAL_OCCUPIED')
        self.assertEqual(refused['restored'], [])
        (self.sb / 'b/report_copy.txt').unlink()
        self.write(plan['moves'][1]['dst'], b'tampered in quarantine')
        refused = self.fw.undo(res['op_id'])
        self.assertEqual(refused['verification']['reason'], 'DESTINATION_CHANGED')
        self.assertFalse((self.sb / 'b/report_copy.txt').exists())  # nothing partially restored

    def test_traversal_absolute_and_symlink_escape_refused(self):
        with self.assertRaises(FileWorkerError) as ctx:
            self.fw.export_report('../outside/secret.txt', 'x.txt')
        self.assertEqual(ctx.exception.code, 'PATH_TRAVERSAL')
        with self.assertRaises(FileWorkerError) as ctx:
            self.fw.export_report(str(self.outside / 'secret.txt'), 'x.txt')
        self.assertEqual(ctx.exception.code, 'ABSOLUTE_PATH')
        with self.assertRaises(FileWorkerError):
            self.fw.write_note('../x.txt', 'escape')
        os.symlink(self.outside / 'secret.txt', self.sb / 'link.txt')
        os.symlink(self.outside, self.sb / 'linkdir')
        with self.assertRaises(FileWorkerError) as ctx:
            self.fw.export_report('link.txt', 'x.txt')
        self.assertEqual(ctx.exception.code, 'SYMLINK_REFUSED')
        with self.assertRaises(FileWorkerError) as ctx:
            self.fw.export_report('linkdir/secret.txt', 'x.txt')
        self.assertEqual(ctx.exception.code, 'SYMLINK_REFUSED')
        inv = self.fw.inventory()  # walk does not follow symlinks out of the sandbox
        self.assertNotIn('linkdir/secret.txt', {f['path'] for f in inv['files']})
        self.assertIn({'path': 'link.txt', 'reason': 'symlink_not_followed'}, inv['skipped'])
        self.assertEqual((self.outside / 'secret.txt').read_bytes(), b'do not touch')
        self.assertFalse((self.sb / 'exports' / 'x.txt').exists())

    def test_write_note_verifies_and_refuses_overwrite(self):
        res = self.fw.write_note('todo.txt', 'buy milk')
        self.assertEqual((self.sb / 'todo.txt').read_bytes(), b'buy milk')
        self.assertEqual(res['sha256'], sha(b'buy milk'))
        with self.assertRaises(FileWorkerError) as ctx:
            self.fw.write_note('todo.txt', 'overwrite attempt')
        self.assertEqual(ctx.exception.code, 'OVERWRITE_REFUSED')
        self.assertEqual((self.sb / 'todo.txt').read_bytes(), b'buy milk')
        with self.assertRaises(FileWorkerError):
            self.fw.write_note('sub/dir.txt', 'no separators')

    def test_export_report_copies_with_hash(self):
        res = self.fw.export_report('a/report.txt', 'q3.txt')
        self.assertEqual(res['path'], 'exports/q3.txt')
        self.assertEqual((self.sb / 'exports' / 'q3.txt').read_bytes(), b'quarterly report')
        self.assertEqual(res['sha256'], sha(b'quarterly report'))
        self.assertTrue((self.sb / 'a/report.txt').exists())
        with self.assertRaises(FileWorkerError):
            self.fw.export_report('a/report.txt', 'q3.txt')
        # exports are not inventoried as dedupe candidates
        self.assertEqual(len(self.fw.duplicates()['groups'][0]['paths']), 3)


if __name__ == '__main__':
    unittest.main()
