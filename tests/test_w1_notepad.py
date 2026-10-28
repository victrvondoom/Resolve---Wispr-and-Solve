"""W1 Notepad. Native test runs only on an interactive Windows desktop with RESOLVE_GUI_TEST=1.
The guard-logic tests use a FAKE driver (labelled) and prove only RESOLVE's bookkeeping, not Windows input."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import windows_adapter
from notepad_w1 import NotepadW1
from resolve_core import Ledger


class FakeDriver:
    """SIMULATED driver for bookkeeping tests. Writes the file itself to mimic Notepad's save."""
    UnsupportedDesktop = windows_adapter.UnsupportedDesktop

    def __init__(self, path_holder):
        self.focused = 101
        self.typed = ''
        self.path_holder = path_holder

    def list_windows(self):
        p = self.path_holder.get('path')
        return {'windows': [{'hwnd': 101, 'pid': 7, 'title': f'{p.name if p else "x"} - Notepad', 'snapshot_id': 's1'},
                            {'hwnd': 202, 'pid': 8, 'title': 'Popup', 'snapshot_id': 's2'}]}

    def focus_window(self, hwnd):
        self.focused = hwnd

    def type_text(self, hwnd, text, snapshot_id=None):
        if self.focused != hwnd:
            raise self.UnsupportedDesktop(f'foreground changed (expected HWND {hwnd}, current {self.focused}); no input sent')
        self.typed += text

    def press_chord(self, hwnd, keys, snapshot_id=None):
        if self.focused != hwnd:
            raise self.UnsupportedDesktop('foreground changed')
        self.path_holder['path'].write_text(self.typed, encoding='utf-8')

    def close_window(self, hwnd):
        pass


class W1BookkeepingWithFakeDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.holder = {}
        self.drv = FakeDriver(self.holder)
        self.w1 = NotepadW1(Ledger(Path(self.tmp.name) / 'l.sqlite3'), Path(self.tmp.name) / 'ws', self.drv,
                            launcher=lambda exe, p: self.holder.__setitem__('path', Path(p)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_is_confirmed_from_disk(self):
        r = self.w1.run('RESOLVE smoke test', 'a.txt')
        self.assertEqual(r['status'], 'CONFIRMED')
        self.assertEqual((Path(self.tmp.name) / 'ws' / 'a.txt').read_text(), 'RESOLVE smoke test')

    def test_focus_steal_refuses_and_records_failure(self):
        r = self.w1.run('RESOLVE smoke test', 'b.txt', before_type=lambda: self.drv.focus_window(202))
        self.assertEqual(r['status'], 'FAILED')
        self.assertIn('foreground changed', r['refused'])
        self.assertEqual(self.drv.typed, '')

    def test_workspace_escape_refused(self):
        with self.assertRaises(ValueError):
            self.w1.run('x', '../evil.txt')


@unittest.skipUnless(windows_adapter.IS_WINDOWS and os.getenv('RESOLVE_GUI_TEST') == '1',
                     'WINDOWS_NATIVE_TESTS_NOT_RUN: needs interactive Windows + RESOLVE_GUI_TEST=1 (moves real input)')
class W1NativeNotepad(unittest.TestCase):
    def test_real_notepad_type_save_verify(self):
        with tempfile.TemporaryDirectory() as d:
            w1 = NotepadW1(Ledger(Path(d) / 'l.sqlite3'), Path(d) / 'ws')
            r = w1.run('RESOLVE smoke test')
            self.assertEqual(r['status'], 'CONFIRMED', r)
            self.assertEqual(Path(r['path']).read_text(encoding='utf-8-sig').rstrip('\r\n'), 'RESOLVE smoke test')

    def test_real_focus_steal_blocks_typing(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            w1 = NotepadW1(Ledger(Path(d) / 'l.sqlite3'), Path(d) / 'ws')
            thief = []

            def steal():
                thief.append(subprocess.Popen(['notepad.exe']))
                import time
                time.sleep(1.5)
            try:
                r = w1.run('MUST NOT APPEAR', before_type=steal)
                self.assertIn(r['status'], {'FAILED', 'UNCERTAIN'})
                self.assertNotIn('MUST NOT APPEAR', Path(r['path']).read_text(encoding='utf-8-sig', errors='replace'))
            finally:
                for p in thief:
                    p.terminate()


if __name__ == '__main__':
    unittest.main()
