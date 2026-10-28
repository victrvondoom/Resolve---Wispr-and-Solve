"""IEEC mission runner that operates the web fixture apps through Playwright.

Browser actions are DOM-level (role/label/testid locators), NOT physical cursor
movement. Verification reads the apps' SQLite files directly via `WebVerifier`,
never the toasts the pages show.
"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_runtime import CursorLeaseManager, LeaseLost
from resolve_core import Action, Ledger, digest, now, uid

CHROMIUM = '/opt/pw-browsers/chromium'


def browser_launch_options(headless: bool = True) -> dict:
    """RESOLVE_BROWSER_CHANNEL=msedge|chrome uses an installed browser (typical on Windows, no download);
    RESOLVE_CHROMIUM=<path> pins a binary; otherwise the preinstalled cloud Chromium or Playwright's default."""
    import os
    opts: dict = {'headless': headless}
    if os.environ.get('RESOLVE_BROWSER_CHANNEL'):
        opts['channel'] = os.environ['RESOLVE_BROWSER_CHANNEL']
    elif os.environ.get('RESOLVE_CHROMIUM'):
        opts['executable_path'] = os.environ['RESOLVE_CHROMIUM']
    elif Path(CHROMIUM).exists():
        opts['executable_path'] = CHROMIUM
    return opts


class MissionError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(f"{code}: {message}")
        self.code, self.extra = code, extra


