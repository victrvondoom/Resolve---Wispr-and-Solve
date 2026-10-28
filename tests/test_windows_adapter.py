"""Windows adapter checks. Physical-input test is explicitly opt-in."""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import windows_adapter as adapter


@unittest.skipUnless(adapter.IS_WINDOWS, "Win32 desktop required")
class WindowsObservationTests(unittest.TestCase):
    def test_window_observations_have_temporal_identity(self):
        state = adapter.list_windows()
        self.assertIn("foreground_generation", state)
        self.assertIn("observed_at", state)
        self.assertEqual(state["semantic_uia"], "UNSUPPORTED")
        foreground = next((w for w in state["windows"] if w["foreground"]), None)
        if foreground is None:
            self.skipTest("no visible titled foreground window")
        self.assertEqual(len(foreground["bounds"]), 4)
        self.assertEqual(adapter.validate_snapshot(foreground["hwnd"], foreground["snapshot_id"])["pid"], foreground["pid"])

    def test_stale_snapshot_is_refused(self):
        foreground = next((w for w in adapter.list_windows()["windows"] if w["foreground"]), None)
        if foreground is None:
            self.skipTest("no visible titled foreground window")
        token = foreground["snapshot_id"]
        adapter._snapshots[token]["observed_at"] = time.time() - adapter.SNAPSHOT_MAX_AGE - 1
        with self.assertRaisesRegex(adapter.UnsupportedDesktop, "stale"):
            adapter.validate_snapshot(foreground["hwnd"], token)

    def test_emergency_stop_refuses_input(self):
        foreground = next((w for w in adapter.list_windows()["windows"] if w["foreground"]), None)
        if foreground is None:
            self.skipTest("no visible titled foreground window")
        adapter.stop()
        try:
            with self.assertRaisesRegex(adapter.UnsupportedDesktop, "emergency stop"):
                adapter.type_text(foreground["hwnd"], "never typed", foreground["snapshot_id"])
        finally:
            adapter.reset_stop()


@unittest.skipUnless(adapter.IS_WINDOWS and os.getenv("RESOLVE_GUI_TEST") == "1", "set RESOLVE_GUI_TEST=1 for physical input")
class WindowsPhysicalInputTest(unittest.TestCase):
    def test_controlled_tk_window_receives_real_input(self):
        import ctypes
        import tkinter as tk

        root = tk.Tk()
        root.title("RESOLVE Controlled Input Test")
        root.geometry("500x220+100+100")
        entry = tk.Entry(root, font=("Segoe UI", 14))
        entry.place(x=20, y=25, width=400, height=40)
        clicked = tk.IntVar(value=0)
        button = tk.Button(root, text="Click me", command=lambda: clicked.set(clicked.get() + 1))
        button.place(x=20, y=90, width=160, height=45)
        try:
            root.update()
            root.lift()
            root.attributes("-topmost", True)
            root.focus_force()
            root.update()
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = int(user32.GetAncestor(root.winfo_id(), 2))
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow(hwnd)
            print("Focus the RESOLVE Controlled Input Test window within 10 seconds if Windows does not foreground it automatically.")
            for _ in range(200):
                root.update()
                if adapter.list_windows()["foreground_hwnd"] == hwnd:
                    break
                time.sleep(0.05)
            if adapter.list_windows()["foreground_hwnd"] != hwnd:
                self.skipTest("interactive session did not allow foreground activation")
            # A user's click to focus the test window may itself hit the test
            # button. Let that activation settle, then establish ground truth.
            time.sleep(0.25)
            root.update()
            clicked.set(0)
            entry_x = entry.winfo_rootx() + 25
            entry_y = entry.winfo_rooty() + 20
            button_x = button.winfo_rootx() + 45
            button_y = button.winfo_rooty() + 22
            state = adapter.list_windows()
            snapshot = next(w["snapshot_id"] for w in state["windows"] if w["hwnd"] == hwnd)
            time.sleep(0.15)
            root.update()
            adapter.move_pointer(hwnd, entry_x, entry_y, snapshot, smooth=False)
            adapter.click(hwnd, entry_x, entry_y, snapshot)
            for _ in range(20):
                root.update()
                if root.focus_get() == entry:
                    break
                time.sleep(0.01)
            if root.focus_get() != entry:
                # Focusing an empty test field is idempotent; a second click can
                # be necessary after Windows finishes activating a new window.
                state = adapter.list_windows()
                snapshot = next(w["snapshot_id"] for w in state["windows"] if w["hwnd"] == hwnd)
                adapter.click(hwnd, entry_x, entry_y, snapshot)
                for _ in range(20):
                    root.update()
                    if root.focus_get() == entry:
                        break
                    time.sleep(0.01)
            self.assertEqual(root.focus_get(), entry, "real click did not focus the test entry")
            adapter.type_text(hwnd, "Resolve 42", snapshot)
            for _ in range(20):
                root.update()
                if entry.get() == "Resolve 42":
                    break
                time.sleep(0.01)
            self.assertEqual(entry.get(), "Resolve 42")
            self.assertEqual(clicked.get(), 0, "entry interaction unexpectedly activated button")
            state = adapter.list_windows()
            snapshot = next(w["snapshot_id"] for w in state["windows"] if w["hwnd"] == hwnd)
            adapter.click(hwnd, button_x, button_y, snapshot)
            for _ in range(20):
                root.update()
                time.sleep(0.01)
            self.assertEqual(clicked.get(), 1)
            try:
                import PIL  # noqa: F401 - screenshot evidence is optional
            except ImportError:
                print('Physical input verified; Pillow unavailable, so screenshot capture was skipped.')
                return
            state = adapter.list_windows()
            snapshot = next(w["snapshot_id"] for w in state["windows"] if w["hwnd"] == hwnd)
            capture = adapter.capture_window(hwnd, snapshot)
            self.assertGreater(capture["bytes"], 1000)
            self.assertEqual(len(capture["sha256"]), 64)
            print("Controlled GUI screenshot:", capture["path"], capture["sha256"])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
