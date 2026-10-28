"""HTTP surface for the browser-fixture IEEC missions plus the persisted-event SSE stream."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps'))

from web_fixtures import FixtureWorld
from web_mission import MissionError, WebMissionRunner
from supervisor import Supervisor


class WebApi:
    def __init__(self, ledger, root: Path | None = None):
        self.ledger = ledger
        self.root = Path(root or os.environ.get('RESOLVE_FIXTURE_DIR', ROOT / 'data' / 'fixtures'))
        self.world: FixtureWorld | None = None
        self.runner: WebMissionRunner | None = None
        self.supervisor: Supervisor | None = None
        import threading
        self._world_lock = threading.Lock()
        self.skills = None
        self.files = None

    def ensure_world(self) -> dict:
        with self._world_lock:  # concurrent first requests must not both seed the fixture DBs
            return self._ensure_world()

    def _ensure_world(self) -> dict:
        if not self.world:
            self.world = FixtureWorld(self.root, test_mode=os.environ.get('RESOLVE_TEST_MODE', '1') == '1')
            urls = {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url}
            self.runner = WebMissionRunner(self.ledger, urls, self.root)
            self.supervisor = Supervisor(self.runner, self.root)
            self._optional_modules()
        return {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url, 'test_mode': self.world.crm.test_mode}

    def _optional_modules(self) -> None:
        try:
            from web_skills import SkillLibrary
            self.skills = SkillLibrary(self.ledger.path)
            self.supervisor.skills = self.skills
        except ImportError:
            self.skills = None
        try:
            from file_worker import FileWorker
            sandbox = Path(os.environ.get('RESOLVE_FILE_SANDBOX', self.root / 'sandbox'))
            sandbox.mkdir(parents=True, exist_ok=True)
            self.files = FileWorker(sandbox, Path(self.ledger.path))
        except ImportError:
            self.files = None

    def status(self) -> dict:
        import importlib.util
        import windows_adapter
        win = {'available': bool(windows_adapter.IS_WINDOWS), 'reason': 'Windows host' if windows_adapter.IS_WINDOWS else 'WINDOWS_NATIVE_TESTS_NOT_RUN: host is not Windows'}
        return {'playwright': importlib.util.find_spec('playwright') is not None, 'windows_native': win,
                'speech': {'mode': 'browser Web Speech API when the browser exposes it; typed transcript otherwise',
                           'note': 'no server-side speech engine; partial speech never executes'},
                'provider': 'deterministic templates (no model, no API keys)',
                'test_mode': os.environ.get('RESOLVE_TEST_MODE', '1') == '1',
                'world_started': self.world is not None,
                'skills': self.skills is not None, 'files': self.files is not None}

    def missions(self) -> list[dict]:
        rows = [m for m in self.ledger.all_json('missions') if str(m.get('mission_id', '')).startswith('wm_')]
        rows.sort(key=lambda m: m.get('created_at', ''), reverse=True)
        return [{k: m.get(k) for k in ('mission_id', 'goal', 'status', 'intent_revision', 'created_at', 'updated_at')} for m in rows]

    def mission_events(self, mission_id: str) -> list[dict]:
        ids = set(self.runner.get(mission_id)['actions'])
        return [{'seq': e['seq'], 'action_id': e['action_id'], 'state': e['state'], 'at': e['at']} for e in reversed(self.ledger.events()) if e['action_id'] in ids]

    def call(self, method: str, path: str, data: dict):
        """Return (status, body) or None if the route is not ours."""
        if not path.startswith('/api/web/'):
            return None
        try:
            parts = path.removeprefix('/api/web/').strip('/').split('/')
            if parts == ['world'] and method == 'POST':
                return 200, self.ensure_world()
            if parts == ['status'] and method == 'GET':
                return 200, self.status()
            if parts == ['benchmark'] and method == 'GET':
                f = ROOT / 'data' / 'benchmarks' / 'web_aggregate.json'
                return (200, json.loads(f.read_text())) if f.exists() else (404, {'error': 'no web benchmark has been run; python benchmarks/web_bench.py'})
            if parts == ['missions'] and method == 'GET':
                return 200, {'missions': self.missions()}
            if parts == ['desktop', 'notepad'] and method == 'POST':
                import windows_adapter
                if not windows_adapter.IS_WINDOWS:
                    return 503, {'error': 'WINDOWS_NATIVE_TESTS_NOT_RUN: Notepad automation needs an interactive Windows desktop', 'code': 'UNSUPPORTED'}
                from notepad_w1 import NotepadW1
                return 200, NotepadW1(self.ledger).run(str(data.get('text') or 'RESOLVE smoke test'))
            if parts == ['catalog'] and method == 'GET':
                from catalog import load_catalog, summary
                entries = load_catalog()
                return 200, {'summary': summary(entries), 'concepts': entries}
            self.ensure_world()
            if parts == ['agents'] and method == 'GET':
                return 200, {'agents': self.supervisor.agents()}
            if parts == ['missions'] and method == 'POST':
                return 201, self.runner.start(str(data.get('command', '')))
            if parts[0] == 'missions' and len(parts) == 2 and method == 'GET':
                m = self.runner.get(parts[1])
                return 200, {**m, 'actions_detail': [self.ledger.action(a) for a in m['actions']]}
            if parts[0] == 'missions' and len(parts) == 3 and method == 'GET' and parts[2] == 'events':
                return 200, {'events': self.mission_events(parts[1])}
            if parts[0] == 'missions' and len(parts) == 3 and method == 'GET' and parts[2] == 'tasks':
                return 200, {'tasks': self.supervisor.scheduler.events(parts[1])}
            if parts[0] == 'missions' and len(parts) == 3 and method == 'POST':
                mid, op = parts[1], parts[2]
                if op == 'bind':
                    return 200, self.runner.bind(mid, data['contact_id'], int(data['revision']))
                if op == 'correct':
                    return 200, self.runner.correct(mid, data['contact_id'])
                if op == 'plan':
                    return 200, self.runner.plan(mid)
                if op == 'utterances':
                    text = str(data.get('text', ''))
                    if data.get('partial'):
                        return 409, {'error': 'PARTIAL_SPEECH: partial transcripts preview only and never execute', 'code': 'PARTIAL_SPEECH'}
                    return 200, self.runner.utter(mid, text)
                if op == 'cancel':
                    return 200, self.runner.cancel(mid)
                if op == 'supervise':
                    phase = data.get('phase', 'investigate')
                    if phase not in {'investigate', 'execute'}:
                        return 400, {'error': 'phase must be investigate or execute'}
                    return 200, getattr(self.supervisor, phase)(mid)
            if parts[0] == 'actions' and len(parts) == 3 and method == 'POST':
                aid, op = parts[1], parts[2]
                if op == 'approve':
                    return 200, self.runner.approve(aid, str(data.get('digest', '')))
                if op == 'run':
                    a = self.ledger.action(aid)
                    if not a:
                        return 404, {'error': 'unknown action'}
                    return 200, (self.runner.run_send(aid) if a['operation'] == 'outbox_send' else self.runner.run_task(aid))
                if op == 'reconcile':
                    return 200, self.runner.reconcile(aid)
            if parts == ['utterances'] and method == 'POST':  # utterance with no mission yet -> NEW_GOAL
                if data.get('partial'):
                    return 409, {'error': 'PARTIAL_SPEECH: partial transcripts preview only and never execute', 'code': 'PARTIAL_SPEECH'}
                return 201, self.runner.utter(None, str(data.get('text', '')))
            if parts[0] == 'skills':
                return self.skill_route(method, parts[1:], data)
            if parts[0] == 'files':
                return self.file_route(method, parts[1:], data)
            if parts == ['faults'] and method == 'POST':
                import urllib.request
                app = getattr(self.world, data['app'])
                req = urllib.request.Request(f"{app.url}/_fault/{data['name']}", data=json.dumps({'value': data.get('value', True)}).encode(), method='POST')
                return 200, json.loads(urllib.request.urlopen(req).read())
            return 404, {'error': 'unknown web route'}
        except Exception as exc:
            if type(exc).__name__ != 'SkillError' and not isinstance(exc, (MissionError, KeyError)):
                code = getattr(exc, 'code', 500)
                return (code if isinstance(code, int) else 500), {'error': str(exc)}
            if type(exc).__name__ == 'SkillError':
                return 409, {'error': str(exc), 'code': exc.code, **getattr(exc, 'extra', {})}
            if isinstance(exc, KeyError):
                return 400, {'error': f'missing field {exc}'}
            status = {'NOT_FOUND': 404, 'UNPARSEABLE': 400}.get(exc.code, 409)
            return status, {'error': str(exc), 'code': exc.code, **exc.extra}

    def skill_route(self, method, rest, data):
        if self.skills is None:
            return 503, {'error': 'skill library module unavailable'}
        if not rest and method == 'GET':
            return 200, {'skills': self.skills.list()}
        if rest == ['record'] and method == 'POST':
            return 201, self.skills.record(self.runner.urls, self.root, data['contact_id'], data['title'])
        if rest == ['import'] and method == 'POST':
            return 201, self.skills.import_skill(data)
        if len(rest) == 2 and method == 'POST' and rest[1] == 'approve':
            return 200, self.skills.approve(rest[0], int(data['version']))
        if len(rest) == 2 and method == 'POST' and rest[1] == 'replay':
            if data.get('fault'):
                if data['fault'] != 'layout_shift':
                    return 400, {'error': 'only the layout_shift fault is supported here'}
                self._fault('crm', 'layout_shift', True)
            try:
                return 200, self.skills.replay(rest[0], data.get('params') or {}, self.runner.urls, self.root, data.get('version'))
            finally:
                if data.get('fault'):
                    self._fault('crm', 'layout_shift', False)
        if len(rest) == 2 and method == 'GET' and rest[1] == 'export':
            return 200, self.skills.export(rest[0], None)
        if len(rest) == 1 and method == 'GET':
            return 200, self.skills.get(rest[0])
        return 404, {'error': 'unknown skills route'}

    def file_route(self, method, rest, data):
        if self.files is None:
            return 503, {'error': 'file worker module unavailable'}
        if method != 'POST' or len(rest) != 1:
            return 404, {'error': 'unknown files route'}
        op = rest[0]
        if op == 'inventory':
            return 200, self.files.inventory()
        if op == 'duplicates':
            return 200, self.files.duplicates()
        if op == 'plan':
            return 200, self.files.plan_dedupe()
        if op == 'apply':
            return 200, self.files.apply(data['plan_id'])
        if op == 'undo':
            return 200, self.files.undo(data['op_id'])
        if op == 'note':
            return 201, self.files.write_note(data['name'], data['text'])
        if op == 'seed':  # demo convenience: a few files incl. a content duplicate, inside the sandbox only
            root = self.files.root
            (root / 'reports').mkdir(exist_ok=True)
            (root / 'reports' / 'q3-ops-final.txt').write_text('Q3 Operations Report v4\n')
            (root / 'reports' / 'q3-ops-final (copy).txt').write_text('Q3 Operations Report v4\n')
            (root / 'reports' / 'q3-sales.txt').write_text('Q3 Sales Report v2\n')
            return 201, self.files.inventory()
        return 404, {'error': 'unknown files route'}

    def _fault(self, app_name, name, value):
        import urllib.request
        app = getattr(self.world, app_name)
        req = urllib.request.Request(f"{app.url}/_fault/{name}", data=json.dumps({'value': value}).encode(), method='POST')
        return json.loads(urllib.request.urlopen(req).read())

    def stream(self, handler, after, max_seconds: float) -> None:
        """SSE over persisted state only. Event id is "<action_seq>.<task_seq>" so reconnects resume losslessly."""
        a_cur, _, t_cur = str(after or '0').partition('.')
        a_cur, t_cur = int(a_cur or 0), int(t_cur or 0)
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        deadline = time.monotonic() + max_seconds
        try:
            while time.monotonic() < deadline:
                with self.ledger.connect() as db:
                    acts = db.execute('SELECT seq,action_id,state,at FROM events WHERE seq>? ORDER BY seq LIMIT 100', (a_cur,)).fetchall()
                    has_tasks = db.execute("SELECT 1 FROM sqlite_master WHERE name='task_events'").fetchone()
                    tasks = db.execute('SELECT seq,task_id,mission_id,status,at FROM task_events WHERE seq>? ORDER BY seq LIMIT 100', (t_cur,)).fetchall() if has_tasks else []
                frames = []
                for r in acts:
                    a_cur = r['seq']
                    frames.append((f"action.{r['state'].lower()}", dict(r)))
                for r in tasks:
                    t_cur = r['seq']
                    frames.append((f"task.{r['status'].lower()}", dict(r)))
                for name, data in frames:
                    handler.wfile.write(f"id: {a_cur}.{t_cur}\nevent: {name}\ndata: {json.dumps(data)}\n\n".encode())
                if not frames:
                    handler.wfile.write(b': keepalive\n\n')
                handler.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass
