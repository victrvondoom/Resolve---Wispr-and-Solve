"""Three real Tk demo applications with SQLite state and read-only observation.

CRM, Documents, and Outbox are separate visible windows in one process. Their
business state changes only from Tk widget callbacks. The JSON observation file
is instrumentation for a guarded external controller, not an action API.
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
import tkinter as tk
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "demo_suite.sqlite3"
DEFAULT_TRUTH = ROOT / "data" / "demo_suite_truth.sqlite3"
DEFAULT_OBSERVATION = ROOT / "data" / "demo_suite_observation.json"
DEFAULT_CONTROL = ROOT / "data" / "demo_suite_control.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rowdict(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


class DemoStore:
    def __init__(self, db_path: Path, truth_path: Path):
        self.db_path = Path(db_path)
        self.truth_path = Path(truth_path)
        for path in (self.db_path, self.truth_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(self.db_path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contacts(
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    account TEXT NOT NULL, email TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS reports(
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    version TEXT NOT NULL, modified_at TEXT NOT NULL,
                    content TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS followups(
                    id TEXT PRIMARY KEY, contact_id TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, last_message_id TEXT);
                CREATE TABLE IF NOT EXISTS selection(
                    id INTEGER PRIMARY KEY CHECK(id=1), report_id TEXT,
                    changed_at TEXT);
                CREATE TABLE IF NOT EXISTS drafts(
                    id TEXT PRIMARY KEY, recipient_id TEXT NOT NULL,
                    report_id TEXT NOT NULL, body TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox(
                    id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE,
                    recipient_id TEXT NOT NULL, report_id TEXT NOT NULL,
                    body TEXT NOT NULL, sent_at TEXT NOT NULL);
            """)
            if not db.execute("SELECT 1 FROM contacts LIMIT 1").fetchone():
                db.executemany("INSERT INTO contacts VALUES(?,?,?,?)", [
                    ("alex-northwind", "Alex Morgan", "Northwind", "alex.northwind@example.test"),
                    ("alex-contoso", "Alex Morgan", "Contoso", "alex.contoso@example.test"),
                ])
                reports = [
                    ("report-v3", "Q3 Financial Report - Northwind", "v3", "2026-10-20T09:00:00+00:00", "Northwind Q3 financial report. Prior draft. Revenue 80."),
                    ("report-v4", "Q3 Financial Report - Northwind (final)", "v4", "2026-10-26T15:30:00+00:00", "Northwind Q3 financial report. Final approved revision. Revenue 82."),
                ]
                db.executemany("INSERT INTO reports VALUES(?,?,?,?,?,?)", [
                    (*r, hashlib.sha256(r[4].encode()).hexdigest()) for r in reports
                ])
                db.execute("INSERT INTO followups VALUES(?,?,?,?,?,?)", (
                    "northwind-followup", "alex-northwind", "pending", 1, utc_now(), None))
                db.execute("INSERT INTO selection VALUES(1,NULL,NULL)")
        with self.connect(self.truth_path) as truth:
            truth.execute("PRAGMA journal_mode=WAL")
            truth.execute("""CREATE TABLE IF NOT EXISTS effects(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, effect_id TEXT UNIQUE,
                happened_at TEXT NOT NULL, operation TEXT NOT NULL,
                target_id TEXT NOT NULL, payload TEXT NOT NULL)""")

    @staticmethod
    @contextlib.contextmanager
    def connect(path: Path):
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def snapshot(self) -> dict:
        with self.connect(self.db_path) as db:
            return {
                "contacts": _rowdict(db.execute("SELECT * FROM contacts ORDER BY account")),
                "reports": _rowdict(db.execute("SELECT * FROM reports ORDER BY modified_at")),
                "followups": _rowdict(db.execute("SELECT * FROM followups ORDER BY id")),
                "selection": dict(db.execute("SELECT * FROM selection WHERE id=1").fetchone()),
                "drafts": _rowdict(db.execute("SELECT * FROM drafts ORDER BY created_at")),
                "outbox": _rowdict(db.execute("SELECT * FROM outbox ORDER BY sent_at")),
            }

    def truth_effects(self) -> list[dict]:
        with self.connect(self.truth_path) as db:
            return _rowdict(db.execute("SELECT * FROM effects ORDER BY sequence"))

    def _truth(self, operation: str, target_id: str, payload: dict) -> None:
        with self.connect(self.truth_path) as db:
            db.execute("INSERT INTO effects(effect_id,happened_at,operation,target_id,payload) VALUES(?,?,?,?,?)", (
                uuid.uuid4().hex, utc_now(), operation, target_id, json.dumps(payload, sort_keys=True)))

    def _update_followup(self, contact_id: str) -> dict:
        if contact_id != "alex-northwind":
            raise ValueError("this sample follow-up belongs to Northwind's Alex")
        with self.connect(self.db_path) as db:
            db.execute("UPDATE followups SET status='prepared',version=version+1,updated_at=? WHERE id='northwind-followup'", (utc_now(),))
            row = dict(db.execute("SELECT * FROM followups WHERE id='northwind-followup'").fetchone())
        self._truth("followup_prepared", row["id"], row)
        return row

    def _select_report(self, report_id: str) -> dict:
        with self.connect(self.db_path) as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if not row:
                raise ValueError("report does not exist")
            db.execute("UPDATE selection SET report_id=?,changed_at=? WHERE id=1", (report_id, utc_now()))
            selected = dict(row)
        self._truth("report_selected", report_id, {"version": selected["version"], "sha256": selected["sha256"]})
        return selected

    def _create_draft(self, recipient_id: str, report_id: str, body: str) -> dict:
        body = body.strip()
        if not body or len(body) > 2000:
            raise ValueError("draft body must contain 1 to 2000 characters")
        with self.connect(self.db_path) as db:
            if not db.execute("SELECT 1 FROM contacts WHERE id=?", (recipient_id,)).fetchone():
                raise ValueError("recipient does not exist")
            if not db.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone():
                raise ValueError("report does not exist")
            draft_id = "draft-" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO drafts VALUES(?,?,?,?,?)", (
                draft_id, recipient_id, report_id, body, utc_now()))
            row = dict(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())
        self._truth("draft_prepared", draft_id, row)
        return row

    def _send_draft(self, draft_id: str) -> dict:
        with self.connect(self.db_path) as db:
            draft = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise ValueError("draft does not exist")
            if db.execute("SELECT 1 FROM outbox WHERE draft_id=?", (draft_id,)).fetchone():
                raise ValueError("draft was already sent")
            message_id = "message-" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?)", (
                message_id, draft_id, draft["recipient_id"], draft["report_id"], draft["body"], utc_now()))
            if draft["recipient_id"] == "alex-northwind":
                db.execute("UPDATE followups SET status='contacted',version=version+1,updated_at=?,last_message_id=? WHERE id='northwind-followup'", (
                    utc_now(), message_id))
            row = dict(db.execute("SELECT * FROM outbox WHERE id=?", (message_id,)).fetchone())
        self._truth("simulated_send", message_id, row)
        return row