class WebVerifier:
    """Independent effect checks straight from each app's database."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _q(self, name: str, sql: str, args=()):
        db = sqlite3.connect(str(self.root / f'{name}.sqlite3'), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, args)]
        finally:
            db.close()

    def task(self, op_id: str, contact_id: str) -> dict:
        rows = self._q('crm', 'SELECT * FROM tasks WHERE op_id=?', (op_id,))
        if not rows:
            return {'status': 'FAILED', 'evidence': {'tasks_for_op': 0}}
        wrong = [r for r in rows if r['contact_id'] != contact_id]
        if wrong:
            return {'status': 'WRONG_TARGET', 'evidence': {'task': wrong[0]['id'], 'contact_id': wrong[0]['contact_id']}}
        return {'status': 'CONFIRMED', 'evidence': {'task': rows[0]['id'], 'contact_id': contact_id, 'source': 'crm.sqlite3'}}

    def message(self, op_id: str, to_addr: str, sha: str) -> dict:
        rows = self._q('outbox', 'SELECT * FROM messages WHERE op_id=?', (op_id,))
        if not rows:
            return {'status': 'FAILED', 'evidence': {'messages_for_op': 0}}
        m = rows[0]
        if m['to_addr'] != to_addr or m['attachment_sha256'] != sha:
            return {'status': 'WRONG_TARGET', 'evidence': {'message': m['id'], 'to': m['to_addr'], 'sha256': m['attachment_sha256']}}
        total = self._q('outbox', 'SELECT COUNT(*) n FROM messages WHERE to_addr=? AND attachment_sha256=?', (to_addr, sha))[0]['n']
        if total > 1:
            return {'status': 'UNCERTAIN', 'evidence': {'reason': 'duplicate messages', 'count': total}}
        return {'status': 'CONFIRMED', 'evidence': {'message': m['id'], 'to': to_addr, 'sha256': sha, 'source': 'outbox.sqlite3'}}

    def report_sha(self, report_id: str) -> str | None:
        rows = self._q('docs', 'SELECT sha256 FROM reports WHERE id=?', (report_id,))
        return rows[0]['sha256'] if rows else None


class WebMissionRunner:
    def __init__(self, ledger: Ledger, urls: dict[str, str], root: Path, headless: bool = True):
        self.ledger, self.urls, self.verifier = ledger, urls, WebVerifier(root)
        self.headless = headless
        # one fenced executor at a time across missions/processes sharing this ledger DB
        self.leases = CursorLeaseManager(ledger.path)
        self.hooks: dict = {}
        # Benchmark-only ablation switches (benchmarks/ablations.py). Empty in normal operation; never set by the API.
        # 'temporal_guard' | 'reconcile' | 'verifier' | 'digest'
        self.ablate: set[str] = set()

    @contextmanager
    def browser(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(**browser_launch_options(self.headless))
            try:
                yield b
            finally:
                b.close()

    # ---- observation (DOM) -------------------------------------------------
    def observe(self, browser) -> dict[str, list[dict]]:
        page = browser.new_page()
        page.goto(self.urls['crm'])
        dismiss = page.get_by_role('button', name='Dismiss')
        if dismiss.count():
            dismiss.click()  # popup fault: handle, do not click blindly through it
        page.get_by_test_id('contact-row').first.wait_for(timeout=10000)  # delayed render
        contacts = [{'id': r.get_attribute('data-contact-id'), 'cells': r.locator('td').all_inner_texts()}
                    for r in page.get_by_test_id('contact-row').all()]
        contacts = [{'id': c['id'], 'name': c['cells'][1], 'email': c['cells'][2], 'org': c['cells'][3], 'dept': c['cells'][4]} for c in contacts]
        page.goto(self.urls['docs'])
        page.get_by_test_id('report-row').first.wait_for(timeout=10000)
        reports = [{'id': r.get_attribute('data-report-id'), 'sha256': r.get_attribute('data-sha256'), 'modified_at': r.get_attribute('data-modified'),
                    'cells': r.locator('td').all_inner_texts()} for r in page.get_by_test_id('report-row').all()]
        reports = [{'id': r['id'], 'title': r['cells'][1], 'version': r['cells'][2], 'contact_id': r['cells'][3], 'modified_at': r['modified_at'], 'sha256': r['sha256']} for r in reports]
        page.close()
        return {'contacts': contacts, 'reports': reports}

    # ---- mission lifecycle ---------------------------------------------------
    def start(self, command: str) -> dict:
        m = re.search(r'for ([A-Z][a-z]+ [A-Z][a-z]+)', command)
        if not m:
            raise MissionError('UNPARSEABLE', 'deterministic templates need "for <First Last>"')
        mission = {'mission_id': uid('wm'), 'goal': command, 'intent_revision': 1, 'person': m.group(1),
                   'wants_send': bool(re.search(r'\bsend\b', command, re.I)), 'status': 'ACTIVE', 'binding': None,
                   'created_at': now(), 'updated_at': now(), 'actions': []}
        with self.browser() as b:
            world = self.observe(b)
        cands = [c for c in world['contacts'] if c['name'].casefold() == mission['person'].casefold()]
        mission['candidates'] = cands
        if len(cands) == 0:
            mission['status'] = 'FAILED'
        elif len(cands) > 1:
            mission['status'] = 'NEEDS_CLARIFICATION'
            mission['ambiguity_reason'] = f'{len(cands)} contacts named {mission["person"]}; distinguishing evidence: ' + '; '.join(f"{c['id']} {c['email']} ({c['org']}/{c['dept']})" for c in cands)
        else:
            mission['binding'] = {'contact_id': cands[0]['id'], 'email': cands[0]['email'], 'basis': 'unique name match'}
        self._save(mission)
        return mission

    def get(self, mission_id: str) -> dict:
        m = self.ledger.get_json('missions', mission_id)
        if not m:
            raise MissionError('NOT_FOUND', 'unknown mission')
        return m

    def _save(self, mission: dict) -> None:
        mission['updated_at'] = now()
        self.ledger.write_json('missions', mission['mission_id'], mission)

    def bind(self, mission_id: str, contact_id: str, expected_revision: int) -> dict:
        mission = self.get(mission_id)
        if mission['intent_revision'] != expected_revision:
            raise MissionError('STALE_REVISION', 'mission revision changed')
        cand = next((c for c in mission['candidates'] if c['id'] == contact_id), None)
        if not cand:
            raise MissionError('NOT_A_CANDIDATE', f'{contact_id} is not among the discovered candidates')
        mission['binding'] = {'contact_id': cand['id'], 'email': cand['email'], 'basis': 'user selected'}
        mission['status'] = 'ACTIVE'
        self._save(mission)
        return mission

    PRE_DISPATCH = {'PROPOSED', 'BOUND', 'NEEDS_APPROVAL', 'AUTHORIZED', 'READY'}
    IN_FLIGHT = {'DISPATCHING', 'DISPATCHED', 'OBSERVING', 'UNCERTAIN'}

    def _supersede_pending(self, mission: dict, reason: str) -> dict:
        superseded, in_flight = [], []
        for aid in mission.get('actions', []):
            a = self.ledger.action(aid)
            if not a:
                continue
            if a['status'] in self.PRE_DISPATCH and not ('digest' in self.ablate and a['status'] == 'AUTHORIZED'):
                self.ledger.transition(aid, 'SUPERSEDED', {'reason': reason, 'superseded_at_revision': mission['intent_revision']})
                superseded.append(aid)
            elif a['status'] in self.IN_FLIGHT:
                in_flight.append({'action_id': aid, 'status': a['status']})
        mission.pop('presented', None)  # a spoken/visual approval summary never outlives its revision
        return {'superseded': superseded, 'in_flight': in_flight}

    def correct(self, mission_id: str, contact_id: str | None) -> dict:
        """Correction: bump the revision, supersede pre-dispatch actions and their approvals.
        In-flight actions cannot be recalled; they are reported, never silently treated as cancelled."""
        mission = self.get(mission_id)
        report = self._supersede_pending(mission, 'intent corrected')
        mission['intent_revision'] += 1
        mission['binding'] = None
        mission['actions'] = []
        mission['status'] = 'NEEDS_CLARIFICATION'
        mission['last_correction'] = report
        self._save(mission)
        if contact_id is None:
            return mission
        return self.bind(mission_id, contact_id, mission['intent_revision'])

    def cancel(self, mission_id: str) -> dict:
        mission = self.get(mission_id)
        report = self._supersede_pending(mission, 'mission cancelled by user')
        mission['intent_revision'] += 1
        mission['status'] = 'CANCELLED'
        mission['last_cancel'] = report
        self._save(mission)
        return mission

    def utter(self, mission_id: str | None, text: str) -> dict:
        """Apply one finalised utterance (typed or transcribed). Partial speech must never reach here."""
        from intent import classify
        mission = self.get(mission_id) if mission_id else None
        c = classify(text, mission)
        out: dict = {'classification': c.as_dict()}
        if c.kind == 'NEW_GOAL':
            out['mission'] = self.start(text)
            return out
        entry = {'revision_before': mission['intent_revision'], 'text': text, 'kind': c.kind, 'at': now()}
        if c.kind == 'CANCEL':
            mission = self.cancel(mission_id)
        elif c.kind == 'CORRECTION':
            target = c.matched_candidates[0] if len(c.matched_candidates) == 1 else None
            mission = self.correct(mission_id, target)
            if target is None:
                mission['ambiguity_reason'] = 'correction did not identify exactly one candidate; please choose explicitly'
        elif c.kind == 'REFINEMENT' and mission['status'] == 'NEEDS_CLARIFICATION' and len(c.matched_candidates) == 1:
            mission = self.bind(mission_id, c.matched_candidates[0], mission['intent_revision'])
        elif c.kind == 'APPROVE':
            presented = mission.get('presented')
            if not presented:
                raise MissionError('NOTHING_PRESENTED', 'no approval summary has been presented at this revision')
            out['approval'] = self.approve(presented['action_id'], presented['digest'])
            out['approved_summary'] = presented['summary']
        elif c.kind == 'QUESTION':
            out['answer'] = self.describe(mission)
        mission = self.get(mission_id)
        entry['revision_after'] = mission['intent_revision']
        mission.setdefault('utterances', []).append(entry)
        self._save(mission)
        out['mission'] = mission
        return out

    def describe(self, mission: dict) -> str:
        if mission['status'] == 'NEEDS_CLARIFICATION':
            return mission.get('ambiguity_reason') or 'I need you to pick the intended contact.'
        states = [f"{a['operation']}={a['status']}" for a in (self.ledger.action(x) for x in mission.get('actions', [])) if a]
        return f"Mission {mission['status']} at revision {mission['intent_revision']}; " + (', '.join(states) or 'no actions planned yet')

    def plan(self, mission_id: str) -> dict:
        mission = self.get(mission_id)
        if not mission['binding']:
            raise MissionError('NEEDS_CLARIFICATION', mission.get('ambiguity_reason', 'no unique entity binding'))
        if mission['status'] == 'CANCELLED':
            raise MissionError('CANCELLED', 'mission was cancelled')
        if any((a := self.ledger.action(x)) and a['operation'] == 'outbox_send' and a['status'] in self.IN_FLIGHT | {'CONFIRMED'}
               for x in mission.get('actions', [])):
            raise MissionError('ALREADY_SENT', 'the send for this revision is confirmed or in flight; start a new mission or correct the intent')
        cid, rev = mission['binding']['contact_id'], mission['intent_revision']
        done_task = next((a for a in (self.ledger.action(x) for x in mission.get('actions', []))
                          if a and a['operation'] == 'crm_create_task' and a['status'] == 'CONFIRMED' and a['target_entity_id'] == cid), None)
        self._supersede_pending(mission, 're-planned')  # an older plan's approval can never authorize the new one
        with self.browser() as b:
            world = self.observe(b)
        reports = [r for r in world['reports'] if r['contact_id'] == cid]
        if not reports:
            raise MissionError('NO_REPORT', f'no report belongs to {cid}')
        latest = max(reports, key=lambda r: r['modified_at'])  # version policy: latest modified
        task = Action(uid('act'), mission_id, rev, 'crm_create_task', cid, 'BrowserExecutor', 'crm', None,
                      {'title': f"Follow up with {mission['person']}", 'op_id': uid('op')}, None, {'task_for_contact': cid}, 'IDEMPOTENT_WRITE', 'moderate')
        send_args = {'to_addr': mission['binding']['email'], 'attachment_id': latest['id'], 'attachment_sha256': latest['sha256'],
                     'body': f"Hi {mission['person'].split()[0]}, attached is the latest report ({latest['title']} {latest['version']}).", 'op_id': uid('op')}
        send = Action(uid('act'), mission_id, rev, 'outbox_send', cid, 'BrowserExecutor', 'outbox', None, send_args, None,
                      {'message_to': send_args['to_addr'], 'sha256': latest['sha256']}, 'NON_IDEMPOTENT', 'consequential')
        fresh = (send,) if done_task else (task, send)  # never repeat a confirmed CRM write on re-plan
        for a in fresh:
            self.ledger.create_action(a)
            self.ledger.transition(a.action_id, 'BOUND', {'binding': mission['binding']})
        task_id = done_task['action_id'] if done_task else task.action_id
        self.ledger.transition(send.action_id, 'NEEDS_APPROVAL', {})
        digest_ = self.action_digest(self.ledger.action(send.action_id))
        mission['actions'] = [task_id, send.action_id]
        mission['report'] = latest
        mission['presented'] = {'action_id': send.action_id, 'digest': digest_,
                                'summary': f"Send {latest['title']} {latest['version']} (sha256 {latest['sha256'][:12]}) to {send_args['to_addr']} — SIMULATED DELIVERY"}
        self._save(mission)
        return {'mission': mission, 'task_action': task_id, 'send_action': send.action_id, 'approval_digest': digest_,
                'approval_summary': mission['presented']['summary']}

    @staticmethod
    def action_digest(action: dict) -> str:
        a = action['arguments']
        return digest({'rev': action['intent_revision'], 'target': action['target_entity_id'], 'to': a['to_addr'],
                       'attachment': a['attachment_id'], 'sha256': a['attachment_sha256'], 'body': a['body']})

    def approve(self, action_id: str, presented_digest: str) -> dict:
        a = self.ledger.action(action_id)
        if not a or a['status'] != 'NEEDS_APPROVAL':
            raise MissionError('NOT_AWAITING_APPROVAL', f"action is {a['status'] if a else 'missing'}")
        mission = self.get(a['mission_id'])
        if 'digest' in self.ablate:
            return self.ledger.transition(action_id, 'AUTHORIZED', {'approval_digest': self.action_digest(a)})
        if a['intent_revision'] != mission['intent_revision']:
            raise MissionError('STALE_REVISION', 'approval refers to a superseded intent revision')
        if presented_digest != self.action_digest(a):
            raise MissionError('DIGEST_MISMATCH', 'approval does not match the exact recipient/content/attachment')
        return self.ledger.transition(action_id, 'AUTHORIZED', {'approval_digest': presented_digest})

    # ---- execution -----------------------------------------------------------
    def run_task(self, action_id: str) -> dict:
        a = self.ledger.action(action_id)
        self._guard_revision(a)
        if a['status'] != 'BOUND':
            raise MissionError('BAD_STATE', f"task action is {a['status']}")
        self.ledger.transition(action_id, 'AUTHORIZED', {'authorized_by': 'policy: moderate-risk reversible CRM write'})
        self.ledger.transition(action_id, 'READY', {})
        self.ledger.transition(action_id, 'DISPATCHING', {'dispatch_attempt': uid('try')})  # persisted BEFORE side effect
        args = a['arguments']
        with self.browser() as b:
            page = b.new_page()
            page.goto(self.urls['crm'])
            dismiss = page.get_by_role('button', name='Dismiss')
            if dismiss.count():
                dismiss.click()
            page.get_by_test_id('contact-row').first.wait_for(timeout=10000)
            page.get_by_label('Contact ID').fill(a['target_entity_id'])
            page.get_by_label('Task title').fill(args['title'])
            page.evaluate("op => document.querySelector('#f [name=op_id]').value = op", args['op_id'])
            page.get_by_role('button', name='Create task').click()
            page.locator('#toast:not(:empty)').wait_for(timeout=10000)
            toast = page.locator('#toast').inner_text()
            page.reload()  # persisted state, not optimistic UI
        self.ledger.transition(action_id, 'DISPATCHED', {'ui_toast': toast})
        return self._verify(action_id, lambda: self.verifier.task(args['op_id'], a['target_entity_id']))

    def run_send(self, action_id: str) -> dict:
        a = self.ledger.action(action_id)
        self._guard_revision(a)
        if a['status'] != 'AUTHORIZED':
            raise MissionError('NOT_AUTHORIZED', f"action is {a['status']}; approval required")
        args = a['arguments']
        # temporal guard: the attachment the human approved must still be the attachment on disk
        live = self.verifier.report_sha(args['attachment_id'])
        if 'temporal_guard' in self.ablate:  # ablation: send whatever is on disk now
            args = {**args, 'attachment_sha256': live}
            a = {**a, 'arguments': args, 'approval_digest': self.action_digest({**a, 'arguments': args})}
        if live != args['attachment_sha256']:
            self.ledger.transition(action_id, 'SUPERSEDED', {'reason': 'attachment changed after approval', 'approved_sha256': args['attachment_sha256'], 'live_sha256': live})
            raise MissionError('ATTACHMENT_CHANGED', 'report changed after review; re-plan and re-approve', approved=args['attachment_sha256'], live=live)
        if a['approval_digest'] != self.action_digest(a):
            self.ledger.transition(action_id, 'SUPERSEDED', {'reason': 'approval digest no longer matches'})
            raise MissionError('DIGEST_MISMATCH', 'approved scope changed')
        try:
            lease = self.leases.acquire(f"{a['mission_id']}:{action_id}", duration_seconds=120, wait_seconds=60)
        except TimeoutError:
            raise MissionError('EXECUTOR_BUSY', 'another mission holds the exclusive executor lease')
        try:
            return self._send_under_lease(a, lease)
        finally:
            self.leases.release(lease)

    def _send_under_lease(self, a: dict, lease) -> dict:
        action_id, args = a['action_id'], a['arguments']
        self.ledger.transition(action_id, 'READY', {'lease_generation': lease.generation})
        if self.hooks.get('before_dispatch'):
            self.hooks['before_dispatch']()  # test seam: lets a test fence this worker mid-flight
        try:
            self.leases.validate(lease)  # fencing token checked immediately before the side effect
        except LeaseLost:
            self.ledger.transition(action_id, 'CANCELLED', {'reason': 'lease lost before dispatch; no side effect issued'})
            raise MissionError('LEASE_LOST', 'executor lease was fenced; re-observe before acting')
        self.ledger.transition(action_id, 'DISPATCHING', {'dispatch_attempt': uid('try'), 'lease_generation': lease.generation})
        ack = 'unknown'
        try:
            with self.browser() as b:
                page = b.new_page()
                page.goto(self.urls['outbox'])
                page.get_by_label('To').fill(args['to_addr'])
                page.get_by_label('Attachment ID').fill(args['attachment_id'])
                page.get_by_label('Attachment SHA-256').fill(args['attachment_sha256'])
                page.get_by_label('Body').fill(args['body'])
                page.evaluate("op => document.querySelector('#f [name=op_id]').value = op", args['op_id'])
                page.get_by_role('button', name='Send (simulated)').click()
                page.locator('#toast:not(:empty)').wait_for(timeout=5000)
                ack = page.locator('#toast').inner_text()
        except Exception as e:  # no acknowledgement observed: the effect is UNKNOWN, not failed
            ack = f'no acknowledgement ({type(e).__name__})'
        if not ack.startswith('Sent:') and 'reconcile' in self.ablate:  # ablation: stateless retry on silence
            retry = {**a, 'arguments': {**args, 'op_id': uid('op')}}
            self.ledger.transition(action_id, 'FAILED', {'ack': ack, 'note': 'ABLATION: treated silence as failure'})
            return self._ablation_resend(retry)
        if not ack.startswith('Sent:'):
            self.ledger.transition(action_id, 'UNCERTAIN', {'ack': ack, 'note': 'sent but cannot confirm acceptance; will not resend automatically'})
            return self.reconcile(action_id)
        self.ledger.transition(action_id, 'DISPATCHED', {'ack': ack})
        return self._verify(action_id, lambda: self.verifier.message(args['op_id'], args['to_addr'], args['attachment_sha256']))

    def _ablation_resend(self, a: dict) -> dict:
        args = a['arguments']
        with self.browser() as b:
            page = b.new_page()
            page.goto(self.urls['outbox'])
            for label, key in (('To', 'to_addr'), ('Attachment ID', 'attachment_id'), ('Attachment SHA-256', 'attachment_sha256'), ('Body', 'body')):
                page.get_by_label(label).fill(args[key])
            page.evaluate("op => document.querySelector('#f [name=op_id]').value = op", args['op_id'])
            page.get_by_role('button', name='Send (simulated)').click()
            page.locator('#toast:not(:empty)').wait_for(timeout=5000)
        return {**self.ledger.action(a['action_id']), 'status': 'CONFIRMED', 'note': 'ABLATION: claimed after blind resend'}

    def reconcile(self, action_id: str) -> dict:
        """Read-only. Never re-dispatches."""
        a = self.ledger.action(action_id)
        if a['status'] != 'UNCERTAIN':
            return a
        args = a['arguments']
        fn = (lambda: self.verifier.message(args['op_id'], args['to_addr'], args['attachment_sha256'])) if a['operation'] == 'outbox_send' \
            else (lambda: self.verifier.task(args['op_id'], a['target_entity_id']))
        result = fn()
        self.ledger.transition(action_id, 'OBSERVING', {'verification': result})
        if result['status'] == 'FAILED':
            # absence of a record is only definitive after a bounded wait; hold for a human instead of resending
            self.ledger.transition(action_id, 'UNCERTAIN', {'note': 'no matching effect found; NEEDS_HUMAN_DECISION', 'verification': result})
            return self.ledger.action(action_id)
        self.ledger.transition(action_id, result['status'], {'verification': result, 'evidence_references': [str(result['evidence'])]})
        return self.ledger.action(action_id)

    def _verify(self, action_id: str, check) -> dict:
        self.ledger.transition(action_id, 'OBSERVING', {})
        if 'verifier' in self.ablate:  # ablation: believe the UI
            toast = self.ledger.action(action_id).get('ui_toast') or self.ledger.action(action_id).get('ack') or ''
            ok = toast.startswith(('Task created', 'Sent:'))
            return self.ledger.transition(action_id, 'CONFIRMED' if ok else 'FAILED', {'verification': {'status': 'UI_CLAIM', 'evidence': toast}})
        result = check()
        state = result['status'] if result['status'] in {'CONFIRMED', 'FAILED', 'WRONG_TARGET', 'UNCERTAIN'} else 'UNCERTAIN'
        return self.ledger.transition(action_id, state, {'verification': result, 'verification_status': state, 'evidence_references': [str(result['evidence'])]})

    def _guard_revision(self, action: dict) -> None:
        mission = self.get(action['mission_id'])
        if 'digest' in self.ablate and action['status'] != 'SUPERSEDED':
            return
        if mission['intent_revision'] != action['intent_revision'] or action['status'] == 'SUPERSEDED':
            raise MissionError('SUPERSEDED', 'action belongs to a superseded intent revision')
