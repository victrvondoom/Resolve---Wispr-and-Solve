"""Native Windows smoke gate. Exits 3 and prints WINDOWS_NATIVE_TESTS_NOT_RUN anywhere but an interactive Windows session."""
import os
import subprocess
import sys

if os.name != 'nt':
    print('WINDOWS_NATIVE_TESTS_NOT_RUN: host is not Windows')
    sys.exit(3)
import ctypes
if not ctypes.windll.user32.GetForegroundWindow():
    print('WINDOWS_NATIVE_TESTS_NOT_RUN: no interactive desktop (no foreground window)')
    sys.exit(3)
env = dict(os.environ, RESOLVE_GUI_TEST='1')
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.exit(subprocess.call([sys.executable, '-m', 'unittest', 'tests.test_windows_adapter', 'tests.test_api_gui', 'tests.test_uia_adapter', 'tests.test_w1_notepad', '-v'], cwd=root, env=env))
