"""Local RESOLVE HTTP service. All mutations require a same-origin session token."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from resolve_core import Action, Ledger, digest, reconcile_uncertain_local, resolve_candidates, seed_demo, uid, verify_outbox
from agent_runtime import Cancelled, LeaseLost, MissionScheduler, TaskSpec, register_demo_readers
from capability_registry import WORKSPACES, concepts_for_workspace, load_registry
from skill_runtime import AmbiguousEntity, SkillError, SkillStore, candidate_from_confirmed
import windows_adapter
import uia_adapter
from web_api import WebApi

DB = Path(os.environ.get('RESOLVE_DB', ROOT / 'data' / 'resolve.sqlite3'))
ledger = Ledger(DB)
seed_demo(ledger)
reconcile_uncertain_local(ledger)
SESSION_TOKEN = secrets.token_urlsafe(32)
scheduler = MissionScheduler(ledger.path, revision_check=lambda mid, revision: bool((mission := ledger.get_json('missions', mid)) and mission['status'] == 'ACTIVE' and mission['intent_revision'] == revision))
register_demo_readers(scheduler, ledger)
skill_store = SkillStore(ledger.path)
web_api = WebApi(ledger)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def require(condition: bool, status: int, message: str) -> None:
    if not condition:
        raise ApiError(status, message)


def latest_report(ledger: Ledger, context: str) -> dict:
    reports = resolve_candidates(ledger, 'document', context=context)
    reports = [report for report in reports if report.get('modified_at')]
    require(bool(reports), 409, 'no report with a modification timestamp is available')
    return max(reports, key=lambda report: (report['modified_at'], report.get('version') or ''))


class Handler(BaseHTTPRequestHandler):
    server_version = 'RESOLVE/0.2'

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

    def send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def check_origin(self, mutation: bool = False) -> None:
        port = self.server.server_port
        allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        require(self.client_address[0] in {'127.0.0.1', '::1'}, 403, 'loopback access only')
        require(self.headers.get('Host') in allowed_hosts, 403, 'invalid Host header')
        origin = self.headers.get('Origin')
        require(not origin or origin in {f'http://{host}' for host in allowed_hosts}, 403, 'cross-origin request refused')
        if mutation:
            require(self.headers.get('Content-Type', '').split(';')[0].strip().lower() == 'application/json', 415, 'JSON content type required')
            require(secrets.compare_digest(self.headers.get('X-Resolve-Token', ''), SESSION_TOKEN), 403, 'session token required')

    def body(self) -> dict:
        size = int(self.headers.get('Content-Length', '0'))
        require(0 <= size <= 32768, 413, 'request too large')
        data = json.loads(self.rfile.read(size) or b'{}')
        require(isinstance(data, dict), 400, 'JSON object required')
        return data

    def do_GET(self):
        try:
            self.check_origin()
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/api/stream':
                q = parse_qs(parsed.query)
                after = q.get('after', [self.headers.get('Last-Event-ID', '0')])[0] or '0'
                require(bool(re.fullmatch(r'\d+(\.\d+)?', after)), 400, 'bad stream cursor')
                return web_api.stream(self, after, min(float(q.get('max_s', ['300'])[0]), 3600))
            if path.startswith('/api/web/'):
                status, body = web_api.call('GET', path, {})
                return self.send_json(status, body)
            pages = {'/': 'landing.html', '/app': 'index.html', '/app/': 'index.html'}
            static = re.fullmatch(r'/static/([A-Za-z0-9_.-]+\.(js|css|svg))', path)
            if static:
                asset = ROOT / 'web' / 'static' / static.group(1)
                require(asset.is_file(), 404, 'asset not found')
                raw = asset.read_bytes()
                kind = {'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[static.group(2)]
                self.send_response(200)
                self.send_header('Content-Type', kind + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path in pages:
                page = ROOT / 'web' / pages[path]
                raw = (page if page.exists() else ROOT / 'web' / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == '/api/health':
                return self.send_json(200, {'status': 'ok', 'name': 'RESOLVE', 'version': '0.2', 'database': 'sqlite-wal'})
            if path == '/api/session':
                return self.send_json(200, {'token': SESSION_TOKEN})
            if path == '/api/state':
                return self.send_json(200, {'missions': ledger.all_json('missions'), 'entities': ledger.all_json('entities'), 'actions': ledger.all_actions(), 'events': ledger.events(), 'task_events': scheduler.events(), 'outbox': ledger.outbox(), 'followups': ledger.followups()})
            if path == '/api/mission/history':
                mission_id = parse_qs(parsed.query).get('mission_id', ['mission_demo'])[0]
                return self.send_json(200, {'mission_id': mission_id, 'revisions': ledger.mission_history(mission_id)})
            if path == '/api/benchmark':
                report = ROOT / 'data' / 'benchmarks' / 'aggregate.json'
                require(report.exists(), 404, 'no benchmark run has been recorded')
                return self.send_json(200, json.loads(report.read_text(encoding='utf-8')))
            if path == '/api/workspaces':
                workspace = parse_qs(parsed.query).get('workspace', [None])[0]
                if workspace:
                    require(workspace in WORKSPACES, 400, 'unknown workspace')
                    return self.send_json(200, {'workspace': workspace, 'concepts': concepts_for_workspace(workspace)})
                concepts = load_registry()
                return self.send_json(200, {'total_concepts': len(concepts), 'workspaces': [{'name': name, 'planned_concepts': sum(item['resolve_workspace'] == name for item in concepts)} for name in sorted(WORKSPACES)]})
            if path == '/api/skills':
                return self.send_json(200, {'skills': skill_store.list_skills()})
            if path == '/api/windows':
                return self.send_json(200, windows_adapter.list_windows())
            if path == '/api/uia/availability':
                return self.send_json(200, uia_adapter.availability())
            if path.startswith('/api/evidence/'):
                require(secrets.compare_digest(self.headers.get('X-Resolve-Token', ''), SESSION_TOKEN), 403, 'session token required')
                name = path.removeprefix('/api/evidence/')
                require(bool(re.fullmatch(r'[A-Za-z0-9_-]+\.png', name)), 400, 'invalid evidence name')
                image_path = ROOT / 'data' / 'evidence' / name
                require(image_path.is_file(), 404, 'evidence not found')
                raw = image_path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            raise ApiError(404, 'not found')
        except ApiError as exc:
            self.send_json(exc.status, {'error': exc.message})
        except Exception as exc:
            self.send_json(503, {'error': str(exc)})

    def do_POST(self):
        try:
            self.check_origin(mutation=True)
            data = self.body()
            path = urlparse(self.path).path
            routed = web_api.call('POST', path, data)
            if routed:
                return self.send_json(*routed)
            if path == '/api/resolve':
                return self.send_json(200, {'contacts': resolve_candidates(ledger, 'contact', data.get('name'), data.get('context')), 'documents': resolve_candidates(ledger, 'document', data.get('name'), data.get('context'))})
            if path == '/api/mission/revise':
                mission = ledger.revise_mission(data.get('mission_id', 'mission_demo'), data.get('expected_revision'), data.get('original_request', ''))
                return self.send_json(200, mission)
            if path == '/api/action/draft':
                return self.send_json(201, self.prepare_draft(data))
            if path == '/api/action/approve-send':
                return self.send_json(200, self.approve_send(data))
            if path == '/api/agents/discover':
                return self.send_json(200, self.discover(data))
            if path == '/api/skills/propose':
                candidate = candidate_from_confirmed(ledger, data.get('action_id', ''), version=skill_store.next_version('northwind_followup'))
                review_digest = skill_store.propose(candidate)
                return self.send_json(201, {'skill': asdict(candidate), 'status': 'PENDING_APPROVAL', 'review_digest': review_digest})
            if path == '/api/skills/activate':
                skill = skill_store.activate(str(data['skill_id']), int(data['version']), str(data['review_digest']))
                return self.send_json(200, {'skill': asdict(skill), 'status': 'ACTIVE'})
            if path == '/api/skills/replay':
                plan = skill_store.prepare_replay(str(data['skill_id']), ledger, str(data.get('mission_id', 'mission_demo')), int(data['expected_revision']), dict(data.get('inputs', {})))
                draft = self.prepare_draft({'mission_id': plan.mission_id, 'contact_id': plan.contact['entity_id'], 'report_id': plan.report['entity_id']})
                return self.send_json(201, {'plan': asdict(plan), 'draft': draft})
            if path == '/api/emergency-stop':
                scheduler.cancel()
                windows_adapter.stop()
                return self.send_json(200, {'status': 'STOPPED', 'cursor_lease_revoked': True})
            if path == '/api/resume':
                scheduler.reset_cancellation()
                windows_adapter.reset_stop()
                windows_adapter.resume()
                return self.send_json(200, {'status': 'READY'})
            if path in {'/api/windows/move', '/api/windows/click', '/api/windows/type'}:
                return self.send_json(200, self.desktop_action(path.rsplit('/', 1)[1], data))
            if path == '/api/windows/capture':
                return self.send_json(200, self.capture_window(data))
            if path == '/api/uia/inspect':
                return self.send_json(200, self.inspect_uia(data))
            raise ApiError(404, 'not found')
        except ApiError as exc:
            self.send_json(exc.status, {'error': exc.message})
        except AmbiguousEntity as exc:
            self.send_json(409, {'error': str(exc), 'candidates': exc.candidates})
        except SkillError as exc:
            self.send_json(409, {'error': str(exc)})
        except ValueError as exc:
            self.send_json(409 if str(exc) == 'stale mission revision' else 400, {'error': str(exc)})
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {'error': str(exc)})
        except sqlite3.IntegrityError as exc:
            self.send_json(409, {'error': str(exc)})
        except (Cancelled, LeaseLost, windows_adapter.UnsupportedDesktop, TimeoutError) as exc:
            self.send_json(409, {'error': str(exc)})
        except uia_adapter.UIAUnavailable as exc:
            self.send_json(503, {'error': str(exc), 'status': 'UNSUPPORTED'})
        except Exception as exc:
            self.send_json(500, {'error': str(exc)})

    def prepare_draft(self, data: dict) -> dict:
        with ledger.lock:
            mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require('prepare_draft' in mission['permitted_actions'], 403, 'drafting is not permitted')
            contact = ledger.get_json('entities', data.get('contact_id', ''))
            report = ledger.get_json('entities', data.get('report_id', ''))
            require(bool(contact) and bool(report), 400, 'select an existing contact and report')
            require(contact['kind'] == 'contact' and report['kind'] == 'document', 400, 'entity kinds do not match action')
            context = mission['constraints']['client']
            require(contact.get('context') == context and report.get('context') == context, 409, 'contact/report context differs from mission client')
            require(report['entity_id'] == latest_report(ledger, context)['entity_id'], 409, 'selected report is not latest under the stated modified-time policy')
            action = Action(uid('act'), mission['mission_id'], mission['intent_revision'], 'simulated_send_and_update_followup', contact['entity_id'], 'local-demo-agent', 'local-outbox', None, {'attachment_id': report['entity_id'], 'body': 'Approved Northwind follow-up with the latest report attached.', 'followup_id': 'followup_northwind'}, None, {'recipient_id': contact['stable_id'], 'attachment_id': report['stable_id'], 'followup_status': 'contacted'}, 'NON_IDEMPOTENT', 'consequential')
            ledger.create_action(action)
            ledger.transition(action.action_id, 'BOUND')
            ledger.transition(action.action_id, 'NEEDS_APPROVAL')
            current = ledger.action(action.action_id)
            return {'action': current, 'digest': digest(current), 'contact': contact, 'report': report}

    def discover(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        revision = mission['intent_revision']
        specs = [
            TaskSpec(uid('task'), mission['mission_id'], revision, 'contact-resolver', 'resolve_contacts', {'name': 'Alex'}, capabilities=frozenset({'entities.read'})),
            TaskSpec(uid('task'), mission['mission_id'], revision, 'report-resolver', 'resolve_reports', {'context': mission['constraints']['client']}, capabilities=frozenset({'entities.read'})),
        ]
        results = scheduler.run(specs)
        return {'tasks': [asdict(result) for result in results.values()], 'mission_id': mission['mission_id'], 'intent_revision': revision}

    def approve_send(self, data: dict) -> dict:
        with ledger.lock:
            action = ledger.action(data.get('action_id', ''))
            require(bool(action), 404, 'action not found')
            mission = ledger.get_json('missions', action['mission_id'])
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require(action['intent_revision'] == mission['intent_revision'], 409, 'approval refused: action was superseded by a newer intent revision')
            require(action['operation'] == 'simulated_send_and_update_followup', 403, 'operation cannot use this approval endpoint')
            require('simulated_send_after_approval' in mission['permitted_actions'], 403, 'send is not permitted')
            require(action['status'] == 'NEEDS_APPROVAL', 409, 'action is not awaiting approval')
            approval_digest = digest(action)
            require(data.get('digest') == approval_digest, 409, 'approval digest mismatch; action changed')
            contact = ledger.get_json('entities', action['target_entity_id'])
            report = ledger.get_json('entities', action['arguments']['attachment_id'])
            context = mission['constraints']['client']
            require(bool(contact) and bool(report) and contact.get('context') == context and report.get('context') == context, 409, 'target binding changed')
            require(report['entity_id'] == latest_report(ledger, context)['entity_id'], 409, 'report version changed after approval review')
            require(contact['stable_id'] == action['expected_effect']['recipient_id'] and report['stable_id'] == action['expected_effect']['attachment_id'], 409, 'stable entity identity changed')
            ledger.transition(action['action_id'], 'AUTHORIZED', {'approval_digest': approval_digest})
            ledger.transition(action['action_id'], 'READY')
            ledger.transition(action['action_id'], 'DISPATCHING')
            try:
                message_id = ledger.commit_simulated_send(action['action_id'], action['target_entity_id'], action['arguments']['attachment_id'], action['arguments']['body'], action['arguments']['followup_id'])
            except Exception:
                ledger.transition(action['action_id'], 'UNCERTAIN')
                raise
            ledger.transition(action['action_id'], 'DISPATCHED', {'message_id': message_id})
            ledger.transition(action['action_id'], 'OBSERVING')
            result = verify_outbox(ledger, action['target_entity_id'], action['arguments']['attachment_id'], action['arguments']['followup_id'], action['action_id'])
            ledger.transition(action['action_id'], result['status'], {'verification': result, 'verification_status': result['status'], 'evidence_references': [message_id]})
            return {'action': ledger.action(action['action_id']), 'verification': result}

    def desktop_action(self, operation: str, data: dict) -> dict:
        # A user invokes each input through the local dashboard. The bound snapshot
        # and current mission revision are checked again before SendInput.
        with ledger.lock:
            mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
            require(f'physical_{operation}' in mission['permitted_actions'], 403, 'physical operation is not permitted')
            require(isinstance(data.get('snapshot_id'), str) and bool(data['snapshot_id']), 400, 'fresh window snapshot required')
            hwnd = int(data['hwnd'])
            if operation == 'move':
                require(isinstance(data.get('smooth', True), bool), 400, 'smooth must be a boolean')
            arguments = {key: data[key] for key in ('x', 'y', 'text', 'smooth') if key in data}
            action_id = uid('act')
            lease = scheduler.leases.acquire(action_id, duration_seconds=3, wait_seconds=2, stop=scheduler._stop)
            try:
                scheduler.leases.validate(lease)
                windows_adapter.validate_target(hwnd, data['snapshot_id'], int(data['x']) if operation != 'type' else None, int(data['y']) if operation != 'type' else None)
                action = Action(action_id, mission['mission_id'], mission['intent_revision'], f'physical_{operation}', f'hwnd:{hwnd}', 'local-user-control', 'windows-desktop', {'hwnd': hwnd, 'cursor_lease_generation': lease.generation}, arguments, data['snapshot_id'], {'input_dispatched': True}, 'IDEMPOTENT_WRITE' if operation == 'move' else 'UNKNOWN', 'moderate')
                ledger.create_action(action)
                ledger.transition(action.action_id, 'BOUND')
                ledger.transition(action.action_id, 'AUTHORIZED', {'approval_digest': digest({'operation': operation, 'hwnd': hwnd, 'arguments': arguments, 'snapshot_id': data['snapshot_id']})})
                ledger.transition(action.action_id, 'READY')
                ledger.transition(action.action_id, 'DISPATCHING')
                scheduler.leases.validate(lease)
                if operation == 'move':
                    result = windows_adapter.move_pointer(hwnd, int(data['x']), int(data['y']), snapshot_id=data['snapshot_id'], smooth=data.get('smooth', True))
                elif operation == 'click':
                    result = windows_adapter.click(hwnd, int(data['x']), int(data['y']), snapshot_id=data['snapshot_id'])
                else:
                    result = windows_adapter.type_text(hwnd, str(data['text']), snapshot_id=data['snapshot_id'])
            except Exception:
                if ledger.action(action_id) and ledger.action(action_id)['status'] == 'DISPATCHING':
                    ledger.transition(action_id, 'UNCERTAIN')
                raise
            finally:
                scheduler.leases.release(lease)
            ledger.transition(action.action_id, 'DISPATCHED')
            ledger.transition(action.action_id, 'OBSERVING')
            ledger.transition(action.action_id, 'UNCERTAIN', {'verification_status': 'UNCERTAIN', 'verification': {'reason': 'physical input dispatched; no independent business-effect verifier'}})
            return {'action_id': action.action_id, 'status': 'UNCERTAIN', 'input_result': result}

    def capture_window(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
        require('read' in mission['permitted_actions'], 403, 'window observation is not permitted')
        require(isinstance(data.get('snapshot_id'), str), 400, 'window snapshot required')
        result = windows_adapter.capture_window(int(data['hwnd']), data['snapshot_id'])
        return {**result, 'image_name': Path(result['path']).name}

    def inspect_uia(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
        require('read' in mission['permitted_actions'], 403, 'UIA inspection is not permitted')
        require(isinstance(data.get('snapshot_id'), str), 400, 'window snapshot required')
        hwnd = int(data['hwnd'])
        windows_adapter.validate_snapshot(hwnd, data['snapshot_id'])
        return uia_adapter.inspect_window(hwnd)


def main():
    address = ('127.0.0.1', int(os.environ.get('RESOLVE_PORT', '8765')))
    server = ThreadingHTTPServer(address, Handler)
    print(f'RESOLVE local server at http://{address[0]}:{address[1]}  database={DB}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping RESOLVE.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
