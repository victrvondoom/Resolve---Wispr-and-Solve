"""Guarded physical controller for the three-window RESOLVE demo suite.

All business changes are dispatched through real Tk widgets with Win32 input.
SQLite is read only here. Consequential send is a separate approved operation.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from resolve_core import Action, Ledger, digest, seed_demo, uid
import windows_adapter as win

DEFAULT_OBSERVATION = ROOT / "data" / "demo_suite_observation.json"
DEFAULT_DB = ROOT / "data" / "demo_suite.sqlite3"
DEFAULT_TRUTH = ROOT / "data" / "demo_suite_truth.sqlite3"
DEFAULT_LEDGER = ROOT / "data" / "resolve.sqlite3"


class ControllerError(RuntimeError):
    pass


class ForegroundRequired(ControllerError):
    pass


def canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DemoController:
    def __init__(
        self,
        observation_path: Path = DEFAULT_OBSERVATION,
        db_path: Path = DEFAULT_DB,
        truth_path: Path = DEFAULT_TRUTH,
        ledger_path: Path = DEFAULT_LEDGER,
        *,
        control_path: Path | None = None,
        activate_timeout: float = 6.0,
    ):
        self.observation_path = Path(observation_path)
        self.db_path = Path(db_path)
        self.truth_path = Path(truth_path)
        self.control_path = Path(control_path) if control_path else self.observation_path.with_name("demo_suite_control.json")
        self.ledger = Ledger(ledger_path)
        seed_demo(self.ledger)
        self.activate_timeout = activate_timeout
        self.activation_events: list[str] = []

    def _observe(self, max_age: float = 2.0) -> dict:
        try:
            observation = json.loads(self.observation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControllerError(f"demo observation unavailable: {exc}") from exc
        if observation.get("running") is False or not observation.get("windows"):
            raise ControllerError("demo suite is not running")
        observed = observation.get("observed_at")
        if not observed:
            raise ControllerError("demo observation has no timestamp")
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds()
        if age > max_age or age < -1:
            raise ControllerError(f"demo observation is stale by {age:.2f} seconds")
        return observation

    def _wait(self, predicate: Callable[[dict], bool], description: str, timeout: float = 4.0) -> dict:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                observation = self._observe()
                if predicate(observation):
                    return observation
            except ControllerError as exc:
                last_error = exc
            time.sleep(0.05)
        raise ControllerError(f"timed out waiting for {description}; last observation error: {last_error}")

    @staticmethod
    def _window(observation: dict, title: str) -> dict:
        matches = [row for row in observation["windows"] if row["title"] == title]
        if len(matches) != 1:
            raise ControllerError(f"expected one {title!r} window, found {len(matches)}")
        return matches[0]

    def _activate(self, title: str) -> tuple[dict, dict]:
        observation = self._observe()
        target = self._window(observation, title)
        hwnd = int(target["hwnd"])
        if os.name != "nt":
            raise ControllerError("physical demo requires Windows")
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE; respects normal Windows foreground restrictions.
        user32.SetForegroundWindow(hwnd)
        if win.list_windows()["foreground_hwnd"] != hwnd:
            # The request is handled by the demo process, which may foreground
            # one of its own known windows. It cannot mutate business state.
            self.control_path.parent.mkdir(parents=True, exist_ok=True)
            command = {"command_id": uid("focus"), "operation": "activate_window", "target": title}
            temporary = self.control_path.with_name(self.control_path.name + f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(command), encoding="utf-8")
            os.replace(temporary, self.control_path)
        deadline = time.monotonic() + self.activate_timeout
        fallback_at = time.monotonic() + 0.5
        fallback_used = False
        while time.monotonic() < deadline:
            live = win.list_windows()
            if live["foreground_hwnd"] == hwnd:
                row = next((x for x in live["windows"] if x["hwnd"] == hwnd), None)
                if row and row["title"] == title and not row["minimized"]:
                    return self._observe(), row
            if not fallback_used and time.monotonic() >= fallback_at:
                self._activate_with_titlebar_click(title, hwnd)
                fallback_used = True
            time.sleep(0.05)
        raise ForegroundRequired(f"Windows did not foreground {title!r}; focus it manually and retry the safe step")

    def _activate_with_titlebar_click(self, title: str, hwnd: int) -> None:
        """Visible activation of one known demo window under the cursor lock.

        A title-bar click is allowed only after Win32 confirms the target window
        is what a physical user would hit at that screen point. This is not used
        for arbitrary applications or protected/elevated desktops.
        """
        known = {"RESOLVE Demo CRM", "RESOLVE Demo Documents", "RESOLVE Demo Outbox", "RESOLVE Focus Fault"}
        if title not in known:
            raise ControllerError("activation click is limited to known demo windows")
        user32 = ctypes.windll.user32
        from ctypes import wintypes
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # no resize/move/activate, show
        if not user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags):
            raise ControllerError("could not expose the known demo window")
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
        live = win.list_windows()
        row = next((item for item in live["windows"] if item["hwnd"] == hwnd), None)
        if not row or row["title"] != title or row["minimized"]:
            raise ControllerError("demo window identity changed before activation")
        left, top, right, bottom = row["bounds"]
        x, y = left + min(110, max(20, (right - left) // 3)), top + 14
        if not (left <= x < right and top <= y < bottom):
            raise ControllerError("title bar point is outside current window bounds")
        hit = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
        if int(user32.GetAncestor(hit, 2) or 0) != hwnd:
            raise ControllerError("title bar point is occluded; activation click refused")
        mission = self.ledger.get_json("missions", "mission_demo")
        action = Action(
            uid("act"), mission["mission_id"], mission["intent_revision"],
            "activate_demo_window", title, "desktop-controller", "tk-demo-suite",
            {"hwnd": hwnd, "title": title}, {"point": [x, y]}, row["snapshot_id"],
            {"foreground_hwnd": hwnd}, "CONDITIONALLY_IDEMPOTENT", "low")
        self.ledger.create_action(action)
        self.activation_events.append(action.action_id)
        for state in ("BOUND", "AUTHORIZED", "READY"):
            self.ledger.transition(action.action_id, state)
        try:
            with win._desktop_turn():
                current = win.list_windows()
                current_row = next((item for item in current["windows"] if item["hwnd"] == hwnd), None)
                if not current_row or current_row["title"] != title or current_row["bounds"] != row["bounds"]:
                    raise ControllerError("demo window moved before activation")
                hit = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
                if int(user32.GetAncestor(hit, 2) or 0) != hwnd:
                    raise ControllerError("title bar point changed before activation")
                self.ledger.transition(action.action_id, "DISPATCHING")
                if not user32.SetCursorPos(x, y):
                    raise ControllerError("could not move pointer to verified title bar")
                hit = int(user32.WindowFromPoint(wintypes.POINT(x, y)) or 0)
                if int(user32.GetAncestor(hit, 2) or 0) != hwnd:
                    raise ControllerError("title bar occluded immediately before click")
                win._send(win._mouse(0x0002), win._mouse(0x0004))
        except Exception as exc:
            current = self.ledger.action(action.action_id)
            if current["status"] == "DISPATCHING":
                self.ledger.transition(action.action_id, "UNCERTAIN", {"activation_error": str(exc)})
            elif current["status"] == "READY":
                self.ledger.transition(action.action_id, "CANCELLED", {"activation_error": str(exc)})
            raise ControllerError(f"verified activation click failed: {exc}") from exc
        self.ledger.transition(action.action_id, "DISPATCHED")
        self.ledger.transition(action.action_id, "OBSERVING")
        if win.list_windows()["foreground_hwnd"] == hwnd:
            self.ledger.transition(action.action_id, "CONFIRMED", {"evidence_references": [f"HWND:{hwnd}"]})
        else:
            self.ledger.transition(action.action_id, "FAILED", {"activation_error": "foreground did not change"})

    def _click(self, title: str, target_name: str, entity_id: str | None = None) -> dict:
        observation, native = self._activate(title)
        window = self._window(observation, title)
        target = window["controls"].get(target_name)
        if target is None:
            raise ControllerError(f"widget {target_name!r} missing from {title!r}")
        if isinstance(target, list):
            matches = [candidate for candidate in target if candidate.get("entity_id") == entity_id]
            if len(matches) != 1:
                raise ControllerError(f"entity {entity_id!r} did not resolve uniquely in {target_name!r}")
            target = matches[0]
        center = target.get("center") or target.get("screen_center")
        if not center or len(center) != 2 or target.get("mapped") is False:
            raise ControllerError(f"target {target_name!r} is not visibly actionable")
        x, y = map(int, center)
        if int(window["hwnd"]) != native["hwnd"]:
            raise ControllerError("instrumented and Win32 HWNDs disagree")
        win.validate_target(native["hwnd"], native["snapshot_id"], x, y)
        return win.click(native["hwnd"], x, y, native["snapshot_id"])

    def _read_db(self) -> dict:
        if not self.db_path.exists():
            raise ControllerError("demo state database is missing")
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            def rows(sql: str, args: tuple = ()):
                return [dict(row) for row in db.execute(sql, args).fetchall()]
            return {
                "contacts": rows("SELECT * FROM contacts"),
                "reports": rows("SELECT * FROM reports"),
                "followups": rows("SELECT * FROM followups"),
                "selection": rows("SELECT * FROM selection"),
                "drafts": rows("SELECT * FROM drafts"),
                "outbox": rows("SELECT * FROM outbox"),
            }

    def _read_truth(self) -> list[dict]:
        if not self.truth_path.exists():
            raise ControllerError("separate demo truth database is missing")
        with contextlib.closing(sqlite3.connect(self.truth_path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            return [dict(row) for row in db.execute("SELECT * FROM effects ORDER BY sequence").fetchall()]

    def _resolve_targets(self) -> tuple[dict, dict]:
        state = self._read_db()
        contact_matches = [c for c in state["contacts"] if c["display_name"] == "Alex Morgan" and c["account"] == "Northwind"]
        if len(contact_matches) != 1 or contact_matches[0]["id"] != "alex-northwind":
            raise ControllerError("Northwind Alex identity is unresolved")
        reports = [r for r in state["reports"] if "Northwind" in r["title"]]
        if len(reports) < 2 or len({r["modified_at"] for r in reports}) != len(reports):
            raise ControllerError("report freshness policy is ambiguous")
        report = max(reports, key=lambda r: r["modified_at"])
        if report["id"] != "report-v4" or report["version"] != "v4":
            raise ControllerError("latest report is not the expected v4 fixture")
        if hashlib.sha256(report["content"].encode()).hexdigest() != report["sha256"]:
            raise ControllerError("latest report content hash is invalid")
        return contact_matches[0], report

    def _inject_and_recover_focus(self):
        before, native = self._activate("RESOLVE Demo Documents")
        docs = self._window(before, "RESOLVE Demo Documents")
        old_snapshot = native["snapshot_id"]
        x, y = docs["controls"]["use_report"]["center"]
        self._click("RESOLVE Demo Documents", "inject_focus_fault")
        opened = self._wait(lambda o: o["ui_selection"]["focus_popup_open"], "focus fault popup")
        popup = self._window(opened, "RESOLVE Focus Fault")
        if int(opened.get("foreground_hwnd") or 0) != int(popup["hwnd"]):
            # A modal Tk grab is real, but we need an OS foreground transition
            # to claim the temporal Win32 guard caught this injected fault.
            raise ControllerError("popup did not become OS foreground; focus fault unverified")
        try:
            win.validate_target(int(docs["hwnd"]), old_snapshot, int(x), int(y))
        except win.UnsupportedDesktop:
            guard_refused = True
        else:
            guard_refused = False
        if not guard_refused:
            raise ControllerError("stale document target passed the temporal guard")
        self._click("RESOLVE Focus Fault", "dismiss_popup")
        self._wait(lambda o: not o["ui_selection"]["focus_popup_open"], "popup dismissal")
        return {"fault": "foreground focus stolen", "stale_target_refused": True, "reobserved": True}

    def prepare(self, *, inject_focus_fault: bool = True) -> dict:
        """Physically prepare the CRM, document selection, and local outbox draft."""
        mission = self.ledger.get_json("missions", "mission_demo")
        if not mission or mission["status"] != "ACTIVE":
            raise ControllerError("demo mission is not active")
        contact, report = self._resolve_targets()
        before = self._read_db()
        followup = before["followups"][0]
        if followup["status"] == "contacted":
            raise ControllerError("demo follow-up was already completed; use isolated fixture data")

        if followup["status"] != "prepared":
            self._click("RESOLVE Demo CRM", "contact_rows", contact["id"])
            self._wait(lambda o: o["ui_selection"]["crm_contact_id"] == contact["id"], "CRM contact selection")
            self._click("RESOLVE Demo CRM", "update_followup")
            self._wait(lambda o: o["state"]["followups"][0]["status"] == "prepared", "follow-up update")

        self._click("RESOLVE Demo Documents", "report_rows", report["id"])
        self._wait(lambda o: o["ui_selection"]["docs_report_id"] == report["id"], "document selection")
        self._click("RESOLVE Demo Documents", "use_report")
        self._wait(lambda o: o["state"]["selection"]["report_id"] == report["id"], "persisted report selection")

        fault = self._inject_and_recover_focus() if inject_focus_fault else {"fault": "not injected"}
        self._click("RESOLVE Demo Outbox", "recipient_choices", contact["id"])
        self._wait(lambda o: o["ui_selection"]["outbox_recipient_id"] == contact["id"], "outbox recipient selection")
        self._click("RESOLVE Demo Outbox", "report_choices", report["id"])
        self._wait(lambda o: o["ui_selection"]["outbox_report_id"] == report["id"], "outbox report selection")
        self._click("RESOLVE Demo Outbox", "prepare_draft")
        prepared = self._wait(lambda o: bool(o["ui_selection"]["prepared_draft_id"]), "persisted draft")
        draft_id = prepared["ui_selection"]["prepared_draft_id"]
        matching = [d for d in self._read_db()["drafts"] if d["id"] == draft_id]
        if len(matching) != 1:
            raise ControllerError("prepared draft not found in persisted outbox state")
        draft = matching[0]
        if draft["recipient_id"] != contact["id"] or draft["report_id"] != report["id"]:
            raise ControllerError("draft target differs from resolved contact/report")
        if not any(e["operation"] == "draft_prepared" and e["target_id"] == draft_id for e in self._read_truth()):
            raise ControllerError("draft ground-truth event is missing")

        draft_fingerprint = canonical_hash(draft)
        action = Action(
            uid("act"), mission["mission_id"], mission["intent_revision"],
            "tk_simulated_send", contact["id"], "desktop-controller", "tk-demo-outbox",
            {"hwnd": self._window(prepared, "RESOLVE Demo Outbox")["hwnd"], "title": "RESOLVE Demo Outbox"},
            {"draft_id": draft_id, "draft_fingerprint": draft_fingerprint,
             "recipient_id": contact["id"], "report_id": report["id"], "report_sha256": report["sha256"]},
            prepared["snapshot_id"],
            {"outbox_draft_id": draft_id, "recipient_id": contact["id"], "report_id": report["id"],
             "followup_status": "contacted"},
            "NON_IDEMPOTENT", "consequential",
        )
        self.ledger.create_action(action)
        self.ledger.transition(action.action_id, "BOUND")
        self.ledger.transition(action.action_id, "NEEDS_APPROVAL")
        reviewed = self.ledger.action(action.action_id)
        return {
            "status": "NEEDS_APPROVAL", "action_id": action.action_id,
            "approval_digest": digest(reviewed), "mission_revision": mission["intent_revision"],
            "recipient": {"id": contact["id"], "name": contact["display_name"], "account": contact["account"], "email": contact["email"]},
            "attachment": {"id": report["id"], "title": report["title"], "version": report["version"], "sha256": report["sha256"]},
            "draft": draft, "fault_recovery": fault,
        }

    def _verify_send(self, action: dict) -> dict:
        draft_id = action["arguments"]["draft_id"]
        state = self._read_db()
        matching = [message for message in state["outbox"] if message["draft_id"] == draft_id]
        if not matching:
            return {"status": "UNCERTAIN", "reason": "no persisted outbox effect yet"}
        if len(matching) != 1:
            return {"status": "UNCERTAIN", "reason": "multiple outbox effects for one draft"}
        message = matching[0]
        if message["recipient_id"] != action["arguments"]["recipient_id"] or message["report_id"] != action["arguments"]["report_id"]:
            return {"status": "WRONG_TARGET", "message_id": message["id"]}
        followup = next((f for f in state["followups"] if f["contact_id"] == message["recipient_id"]), None)
        if not followup or followup["status"] != "contacted" or followup["last_message_id"] != message["id"]:
            return {"status": "UNCERTAIN", "reason": "follow-up effect is not linked to message", "message_id": message["id"]}
        truth = [e for e in self._read_truth() if e["operation"] == "simulated_send" and e["target_id"] == message["id"]]
        if len(truth) != 1 or json.loads(truth[0]["payload"]).get("draft_id") != draft_id:
            return {"status": "UNCERTAIN", "reason": "independent truth event is missing or mismatched", "message_id": message["id"]}
        return {
            "status": "CONFIRMED", "message_id": message["id"], "draft_id": draft_id,
            "recipient_id": message["recipient_id"], "report_id": message["report_id"],
            "followup_id": followup["id"], "followup_version": followup["version"],
            "truth_effect_id": truth[0]["effect_id"], "outbox_sha256": canonical_hash(message),
            "source": "read-only simulator SQLite plus separate truth ledger",
        }

    def send_approved(self, action_id: str, approval_digest: str) -> dict:
        """Dispatch one approved local simulator send; never automatically retry it."""
        action = self.ledger.action(action_id)
        if not action or action["operation"] != "tk_simulated_send":
            raise ControllerError("approved demo action was not found")
        if action["status"] != "NEEDS_APPROVAL":
            raise ControllerError(f"action is {action['status']}; duplicate dispatch refused")
        mission = self.ledger.get_json("missions", action["mission_id"])
        if not mission or mission["status"] != "ACTIVE" or mission["intent_revision"] != action["intent_revision"]:
            raise ControllerError("approved action was superseded or mission is inactive")
        if not approval_digest or digest(action) != approval_digest:
            raise ControllerError("approval digest does not match reviewed action")
        contact, report = self._resolve_targets()
        if contact["id"] != action["target_entity_id"] or report["id"] != action["arguments"]["report_id"] or report["sha256"] != action["arguments"]["report_sha256"]:
            raise ControllerError("recipient or report binding changed after approval")
        state = self._read_db()
        drafts = [d for d in state["drafts"] if d["id"] == action["arguments"]["draft_id"]]
        if len(drafts) != 1 or canonical_hash(drafts[0]) != action["arguments"]["draft_fingerprint"]:
            raise ControllerError("draft changed after approval")
        if any(m["draft_id"] == drafts[0]["id"] for m in state["outbox"]):
            raise ControllerError("draft already has an outbox effect; duplicate refused")
        if state["followups"][0]["status"] != "prepared" or state["selection"][0]["report_id"] != report["id"]:
            raise ControllerError("CRM or document precondition changed")
        observation = self._observe()
        if observation["ui_selection"]["prepared_draft_id"] != drafts[0]["id"]:
            raise ControllerError("visible draft differs from approved draft")
        if observation["ui_selection"]["outbox_recipient_id"] != contact["id"] or observation["ui_selection"]["outbox_report_id"] != report["id"]:
            raise ControllerError("visible recipient or attachment differs from approved action")

        # The checkbox is a reversible simulator UI step. The actual send click
        # occurs only after DISPATCHING has been durably written to the ledger.
        if not observation["ui_selection"]["local_approved"]:
            self._click("RESOLVE Demo Outbox", "approve_local")
            self._wait(lambda o: o["ui_selection"]["local_approved"], "local approval checkbox")
        current, native = self._activate("RESOLVE Demo Outbox")
        window = self._window(current, "RESOLVE Demo Outbox")
        x, y = window["controls"]["send_local"]["center"]
        win.validate_target(native["hwnd"], native["snapshot_id"], x, y)
        self.ledger.transition(action_id, "AUTHORIZED", {"approval_digest": approval_digest})
        self.ledger.transition(action_id, "READY")
        self.ledger.transition(action_id, "DISPATCHING")
        try:
            win.click(native["hwnd"], x, y, native["snapshot_id"])
        except Exception as exc:
            self.ledger.transition(action_id, "UNCERTAIN", {"dispatch_error": str(exc)})
            raise ControllerError(f"send outcome uncertain; no retry attempted: {exc}") from exc
        self.ledger.transition(action_id, "DISPATCHED")
        self.ledger.transition(action_id, "OBSERVING")
        deadline = time.monotonic() + 3
        result = self._verify_send(action)
        while result["status"] == "UNCERTAIN" and time.monotonic() < deadline:
            time.sleep(0.05)
            result = self._verify_send(action)
        self.ledger.transition(action_id, result["status"], {"verification": result, "evidence_references": [
            result.get("message_id"), result.get("truth_effect_id")]})
        return {"action": self.ledger.action(action_id), "verification": result}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Guarded physical RESOLVE demo controller")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--prepare", action="store_true")
    commands.add_argument("--send", action="store_true")
    parser.add_argument("--no-focus-fault", action="store_true")
    parser.add_argument("--action-id")
    parser.add_argument("--approval-digest")
    parser.add_argument("--observation", type=Path, default=DEFAULT_OBSERVATION)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--control", type=Path, default=None)
    args = parser.parse_args(argv)
    controller = DemoController(args.observation, args.db, args.truth, args.ledger,
                                control_path=args.control)
    try:
        if args.prepare:
            result = controller.prepare(inject_focus_fault=not args.no_focus_fault)
        else:
            if not args.action_id or not args.approval_digest:
                parser.error("--send requires --action-id and --approval-digest")
            result = controller.send_approved(args.action_id, args.approval_digest)
        print(json.dumps(result, sort_keys=True))
    except ControllerError as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
