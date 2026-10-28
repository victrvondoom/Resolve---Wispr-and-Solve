"""Opt-in controlled GUI test of the full HTTP-to-Win32 input path."""
try:
    import tkinter  # noqa: F401
except ImportError:
    import unittest as _u
    raise _u.SkipTest('tkinter unavailable on this host (headless Linux); Tk desktop fixture tests are NOT_RUN here')

import ctypes
from concurrent.futures import ThreadPoolExecutor
import json
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt' and os.getenv('RESOLVE_GUI_TEST') == '1', 'set RESOLVE_GUI_TEST=1 for physical input')
class ApiGuiTest(unittest.TestCase):
    def test_guarded_http_input_reaches_controlled_window(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            env = os.environ.copy()
            env['RESOLVE_PORT'] = str(port)
            env['RESOLVE_DB'] = str(Path(temp) / 'gui.sqlite3')
            flags = subprocess.CREATE_NO_WINDOW
            server = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)

            def get(path):
                with urllib.request.urlopen(base + path, timeout=3) as response:
                    return json.loads(response.read())

            def post(path, payload, timeout=3):
                request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'X-Resolve-Token': token}, method='POST')
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read())

            root = None
            try:
                for _ in range(50):
                    try:
                        get('/api/health')
                        break
                    except Exception:
                        time.sleep(.1)
                token = get('/api/session')['token']
                root = tk.Tk()
                root.title('RESOLVE API Controlled Input Test')
                root.geometry('500x220+100+100')
                entry = tk.Entry(root, font=('Segoe UI', 14))
                entry.place(x=20, y=25, width=400, height=40)
                clicked = tk.IntVar(value=0)
                button = tk.Button(root, text='Click me', command=lambda: clicked.set(clicked.get() + 1))
                button.place(x=20, y=90, width=160, height=45)
                root.update()
                root.lift()
                root.attributes('-topmost', True)
                root.focus_force()
                user32 = ctypes.windll.user32
                user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                user32.GetAncestor.restype = ctypes.c_void_p
                hwnd = int(user32.GetAncestor(root.winfo_id(), 2))
                user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                user32.SetForegroundWindow(hwnd)
                print('Focus the RESOLVE API Controlled Input Test window within 10 seconds if needed.')
                for _ in range(200):
                    root.update()
                    if get('/api/windows')['foreground_hwnd'] == hwnd:
                        break
                    time.sleep(.05)
                if get('/api/windows')['foreground_hwnd'] != hwnd:
                    self.skipTest('interactive session did not allow foreground activation')
                time.sleep(.25)
                root.update()
                clicked.set(0)
                entry_x, entry_y = entry.winfo_rootx() + 25, entry.winfo_rooty() + 20
                button_x, button_y = button.winfo_rootx() + 45, button.winfo_rooty() + 22

                def payload(**extra):
                    observation = get('/api/windows')
                    window = next(w for w in observation['windows'] if w['hwnd'] == hwnd)
                    return {'mission_id': 'mission_demo', 'intent_revision': 1, 'hwnd': hwnd, 'snapshot_id': window['snapshot_id'], **extra}

                try:
                    moved = post('/api/windows/move', payload(x=entry_x, y=entry_y, smooth=False))
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode('utf-8', 'replace')
                    exc.close()
                    self.fail(f'guarded move refused: {detail}')
                self.assertEqual(moved['input_result']['status'], 'DISPATCHED')
                post('/api/windows/click', payload(x=entry_x, y=entry_y))
                for _ in range(20):
                    root.update()
                    if root.focus_get() == entry:
                        break
                    time.sleep(.01)
                if root.focus_get() != entry:
                    post('/api/windows/click', payload(x=entry_x, y=entry_y))
                    root.update()
                self.assertEqual(root.focus_get(), entry)
                typed = post('/api/windows/type', payload(text='Resolve API 42'))
                root.update()
                self.assertEqual(typed['status'], 'UNCERTAIN')  # Input dispatched; business effect has no independent verifier.
                self.assertEqual(entry.get(), 'Resolve API 42')
                post('/api/windows/click', payload(x=button_x, y=button_y))
                root.update()
                self.assertEqual(clicked.get(), 1)
                if importlib.util.find_spec('PIL') is not None:
                    capture = post('/api/windows/capture', payload())
                    self.assertGreater(capture['bytes'], 1000)
                    image_request = urllib.request.Request(base + '/api/evidence/' + capture['image_name'], headers={'X-Resolve-Token': token})
                    with urllib.request.urlopen(image_request, timeout=3) as image_response:
                        self.assertEqual(image_response.read(8), b'\x89PNG\r\n\x1a\n')
                if importlib.util.find_spec('uiautomation') is not None:
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        future = worker.submit(post, '/api/uia/inspect', payload(), 8)
                        while not future.done():
                            root.update()
                            time.sleep(.01)
                        tree = future.result()
                    self.assertEqual(tree['status'], 'OBSERVED')
                    self.assertGreaterEqual(tree['node_count'], 1)
                post('/api/emergency-stop', {})
                with self.assertRaises(urllib.error.HTTPError) as refusal:
                    post('/api/windows/type', payload(text='blocked'))
                self.assertEqual(refusal.exception.code, 409)
                refusal.exception.close()
                root.update()
                self.assertEqual(entry.get(), 'Resolve API 42')
            finally:
                if root is not None:
                    root.destroy()
                server.terminate()
                server.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
