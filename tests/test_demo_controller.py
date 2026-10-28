"""Guard and optional real desktop end-to-end checks for DemoController."""
try:
    import tkinter  # noqa: F401
except ImportError:
    import unittest as _u
    raise _u.SkipTest('tkinter unavailable on this host (headless Linux); Tk desktop fixture tests are NOT_RUN here')

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))
sys.path.insert(0, str(ROOT / "src"))
from demo_suite import DemoStore
from demo_controller import ControllerError, DemoController, canonical_hash
from resolve_core import Action, digest, uid


class ControllerKernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = DemoStore(self.path / "demo.sqlite3", self.path / "truth.sqlite3")
        self.controller = DemoController(
            self.path / "observation.json", self.path / "demo.sqlite3",
            self.path / "truth.sqlite3", self.path / "ledger.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_resolves_stable_contact_and_latest_hashed_report(self):
        contact, report = self.controller._resolve_targets()
        self.assertEqual(contact["id"], "alex-northwind")
        self.assertEqual(report["id"], "report-v4")
        self.assertEqual(report["version"], "v4")

    def test_stale_observation_refused(self):
        self.controller.observation_path.write_text(json.dumps({
            "running": True, "windows": [{"title": "RESOLVE Demo CRM"}],
            "observed_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        with self.assertRaisesRegex(ControllerError, "stale"):
            self.controller._observe()

    def test_verifier_requires_separate_truth_and_linked_followup(self):
        draft = self.store._create_draft("alex-northwind", "report-v4", "Approved local test")
        action = {"arguments": {"draft_id": draft["id"], "recipient_id": "alex-northwind", "report_id": "report-v4"}}
        self.assertEqual(self.controller._verify_send(action)["status"], "UNCERTAIN")
        message = self.store._send_draft(draft["id"])
        result = self.controller._verify_send(action)
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["message_id"], message["id"])
        with self.store.connect(self.store.truth_path) as truth:
            truth.execute("DELETE FROM effects WHERE operation='simulated_send'")
        self.assertEqual(self.controller._verify_send(action)["status"], "UNCERTAIN")

    def test_approval_digest_mismatch_never_dispatches(self):
        draft = self.store._create_draft("alex-northwind", "report-v4", "Local test")
        mission = self.controller.ledger.get_json("missions", "mission_demo")
        action = Action(
            uid("act"), mission["mission_id"], mission["intent_revision"],
            "tk_simulated_send", "alex-northwind", "desktop-controller", "tk-demo-outbox",
            None, {"draft_id": draft["id"], "draft_fingerprint": canonical_hash(draft),
                   "recipient_id": "alex-northwind", "report_id": "report-v4", "report_sha256": "unused"},
            None, {"outbox_draft_id": draft["id"]}, "NON_IDEMPOTENT", "consequential")
        self.controller.ledger.create_action(action)
        self.controller.ledger.transition(action.action_id, "BOUND")
        self.controller.ledger.transition(action.action_id, "NEEDS_APPROVAL")
        with self.assertRaisesRegex(ControllerError, "digest"):
            self.controller.send_approved(action.action_id, "wrong")
        self.assertEqual(self.controller.ledger.action(action.action_id)["status"], "NEEDS_APPROVAL")
        self.assertEqual(self.store.snapshot()["outbox"], [])


@unittest.skipUnless(os.getenv("RESOLVE_PHYSICAL_DEMO_TEST") == "1", "set RESOLVE_PHYSICAL_DEMO_TEST=1 for real Tk input")
class PhysicalDemoTests(unittest.TestCase):
    def test_three_window_guarded_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            db, truth, observation, ledger = (base / x for x in
                ("demo.sqlite3", "truth.sqlite3", "observation.json", "ledger.sqlite3"))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            suite = subprocess.Popen(
                [sys.executable, str(ROOT / "apps" / "demo_suite.py"),
                 "--db", str(db), "--truth", str(truth), "--observation", str(observation)],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not observation.exists():
                    if suite.poll() is not None:
                        self.fail("demo suite exited before publishing observation")
                    time.sleep(0.05)
                self.assertTrue(observation.exists())
                controller = DemoController(observation, db, truth, ledger, activate_timeout=8)
                start_obs = json.loads(observation.read_text(encoding="utf-8"))
                print("Initial demo foreground:", start_obs.get("foreground_hwnd"), [(w["title"], w["hwnd"]) for w in start_obs["windows"]])
                prepared = controller.prepare(inject_focus_fault=True)
                self.assertEqual(prepared["status"], "NEEDS_APPROVAL")
                self.assertTrue(prepared["fault_recovery"]["stale_target_refused"])
                self.assertEqual(DemoStore(db, truth).snapshot()["outbox"], [])
                with self.assertRaisesRegex(ControllerError, "digest"):
                    controller.send_approved(prepared["action_id"], "bad-digest")
                result = controller.send_approved(prepared["action_id"], prepared["approval_digest"])
                self.assertEqual(result["verification"]["status"], "CONFIRMED")
                self.assertEqual(result["action"]["status"], "CONFIRMED")
                self.assertEqual(len(DemoStore(db, truth).snapshot()["outbox"]), 1)
                print("Physical demo verified:", json.dumps(result["verification"], sort_keys=True))
            finally:
                suite.terminate()
                suite.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
