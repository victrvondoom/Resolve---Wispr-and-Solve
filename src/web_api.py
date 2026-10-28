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


class WebApi:
    def __init__(self, ledger, root: Path | None = None):
        self.ledger = ledger
        self.root = Path(root or os.environ.get('RESOLVE_FIXTURE_DIR', ROOT / 'data' / 'fixtures'))
        self.world: FixtureWorld | None = None
        self.runner: WebMissionRunner | None = None

    def ensure_world(self) -> dict:
        if not self.world:
            self.world = FixtureWorld(self.root, test_mode=os.environ.get('RESOLVE_TEST_MODE', '1') == '1')
            urls = {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url}
            self.runner = WebMissionRunner(self.ledger, urls, self.root)
        return {'crm': self.world.crm.url, 'docs': self.world.docs.url, 'outbox': self.world.outbox.url, 'test_mode': self.world.crm.test_mode}

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
            self.ensure_world()
            if parts == ['missions'] and method == 'POST':
                return 201, self.runner.start(str(data.get('command', '')))
            if parts[0] == 'missions' and len(parts) == 2 and method == 'GET':
                m = self.runner.get(parts[1])
                return 200, {**m, 'actions_detail': [self.ledger.action(a) for a in m['actions']]}
            if parts[0] == 'missions' and len(parts) == 3 and method == 'GET' and parts[2] == 'events':
                return 200, {'events': self.mission_events(parts[1])}
            if parts[0] == 'missions' and len(parts) == 3 and method == 'POST':
                mid, op = parts[1], parts[2]
                if op == 'bind':
                    return 200, self.runner.bind(mid, data['contact_id'], int(data['revision']))
                if op == 'correct':
                    return 200, self.runner.correct(mid, data['contact_id'])
                if op == 'plan':
                    return 200, self.runner.plan(mid)
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
            if parts == ['faults'] and method == 'POST':
                import urllib.request
                app = getattr(self.world, data['app'])
                req = urllib.request.Request(f"{app.url}/_fault/{data['name']}", data=json.dumps({'value': data.get('value', True)}).encode(), method='POST')
                return 200, json.loads(urllib.request.urlopen(req).read())
            return 404, {'error': 'unknown web route'}
        except MissionError as exc:
            status = {'NOT_FOUND': 404, 'UNPARSEABLE': 400}.get(exc.code, 409)
            return status, {'error': str(exc), 'code': exc.code, **exc.extra}
        except KeyError as exc:
            return 400, {'error': f'missing field {exc}'}
        except Exception as exc:  # urllib HTTPError for disabled faults, etc.
            code = getattr(exc, 'code', 500)
            return (code if isinstance(code, int) else 500), {'error': str(exc)}

    def stream(self, handler, after: int, max_seconds: float) -> None:
        """SSE over the persisted ledger. Each frame id is the events.seq, so a reconnect resumes losslessly."""
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        deadline = time.monotonic() + max_seconds
        cursor = after
        try:
            while time.monotonic() < deadline:
                with self.ledger.connect() as db:
                    rows = db.execute('SELECT seq,action_id,state,at FROM events WHERE seq>? ORDER BY seq LIMIT 100', (cursor,)).fetchall()
                for r in rows:
                    cursor = r['seq']
                    handler.wfile.write(f"id: {r['seq']}\nevent: action.{r['state'].lower()}\ndata: {json.dumps(dict(r))}\n\n".encode())
                handler.wfile.write(b': keepalive\n\n' if not rows else b'')
                handler.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass
