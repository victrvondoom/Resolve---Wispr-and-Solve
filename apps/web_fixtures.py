"""Three separate stateful web fixture apps: CRM, report store, outbox.

Each app has its own HTTP server and SQLite file. Every real write also lands in
that app's `ground_truth` table; RESOLVE's verifier never reads that table, only
the tests' oracle does. Fault endpoints exist only when test_mode=True.
The outbox is a SIMULATED DELIVERY fixture: nothing leaves this machine.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    return db


TRUTH = "CREATE TABLE IF NOT EXISTS ground_truth(seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, detail TEXT)"


def truth(db: sqlite3.Connection, kind: str, detail: dict) -> None:
    db.execute('INSERT INTO ground_truth(at,kind,detail) VALUES(?,?,?)', (now(), kind, json.dumps(detail, sort_keys=True)))


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the drop_ack fault closes sockets on purpose; broken pipes are expected, not errors


class App:
    """Base: a ThreadingHTTPServer on an ephemeral loopback port."""
    name = 'app'

    def __init__(self, root: Path, test_mode: bool, seed: int = 0):
        self.seed = seed
        self.root = Path(root)
        self.test_mode = test_mode
        self.faults: dict[str, object] = {}
        self.lock = threading.Lock()
        self.db_path = self.root / f'{self.name}.sqlite3'
        self.init_db()
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype='application/json'):
                raw = body if isinstance(body, bytes) else (json.dumps(body) if ctype == 'application/json' else body).encode()
                self.send_response(status)
                self.send_header('Content-Type', ctype + '; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urlparse(self.path)
                try:
                    app.get(self, u.path, parse_qs(u.query))
                except Exception as e:  # surfaced, never silent
                    self._send(500, {'error': str(e)})

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}') if n else {}
                if u.path.startswith('/_fault/'):
                    if not app.test_mode:
                        return self._send(403, {'error': 'fault injection disabled outside test mode'})
                    app.faults[u.path[8:]] = body.get('value', True)
                    return self._send(200, {'faults': app.faults})
                try:
                    app.post(self, u.path, body)
                except Exception as e:
                    self._send(500, {'error': str(e)})

        self.server = _QuietServer(('127.0.0.1', 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def init_db(self): ...
    def get(self, h, path, q): h._send(404, {'error': 'not found'})
    def post(self, h, path, body): h._send(404, {'error': 'not found'})


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:15px system-ui;margin:24px;max-width:960px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:6px;text-align:left}}
.modal{{position:fixed;inset:0;background:#0008;display:flex;align-items:center;justify-content:center}}.modal div{{background:#fff;padding:24px}}
.banner{{background:#fde68a;padding:6px}}</style></head><body><h1>{title}</h1>{body}</body></html>"""