class DemoSuite:
    def __init__(self, store: DemoStore, observation_path: Path, control_path: Path | None = None):
        self.store = store
        self.observation_path = Path(observation_path)
        self.control_path = Path(control_path) if control_path else self.observation_path.with_name("demo_suite_control.json")
        self.observation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            previous = json.loads(self.control_path.read_text(encoding="utf-8"))
            self._last_control_id: str | None = previous.get("command_id")
        except (OSError, json.JSONDecodeError):
            self._last_control_id = None
        self._last_activation: dict | None = None
        self.root = tk.Tk()
        self.root.title("RESOLVE Demo CRM")
        self.root.geometry("455x485+30+120")
        self.root.configure(bg="#ecf1f5")
        self.docs = tk.Toplevel(self.root)
        self.docs.title("RESOLVE Demo Documents")
        self.docs.geometry("455x485+515+120")
        self.docs.configure(bg="#ecf1f5")
        self.outbox = tk.Toplevel(self.root)
        self.outbox.title("RESOLVE Demo Outbox")
        self.outbox.geometry("485x485+1000+120")
        self.outbox.configure(bg="#ecf1f5")
        self.popup: tk.Toplevel | None = None
        self.popup_dismiss: ttk.Button | None = None
        self.latest_draft_id: str | None = None
        self._build_crm()
        self._build_docs()
        self._build_outbox()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.docs.protocol("WM_DELETE_WINDOW", self.close)
        self.outbox.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(300, self._tick)

    def _heading(self, parent, title: str, subtitle: str):
        ttk.Label(parent, text=title, font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=17, pady=(16, 0))
        ttk.Label(parent, text=subtitle, wraplength=430).pack(anchor="w", padx=17, pady=(2, 12))

    def _build_crm(self):
        self._heading(self.root, "Client follow-up", "Two contacts share a display name. Check account and email before changing a record.")
        self.crm_list = tk.Listbox(self.root, height=4, font=("Segoe UI", 10), exportselection=False)
        self.crm_list.pack(fill="x", padx=17)
        state = self.store.snapshot()
        self.contacts = state["contacts"]
        for contact in self.contacts:
            self.crm_list.insert("end", f"{contact['display_name']}  |  {contact['account']}  |  {contact['email']}")
        self.crm_list.bind("<<ListboxSelect>>", lambda _: self._observe())
        self.crm_update = ttk.Button(self.root, text="Update selected follow-up", command=self._on_followup)
        self.crm_update.pack(anchor="w", padx=17, pady=(16, 8))
        self.crm_status = tk.StringVar()
        ttk.Label(self.root, textvariable=self.crm_status, wraplength=410).pack(anchor="w", padx=17)
        self._refresh_labels()

    def _build_docs(self):
        self._heading(self.docs, "Report versions", "Modified timestamps and content hashes distinguish v3 from v4.")
        self.doc_list = tk.Listbox(self.docs, height=5, font=("Segoe UI", 10), exportselection=False)
        self.doc_list.pack(fill="x", padx=17)
        state = self.store.snapshot()
        self.reports = state["reports"]
        for report in self.reports:
            self.doc_list.insert("end", f"{report['version']} | {report['title']} | {report['modified_at'][:10]}")
        self.doc_list.bind("<<ListboxSelect>>", lambda _: self._on_doc_highlight())
        self.report_preview = tk.Text(self.docs, height=4, width=48, font=("Segoe UI", 9), wrap="word")
        self.report_preview.pack(fill="x", padx=17, pady=(8, 0))
        self.report_preview.configure(state="disabled")
        self.doc_select = ttk.Button(self.docs, text="Use selected report", command=self._on_report)
        self.doc_select.pack(anchor="w", padx=17, pady=(16, 8))
        self.doc_status = tk.StringVar()
        ttk.Label(self.docs, textvariable=self.doc_status, wraplength=410).pack(anchor="w", padx=17)
        self.fault_button = ttk.Button(self.docs, text="Inject focus-stealing popup", command=self.inject_focus_fault)
        self.fault_button.pack(anchor="w", padx=17, pady=(36, 8))

    def _on_doc_highlight(self):
        selection = self.doc_list.curselection()
        if not selection:
            return
        report = self.reports[selection[0]]
        self.report_preview.configure(state="normal")
        self.report_preview.delete("1.0", "end")
        self.report_preview.insert("1.0", f"{report['content']}\nSHA-256: {report['sha256']}")
        self.report_preview.configure(state="disabled")
        self._observe()

    def _build_outbox(self):
        self._heading(self.outbox, "Local messaging simulator", "This outbox never sends external email. Review recipient, report, and draft before approving.")
        self.recipient_var = tk.StringVar(value="")
        self.report_var = tk.StringVar(value="")
        self.recipient_buttons = []
        self.report_buttons = []
        ttk.Label(self.outbox, text="Recipient").pack(anchor="w", padx=17)
        for contact in self.contacts:
            button = ttk.Radiobutton(self.outbox, text=f"{contact['display_name']} — {contact['account']}", value=contact["id"], variable=self.recipient_var, command=self._observe)
            button.pack(anchor="w", padx=17)
            self.recipient_buttons.append((contact["id"], button))
        ttk.Label(self.outbox, text="Attachment").pack(anchor="w", padx=17, pady=(10, 0))
        for report in self.reports:
            button = ttk.Radiobutton(self.outbox, text=f"{report['version']} — {report['title']}", value=report["id"], variable=self.report_var, command=self._observe)
            button.pack(anchor="w", padx=17)
            self.report_buttons.append((report["id"], button))
        ttk.Label(self.outbox, text="Message").pack(anchor="w", padx=17, pady=(10, 0))
        self.body = tk.Text(self.outbox, height=3, width=51, font=("Segoe UI", 10))
        self.body.pack(fill="x", padx=17)
        self.body.insert("1.0", "Following up with the latest Northwind report.")
        self.prepare_button = ttk.Button(self.outbox, text="Prepare draft", command=self._on_prepare)
        self.prepare_button.pack(anchor="w", padx=17, pady=(8, 2))
        self.approved_var = tk.BooleanVar(value=False)
        self.approve_check = ttk.Checkbutton(self.outbox, text="I approve this exact local draft", variable=self.approved_var, command=self._observe)
        self.approve_check.pack(anchor="w", padx=17)
        self.send_button = ttk.Button(self.outbox, text="Send approved draft to local outbox", command=self._on_send)
        self.send_button.pack(anchor="w", padx=17, pady=(4, 2))
        self.outbox_status = tk.StringVar(value="No draft prepared.")
        ttk.Label(self.outbox, textvariable=self.outbox_status, wraplength=450).pack(anchor="w", padx=17)

    def _on_followup(self):
        selection = self.crm_list.curselection()
        if not selection:
            self.crm_status.set("Select a contact first.")
            return
        try:
            row = self.store._update_followup(self.contacts[selection[0]]["id"])
            self.crm_status.set(f"Follow-up {row['status']} · version {row['version']}")
        except Exception as exc:
            self.crm_status.set(str(exc))
        self._observe()

    def _on_report(self):
        selection = self.doc_list.curselection()
        if not selection:
            self.doc_status.set("Select a report version first.")
            return
        try:
            report = self.store._select_report(self.reports[selection[0]]["id"])
            self.doc_status.set(f"Selected {report['version']} · SHA-256 {report['sha256'][:16]}…")
        except Exception as exc:
            self.doc_status.set(str(exc))
        self._observe()

    def _on_prepare(self):
        try:
            if not self.recipient_var.get() or not self.report_var.get():
                raise ValueError("Select recipient and attachment first.")
            draft = self.store._create_draft(self.recipient_var.get(), self.report_var.get(), self.body.get("1.0", "end"))
            self.latest_draft_id = draft["id"]
            self.approved_var.set(False)
            self.outbox_status.set(f"Draft {draft['id']} ready: {draft['recipient_id']} / {draft['report_id']}. Approval required.")
        except Exception as exc:
            self.outbox_status.set(str(exc))
        self._observe()

    def _on_send(self):
        try:
            if not self.latest_draft_id:
                raise ValueError("Prepare a draft first.")
            if not self.approved_var.get():
                raise ValueError("Approve the exact local draft first.")
            message = self.store._send_draft(self.latest_draft_id)
            self.outbox_status.set(f"Local outbox message {message['id']} recorded for {message['recipient_id']} with {message['report_id']}.")
            self.approved_var.set(False)
        except Exception as exc:
            self.outbox_status.set(str(exc))
        self._refresh_labels()
        self._observe()

    def inject_focus_fault(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.lift()
            return
        self.popup = tk.Toplevel(self.docs)
        self.popup.title("RESOLVE Focus Fault")
        self.popup.geometry("330x150+620+260")
        self.popup.attributes("-topmost", True)
        ttk.Label(self.popup, text="Focus was stolen by this popup.", font=("Segoe UI", 11)).pack(padx=17, pady=(20, 12))
        self.popup_dismiss = ttk.Button(self.popup, text="Dismiss popup", command=self.dismiss_focus_fault)
        self.popup_dismiss.pack(padx=17)
        self.popup.grab_set()
        self.popup.focus_force()
        self._observe()

    def dismiss_focus_fault(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.grab_release()
            self.popup.destroy()
        self.popup = None
        self.popup_dismiss = None
        self._observe()

    def _refresh_labels(self):
        record = self.store.snapshot()["followups"][0]
        self.crm_status.set(f"Northwind follow-up: {record['status']} · version {record['version']}")

    @staticmethod
    def _hwnd(window: tk.Misc) -> int:
        if os.name != "nt":
            return int(window.winfo_id())
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        return int(user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id())

    @staticmethod
    def _target(widget: tk.Misc) -> dict:
        widget.update_idletasks()
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        width, height = widget.winfo_width(), widget.winfo_height()
        return {"bounds": [x, y, x + width, y + height], "center": [x + width // 2, y + height // 2], "mapped": bool(widget.winfo_ismapped())}

    def _list_rows(self, widget: tk.Listbox, records: list[dict]) -> list[dict]:
        result = []
        for index, record in enumerate(records):
            bbox = widget.bbox(index)
            if bbox:
                x, y, width, height = bbox
                result.append({"entity_id": record["id"], "screen_center": [widget.winfo_rootx() + x + min(width, 150) // 2, widget.winfo_rooty() + y + height // 2]})
        return result

    def _window(self, window: tk.Misc, controls: dict) -> dict:
        if not window.winfo_exists():
            return {}
        window.update_idletasks()
        return {"title": window.title(), "hwnd": self._hwnd(window), "bounds": [
            window.winfo_rootx(), window.winfo_rooty(),
            window.winfo_rootx() + window.winfo_width(), window.winfo_rooty() + window.winfo_height()],
            "controls": controls}

    def _consume_activation(self):
        """Accept only focus requests for known demo windows; no app data writes."""
        try:
            if not self.control_path.exists() or self.control_path.stat().st_size > 1024:
                return
            command = json.loads(self.control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        command_id = command.get("command_id")
        if not isinstance(command_id, str) or not command_id or command_id == self._last_control_id:
            return
        self._last_control_id = command_id
        known = {
            "RESOLVE Demo CRM": self.root,
            "RESOLVE Demo Documents": self.docs,
            "RESOLVE Demo Outbox": self.outbox,
        }
        if self.popup is not None and self.popup.winfo_exists():
            known["RESOLVE Focus Fault"] = self.popup
        target = command.get("target")
        if command.get("operation") != "activate_window" or target not in known:
            self._last_activation = {"command_id": command_id, "status": "REJECTED", "target": target}
            return
        window = known[target]
        window.deiconify()
        window.lift()
        window.focus_force()
        if os.name == "nt":
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow(self._hwnd(window))
        self._last_activation = {"command_id": command_id, "status": "REQUESTED", "target": target}

    def observation(self) -> dict:
        self.root.update_idletasks()
        state = self.store.snapshot()
        windows = [
            self._window(self.root, {
                "contact_rows": self._list_rows(self.crm_list, self.contacts),
                "update_followup": self._target(self.crm_update),
            }),
            self._window(self.docs, {
                "report_rows": self._list_rows(self.doc_list, self.reports),
                "use_report": self._target(self.doc_select),
                "inject_focus_fault": self._target(self.fault_button),
            }),
            self._window(self.outbox, {
                "recipient_choices": [{"entity_id": key, **self._target(widget)} for key, widget in self.recipient_buttons],
                "report_choices": [{"entity_id": key, **self._target(widget)} for key, widget in self.report_buttons],
                "message_body": self._target(self.body),
                "prepare_draft": self._target(self.prepare_button),
                "approve_local": self._target(self.approve_check),
                "send_local": self._target(self.send_button),
            }),
        ]
        if self.popup is not None and self.popup.winfo_exists():
            windows.append(self._window(self.popup, {"dismiss_popup": self._target(self.popup_dismiss)}))
        foreground = None
        if os.name == "nt":
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            foreground = int(user32.GetForegroundWindow() or 0)
        return {
            "schema_version": 1, "snapshot_id": uuid.uuid4().hex,
            "observed_at": utc_now(), "process_id": os.getpid(),
            "foreground_hwnd": foreground,
            "last_activation": self._last_activation,
            "provenance": "Tk widget geometry and read-only SQLite state",
            "windows": windows,
            "ui_selection": {
                "crm_contact_id": self.contacts[self.crm_list.curselection()[0]]["id"] if self.crm_list.curselection() else None,
                "docs_report_id": self.reports[self.doc_list.curselection()[0]]["id"] if self.doc_list.curselection() else None,
                "outbox_recipient_id": self.recipient_var.get() or None,
                "outbox_report_id": self.report_var.get() or None,
                "prepared_draft_id": self.latest_draft_id,
                "local_approved": self.approved_var.get(),
                "focus_popup_open": self.popup is not None and self.popup.winfo_exists(),
            },
            "state": state,
        }

    def _observe(self):
        if not self.root.winfo_exists():
            return
        payload = self.observation()
        temporary = self.observation_path.with_name(self.observation_path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.observation_path)

    def _tick(self):
        try:
            self._consume_activation()
            self._observe()
        except Exception as exc:
            print(f"Demo observation error: {exc}", file=sys.stderr)
        if self.root.winfo_exists():
            self.root.after(100, self._tick)

    def close(self):
        payload = {"schema_version": 1, "running": False, "observed_at": utc_now(), "windows": []}
        temporary = self.observation_path.with_name(self.observation_path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.observation_path)
        self.root.destroy()

    def run(self):
        self._observe()
        self.root.mainloop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the three visible RESOLVE local demo applications")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--observation", type=Path, default=DEFAULT_OBSERVATION)
    parser.add_argument("--control", type=Path, default=None,
                        help="activation command path (defaults beside observation file)")
    args = parser.parse_args(argv)
    suite = DemoSuite(DemoStore(args.db, args.truth), args.observation, args.control)
    print(f"RESOLVE demo CRM/Documents/Outbox ready; observation={args.observation}", flush=True)
    suite.run()


if __name__ == "__main__":
    main()
