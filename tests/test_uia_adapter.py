import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import uia_adapter


class UIAAdapterTests(unittest.TestCase):
    def test_limits_are_validated_before_dependency_import(self):
        with self.assertRaises(ValueError):
            uia_adapter.inspect_window(1, max_nodes=0)
        with self.assertRaises(ValueError):
            uia_adapter.inspect_window(1, max_depth=9)
        with self.assertRaises(ValueError):
            uia_adapter.inspect_window(1, timeout_seconds=0)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows')
    def test_controlled_tk_window_read_only_tree(self):
        if not uia_adapter.availability()['available']:
            self.skipTest('optional uiautomation package is not installed')
        fixture = (
            'import tkinter as tk\n'
            'root=tk.Tk()\n'
            'root.title("RESOLVE UIA test window")\n'
            'tk.Label(root,text="Fixture label").pack()\n'
            'tk.Button(root,text="Fixture button").pack()\n'
            'root.update()\n'
            'print(root.winfo_id(),flush=True)\n'
            'root.mainloop()\n'
        )
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        process = subprocess.Popen([sys.executable, '-u', '-c', fixture],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, creationflags=flags)
        answer = []
        reader = threading.Thread(target=lambda: answer.append(process.stdout.readline()),
                                  daemon=True)
        try:
            reader.start()
            reader.join(timeout=8)
            if reader.is_alive() or not answer or not answer[0].strip().isdigit():
                message = process.stderr.read() if process.poll() is not None else 'window did not start'
                self.skipTest(f'Tk fixture unavailable: {message}')
            hwnd = int(answer[0].strip())
            observed = uia_adapter.inspect_window(hwnd, max_depth=3,
                                                  max_nodes=16, max_children=8,
                                                  timeout_seconds=5)
            self.assertEqual(observed['status'], 'OBSERVED')
            self.assertEqual(observed['window_identity']['hwnd'], hwnd)
            self.assertGreaterEqual(observed['node_count'], 1)
            self.assertLessEqual(observed['node_count'], 16)
            self.assertEqual(len(observed['content_sha256']), 64)
            root = observed['nodes'][0]
            self.assertIn('control_type', root)
            self.assertIn('automation_id', root)
            self.assertIn('bounds', root)
            self.assertIn('enabled', root)
            self.assertIn('visible', root)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