class CrmApp(App):
    name = 'crm'

    def init_db(self):
        with connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY, name TEXT, email TEXT, org TEXT, dept TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, contact_id TEXT REFERENCES contacts(id), title TEXT, op_id TEXT UNIQUE, created_at TEXT)')
            db.execute(TRUTH)
            if not db.execute('SELECT 1 FROM contacts').fetchone():
                rows = [('C-104', 'Jordan Lee', 'jordan.lee@ops.example.test', 'Northwind', 'Operations'),
                        ('C-208', 'Jordan Lee', 'jordan.lee@sales.example.test', 'Contoso', 'Sales'),
                        ('C-310', 'Priya Nair', 'priya@ops.example.test', 'Northwind', 'Operations')]
                if self.seed % 2:  # seed controls row order so first-match heuristics are not secretly always right or wrong
                    rows[0], rows[1] = rows[1], rows[0]
                db.executemany('INSERT INTO contacts VALUES(?,?,?,?,?)', rows)

    def get(self, h, path, q):
        if path == '/api/contacts':
            if self.faults.get('delay_ms'):
                time.sleep(int(self.faults['delay_ms']) / 1000)
            with connect(self.db_path) as db:
                return h._send(200, [dict(r) for r in db.execute('SELECT * FROM contacts ORDER BY rowid')])
        if path == '/api/tasks':
            with connect(self.db_path) as db:
                return h._send(200, [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created_at')])
        if path == '/':
            popup = '<div class="modal" role="dialog" aria-label="Survey"><div><p>Quick survey?</p><button id="dismiss">Dismiss</button></div></div>' if self.faults.get('popup') else ''
            form = """<h2>Create follow-up task</h2>
<form id="f"><label>Contact ID <input name="contact_id" aria-label="Contact ID" required></label>
<label>Task title <input name="title" aria-label="Task title" required></label><input type="hidden" name="op_id">
<button type="submit">Create task</button></form><div id="toast" role="status"></div>
"""
            table = """<label>Search contacts <input id="q" aria-label="Search contacts"></label>
<table aria-label="Contacts"><thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Org</th><th>Dept</th></tr></thead><tbody id="rows"><tr><td colspan="5">Loading…</td></tr></tbody></table>
"""
            if self.faults.get('layout_shift'):
                # simulated app update: the form moves above the table into a disclosure widget, the
                # title label and submit text are renamed; input `name` attributes stay the same
                form = """<details open class="quick-actions"><summary>Quick actions</summary><section aria-label="Follow-ups">
<form id="f"><div><label>Contact ID <input name="contact_id" aria-label="Contact ID" required></label></div>
<div><label>Follow-up title <input name="title" aria-label="Follow-up title" required></label></div><input type="hidden" name="op_id">
<div><button type="submit">Save follow-up</button></div></form><div id="toast" role="status"></div></section></details>
"""
                main = form + table
            else:
                main = table + form
            body = popup + main + """<h2>Tasks</h2><table aria-label="Tasks"><tbody id="tasks"></tbody></table>
<script>
const dm=document.getElementById('dismiss'); if(dm) dm.onclick=()=>document.querySelector('.modal').remove();
async function load(){const c=await (await fetch('/api/contacts')).json(); rows.innerHTML=c.map(x=>`<tr data-testid="contact-row" data-contact-id="${x.id}"><td>${x.id}</td><td>${x.name}</td><td>${x.email}</td><td>${x.org}</td><td>${x.dept}</td></tr>`).join('');
 const t=await (await fetch('/api/tasks')).json(); tasks.innerHTML=t.map(x=>`<tr data-testid="task-row" data-task-id="${x.id}"><td>${x.id}</td><td>${x.contact_id}</td><td>${x.title}</td></tr>`).join('');}
load();
f.onsubmit=async e=>{e.preventDefault(); const d=Object.fromEntries(new FormData(f)); const r=await fetch('/api/tasks',{method:'POST',body:JSON.stringify(d)}); const j=await r.json();
 toast.textContent=r.ok?('Task created: '+j.id):('Error: '+j.error); toast.dataset.taskId=j.id||''; load();};
</script>"""
            return h._send(200, PAGE.format(title='Local CRM', body=body), 'text/html')
        h._send(404, {'error': 'not found'})

    def post(self, h, path, body):
        if path != '/api/tasks':
            return h._send(404, {'error': 'not found'})
        with self.lock, connect(self.db_path) as db:
            if not db.execute('SELECT 1 FROM contacts WHERE id=?', (body.get('contact_id'),)).fetchone():
                return h._send(400, {'error': 'unknown contact'})
            op = body.get('op_id') or f"auto-{uuid.uuid4().hex}"
            prior = db.execute('SELECT id FROM tasks WHERE op_id=?', (op,)).fetchone()
            if prior:
                return h._send(200, {'id': prior['id'], 'duplicate': True})
            tid = 'T-' + uuid.uuid4().hex[:8]
            if self.faults.get('misleading_toast'):  # reports success, writes nothing
                return h._send(200, {'id': tid})
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (tid, body['contact_id'], body.get('title', ''), op, now()))
            truth(db, 'task_created', {'task': tid, 'contact': body['contact_id'], 'op_id': op})
            db.commit()  # acknowledge only after the write is durable (replying inside the txn raced readers)
        h._send(200, {'id': tid})


class DocsApp(App):
    name = 'docs'

    def init_db(self):
        (self.root / 'files').mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, title TEXT, version TEXT, contact_id TEXT, modified_at TEXT, path TEXT, sha256 TEXT)')
            db.execute(TRUTH)
            if not db.execute('SELECT 1 FROM reports').fetchone():
                base = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
                for i, (rid, title, ver, cid, days) in enumerate([
                        ('R-17', 'Q3 Operations Report', 'v3', 'C-104', 0),
                        ('R-22', 'Q3 Operations Report (final)', 'v4', 'C-104', 6),
                        ('R-31', 'Q3 Sales Report', 'v2', 'C-208', 3)]):
                    p = self.root / 'files' / f'{rid}.txt'
                    data = f'{title} {ver}\ncontact={cid}\n'.encode()
                    p.write_bytes(data)
                    db.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?)', (rid, title, ver, cid, (base + timedelta(days=days)).isoformat(), str(p), sha256(data)))

    def get(self, h, path, q):
        with connect(self.db_path) as db:
            if path == '/api/reports':
                return h._send(200, [dict(r) for r in db.execute('SELECT id,title,version,contact_id,modified_at,sha256 FROM reports ORDER BY id')])
            if path.startswith('/files/'):
                r = db.execute('SELECT path FROM reports WHERE id=?', (path[7:],)).fetchone()
                return h._send(200, Path(r['path']).read_bytes(), 'text/plain') if r else h._send(404, {'error': 'no such report'})
            if path == '/':
                body = """<table aria-label="Reports"><thead><tr><th>ID</th><th>Title</th><th>Version</th><th>Contact</th><th>Modified</th><th>SHA-256</th></tr></thead><tbody id="rows"></tbody></table>
<script>fetch('/api/reports').then(r=>r.json()).then(c=>{rows.innerHTML=c.map(x=>`<tr data-testid="report-row" data-report-id="${x.id}" data-sha256="${x.sha256}" data-modified="${x.modified_at}"><td>${x.id}</td><td>${x.title}</td><td>${x.version}</td><td>${x.contact_id}</td><td>${x.modified_at}</td><td>${x.sha256.slice(0,12)}</td></tr>`).join('')})</script>"""
                return h._send(200, PAGE.format(title='Report Store', body=body), 'text/html')
        h._send(404, {'error': 'not found'})

    def post(self, h, path, body):
        if path == '/_edit' and self.test_mode:  # fixture-only: simulate edit after review
            with self.lock, connect(self.db_path) as db:
                r = db.execute('SELECT * FROM reports WHERE id=?', (body['id'],)).fetchone()
                data = Path(r['path']).read_bytes() + b'EDITED AFTER REVIEW\n'
                Path(r['path']).write_bytes(data)
                db.execute('UPDATE reports SET sha256=?, modified_at=? WHERE id=?', (sha256(data), now(), r['id']))
                truth(db, 'report_edited', {'id': r['id'], 'sha256': sha256(data)})
                db.commit()
            return h._send(200, {'ok': True})
        h._send(404, {'error': 'not found'})

    def edit(self, report_id: str) -> None:
        """Out-of-band edit used by tests as the 'file modified after review' fault."""
        with self.lock, connect(self.db_path) as db:
            r = db.execute('SELECT * FROM reports WHERE id=?', (report_id,)).fetchone()
            data = Path(r['path']).read_bytes() + b'EDITED AFTER REVIEW\n'
            Path(r['path']).write_bytes(data)
            db.execute('UPDATE reports SET sha256=?, modified_at=? WHERE id=?', (sha256(data), now(), report_id))
            truth(db, 'report_edited', {'id': report_id, 'sha256': sha256(data)})


