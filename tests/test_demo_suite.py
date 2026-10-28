"""Fixture and UI-event verification for the three-window local demo suite."""
try:
    import tkinter  # noqa: F401
except ImportError:
    import unittest as _u
    raise _u.SkipTest('tkinter unavailable on this host (headless Linux); Tk desktop fixture tests are NOT_RUN here')

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
from demo_suite import DemoStore, DemoSuite


class DemoStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.db = self.directory / "demo.db"
        self.truth = self.directory / "truth.db"
        self.store = DemoStore(self.db, self.truth)

    def tearDown(self):
        self.temp.cleanup()

    def test_fixture_is_ambiguous_and_versioned(self):
        state = self.store.snapshot()
        self.assertEqual(len(state["contacts"]), 2)
        self.assertEqual({c["display_name"] for c in state["contacts"]}, {"Alex Morgan"})
        self.assertEqual({c["account"] for c in state["contacts"]}, {"Northwind", "Contoso"})
        self.assertEqual([r["version"] for r in state["reports"]], ["v3", "v4"])
        self.assertLess(state["reports"][0]["modified_at"], state["reports"][1]["modified_at"])
        self.assertEqual(state["followups"][0]["status"], "pending")
        self.assertEqual(state["outbox"], [])
        self.assertEqual(self.store.truth_effects(), [])

    def test_fixture_persists_across_restart_without_reseeding_effects(self):
        initial = self.store.snapshot()
        reopened = DemoStore(self.db, self.truth)
        self.assertEqual(reopened.snapshot(), initial)
        self.assertEqual(reopened.truth_effects(), [])


@unittest.skipUnless(os.getenv("RESOLVE_DEMO_GUI_TEST") == "1", "set RESOLVE_DEMO_GUI_TEST=1 for Tk UI checks")
class DemoSuiteUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = DemoStore(self.directory / "demo.db", self.directory / "truth.db")
        self.observation_path = self.directory / "observation.json"
        self.suite = DemoSuite(self.store, self.observation_path)
        self.suite.root.update()

    def tearDown(self):
        self.suite.close()
        self.temp.cleanup()

    def test_three_windows_ui_events_and_ground_truth(self):
        suite = self.suite
        suite._observe()
        observation = json.loads(self.observation_path.read_text(encoding="utf-8"))
        self.assertEqual(len(observation["windows"]), 3)
        self.assertEqual({w["title"] for w in observation["windows"]}, {
            "RESOLVE Demo CRM", "RESOLVE Demo Documents", "RESOLVE Demo Outbox"})
        self.assertEqual(len(observation["windows"][0]["controls"]["contact_rows"]), 2)
        self.assertEqual(len(observation["windows"][1]["controls"]["report_rows"]), 2)
        for window, control_name in ((observation["windows"][0], "update_followup"),
                                     (observation["windows"][1], "inject_focus_fault"),
                                     (observation["windows"][2], "send_local")):
            x, y = window["controls"][control_name]["center"]
            left, top, right, bottom = window["bounds"]
            self.assertTrue(left <= x < right and top <= y < bottom)
        self.assertEqual(self.store.truth_effects(), [], "read-only observation changed ground truth")

        # Wrong same-name contact is refused for the Northwind follow-up.
        wrong_index = next(i for i, c in enumerate(suite.contacts) if c["id"] == "alex-contoso")
        suite.crm_list.selection_set(wrong_index)
        suite.crm_update.invoke()
        self.assertEqual(self.store.snapshot()["followups"][0]["status"], "pending")

        correct_index = next(i for i, c in enumerate(suite.contacts) if c["id"] == "alex-northwind")
        suite.crm_list.selection_clear(0, "end")
        suite.crm_list.selection_set(correct_index)
        suite.crm_update.invoke()
        self.assertEqual(self.store.snapshot()["followups"][0]["status"], "prepared")

        latest_index = next(i for i, r in enumerate(suite.reports) if r["id"] == "report-v4")
        suite.doc_list.selection_set(latest_index)
        suite.doc_select.invoke()
        self.assertEqual(self.store.snapshot()["selection"]["report_id"], "report-v4")

        suite.recipient_var.set("alex-northwind")
        suite.report_var.set("report-v4")
        suite.prepare_button.invoke()
        self.assertEqual(len(self.store.snapshot()["drafts"]), 1)
        suite.send_button.invoke()
        self.assertEqual(self.store.snapshot()["outbox"], [], "send bypassed local approval")
        suite.approve_check.invoke()
        suite.send_button.invoke()
        state = self.store.snapshot()
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(state["outbox"][0]["recipient_id"], "alex-northwind")
        self.assertEqual(state["outbox"][0]["report_id"], "report-v4")
        self.assertEqual(state["followups"][0]["last_message_id"], state["outbox"][0]["id"])
        self.assertEqual(state["followups"][0]["status"], "contacted")
        self.assertEqual([e["operation"] for e in self.store.truth_effects()], [
            "followup_prepared", "report_selected", "draft_prepared", "simulated_send"])

        suite.approved_var.set(True)
        suite.send_button.invoke()
        self.assertEqual(len(self.store.snapshot()["outbox"]), 1, "duplicate send changed outbox")

        suite.fault_button.invoke()
        suite.root.update()
        fault = suite.observation()
        self.assertTrue(fault["ui_selection"]["focus_popup_open"])
        self.assertEqual(len(fault["windows"]), 4)
        suite.popup_dismiss.invoke()
        suite.root.update()
        self.assertEqual(len(suite.observation()["windows"]), 3)


if __name__ == "__main__":
    unittest.main()