class OutboxApp(App):
    name = 'outbox'

    def init_db(self):
        with connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, op_id TEXT UNIQUE, to_addr TEXT, attachment_id TEXT, attachment_sha256 TEXT, body TEXT, sent_at TEXT)')
            db.execute(TRUTH)

    def get(self, h, path, q):
        if path == '/api/messages':
            with connect(self.db_path) as db:
                op = (q.get('op_id') or [None])[0]
                rows = db.execute('SELECT * FROM messages WHERE (? IS NULL OR op_id=?) ORDER BY sent_at', (op, op)).fetchall()
            return h._send(200, [dict(r) for r in rows])
        if path == '/':
            body = """<div class="banner" role="note">SIMULATED DELIVERY — nothing leaves this machine</div>
<form id="f"><label>To <input name="to_addr" aria-label="To"></label><label>Attachment ID <input name="attachment_id" aria-label="Attachment ID"></label>
<label>Attachment SHA-256 <input name="attachment_sha256" aria-label="Attachment SHA-256"></label><label>Body <textarea name="body" aria-label="Body"></textarea></label>
<input type="hidden" name="op_id"><button type="submit">Send (simulated)</button></form><div id="toast" role="status"></div>
<script>f.onsubmit=async e=>{e.preventDefault(); const d=Object.fromEntries(new FormData(f)); try{const r=await fetch('/api/send',{method:'POST',body:JSON.stringify(d)}); const j=await r.json(); toast.textContent=r.ok?('Sent: '+j.id):('Error: '+j.error);}catch(x){toast.textContent='Network error: no acknowledgement';}};</script>"""
            return h._send(200, PAGE.format(title='Outbox', body=body), 'text/html')
        h._send(404, {'error': 'not found'})

    def post(self, h, path, body):
        if path != '/api/send':
            return h._send(404, {'error': 'not found'})
        with self.lock, connect(self.db_path) as db:
            op = body.get('op_id') or f'auto-{uuid.uuid4().hex}'
            prior = db.execute('SELECT id FROM messages WHERE op_id=?', (op,)).fetchone()
            if prior:
                return h._send(200, {'id': prior['id'], 'duplicate': True})
            mid = 'M-' + uuid.uuid4().hex[:8]
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?)', (mid, op, body.get('to_addr'), body.get('attachment_id'), body.get('attachment_sha256'), body.get('body'), now()))
            truth(db, 'message_committed', {'id': mid, 'to': body.get('to_addr'), 'attachment_sha256': body.get('attachment_sha256'), 'op_id': op})
        if self.faults.get('hang_after_commit_ms'):  # committed; the process may die before replying
            time.sleep(int(self.faults['hang_after_commit_ms']) / 1000)
        if self.faults.pop('drop_ack', None):  # committed, but the response never arrives
            h.connection.close()
            h.close_connection = True
            return
        h._send(200, {'id': mid})


class FixtureWorld:
    def __init__(self, root: str | Path, test_mode: bool = True, seed: int = 0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.crm = CrmApp(self.root, test_mode, seed).start()
        self.docs = DocsApp(self.root, test_mode, seed).start()
        self.outbox = OutboxApp(self.root, test_mode, seed).start()

    def stop(self):
        for a in (self.crm, self.docs, self.outbox):
            a.stop()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.stop()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='data/fixtures')
    ap.add_argument('--no-test-mode', action='store_true')
    args = ap.parse_args()
    w = FixtureWorld(args.root, not args.no_test_mode)
    print(json.dumps({'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}), flush=True)
    while True:
        time.sleep(3600)
