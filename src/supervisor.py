"""Supervisor: tool-bounded agent roles over one mission DAG.

Every role is a deterministic Python worker with its own capability grant; a role
that calls a tool outside its grant fails with PermissionError. Read-only roles
fan out in parallel; the Planner is gated on entity resolution; the verifier is a
different worker (and verifier instance) from the executor that wrote.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent_runtime import AgentMessage, MissionScheduler, TaskContext, TaskResult, TaskSpec
from resolve_core import now, uid
from web_mission import MissionError, WebMissionRunner, WebVerifier

# role -> (granted tools, read_only)
ROLES: dict[str, tuple[frozenset[str], bool]] = {
    'IntentInterpreter': (frozenset({'intent.parse'}), True),
    'EntityResolver': (frozenset({'crm.read'}), True),
    'Researcher': (frozenset({'docs.read'}), True),
    'FileWorker': (frozenset({'fs.read'}), True),
    'OSObserver': (frozenset({'os.observe'}), True),
    'Planner': (frozenset({'browser.read', 'ledger.write'}), False),
    'SafetyApproval': (frozenset({'policy.check'}), True),
    'BrowserExecutor': (frozenset({'browser.write', 'ledger.write'}), False),
    'DesktopExecutor': (frozenset({'desktop.write'}), False),
    'IndependentVerifier': (frozenset({'db.read', 'ledger.write'}), True),
    'Recovery': (frozenset({'db.read', 'ledger.write'}), False),
    'SkillCompiler': (frozenset({'browser.write', 'skills.write'}), False),
    'PerformanceOptimizer': (frozenset({'metrics.read'}), True),
}

RISK = {'crm_create_task': 'LOCAL_REVERSIBLE', 'outbox_send': 'EXTERNAL_COMMUNICATION'}
ALLOWED_OPERATIONS = set(RISK)


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


class Supervisor:
    def __init__(self, runner: WebMissionRunner, fixture_root: Path, max_workers: int = 6):
        self.runner = runner
        self.ledger = runner.ledger
        self.root = Path(fixture_root)
        self.board: dict[str, dict] = {}  # task_id -> result, readable by dependents
        self.hooks: dict[str, Callable[[], None]] = {}  # test seams: role name -> callable run at role start
        self.scheduler = MissionScheduler(self.ledger.path, max_workers=max_workers, revision_check=self._revision_ok)
        self.verifier = WebVerifier(self.root)  # separate instance from the runner's own checker
        self.skills = None  # optional SkillLibrary, wired by the API layer
        for role in ROLES:
            self.scheduler.register(role, self._wrap(role, getattr(self, f'_role_{role}')))

    # ---- plumbing -------------------------------------------------------------
    def _revision_ok(self, mission_id: str, revision: int) -> bool:
        m = self.ledger.get_json('missions', mission_id)
        return bool(m) and m['intent_revision'] == revision and m['status'] != 'CANCELLED'

    def _wrap(self, role: str, fn):
        def run(msg: AgentMessage, ctx: TaskContext):
            if role in self.hooks:
                self.hooks[role]()
            out = fn(msg, ctx)
            out = {**out, 'tool_calls': ctx.tools_used, 'tools_granted': sorted(ctx.capabilities)}
            self.board[msg.task_id] = out
            return out
        return run

    def _spec(self, mission: dict, role: str, op: str, deps=(), budget: float = 60.0, after=None, **args) -> TaskSpec:
        """deps: whose results this task reads; after: scheduler ordering within this run (defaults to deps)."""
        caps, ro = ROLES[role]
        args['deps'] = tuple(deps)
        tid = f"{mission['mission_id']}:{op}:{uid('t')[-6:]}"
        return TaskSpec(tid, mission['mission_id'], mission['intent_revision'], role, op, args, tuple(deps if after is None else after), caps, ro,
                        'cursor' if role == 'DesktopExecutor' else None, 20, budget)

    def _dep(self, msg: AgentMessage, op: str) -> dict:
        for tid, out in self.board.items():
            if tid.startswith(f'{msg.mission_id}:{op}:') and tid in msg.arguments.get('deps', ()):
                return out
        raise KeyError(f'dependency result {op} unavailable')

    @staticmethod
    def _summ(results: dict[str, TaskResult]) -> list[dict]:
        return [{'task_id': r.task_id, 'agent_id': r.agent_id, 'status': r.status, 'started_at': r.started_at,
                 'finished_at': r.finished_at, 'error': r.error, 'result': r.result} for r in results.values()]

    def agents(self) -> list[dict]:
        return [{'role': r, 'tools': sorted(c), 'read_only': ro} for r, (c, ro) in ROLES.items()]

    # ---- DAGs -----------------------------------------------------------------
    def investigate(self, mission_id: str, budgets: dict[str, float] | None = None) -> dict:
        """Fan-out read-only research, retry timed-out reads once, then gated planning + safety review."""
        budgets = budgets or {}
        m = self.runner.get(mission_id)
        intent = self._spec(m, 'IntentInterpreter', 'intent')
        reads = [self._spec(m, role, op, (intent.task_id,), budgets.get(role, 60.0))
                 for role, op in (('EntityResolver', 'entities'), ('Researcher', 'reports'), ('FileWorker', 'files'), ('OSObserver', 'os'))]
        stage1 = self.scheduler.run([intent, *reads])
        retried = []
        for spec in reads:  # Recovery policy: read-only work may be retried once; writes never are
            if stage1[spec.task_id].status == 'TIMED_OUT':
                again = self._spec(m, spec.agent_id, spec.operation, (), 60.0)
                res = self.scheduler.run([again])[again.task_id]
                stage1[again.task_id] = res
                retried.append({'original': spec.task_id, 'retry': again.task_id, 'status': res.status})
                if res.status == 'SUCCEEDED':
                    reads[reads.index(spec)] = again
        ok = {s.operation: s.task_id for s in reads if stage1[s.task_id].status == 'SUCCEEDED'}
        m = self.runner.get(mission_id)
        if any(r.status == 'SUPERSEDED' for r in stage1.values()) or len(ok) < 4:
            return {'mission_id': mission_id, 'tasks': self._summ(stage1), 'retried': retried, 'planned': False,
                    'reason': 'research incomplete or superseded by a newer intent revision'}
        deps = tuple(ok[k] for k in ('entities', 'reports', 'files'))
        plan = self._spec(m, 'Planner', 'plan', deps, after=())  # inputs come from stage 1
        safety = self._spec(m, 'SafetyApproval', 'safety', (plan.task_id,))
        stage2 = self.scheduler.run([plan, safety])
        tasks = {**stage1, **stage2}
        return {'mission_id': mission_id, 'tasks': self._summ(tasks), 'retried': retried,
                'planned': stage2[plan.task_id].status == 'SUCCEEDED', 'safety': self.board.get(safety.task_id)}

    def execute(self, mission_id: str) -> dict:
        m = self.runner.get(mission_id)
        ex = self._spec(m, 'BrowserExecutor', 'execute', budget=120.0)
        rec = self._spec(m, 'Recovery', 'recover', (ex.task_id,))
        ver = self._spec(m, 'IndependentVerifier', 'verify', (rec.task_id,))
        perf = self._spec(m, 'PerformanceOptimizer', 'perf', (ver.task_id,))
        res = self.scheduler.run([ex, rec, ver, perf])
        return {'mission_id': mission_id, 'tasks': self._summ(res), 'verification': self.board.get(ver.task_id)}

    # ---- roles ----------------------------------------------------------------
    def _role_IntentInterpreter(self, msg, ctx):
        ctx.use('intent.parse')
        m = self.runner.get(msg.mission_id)
        return {'person': m['person'], 'wants_send': m['wants_send'], 'version_policy': 'latest modified_at among the bound contact\'s reports',
                'revision': m['intent_revision'], 'provider': 'deterministic-template'}

    def _role_EntityResolver(self, msg, ctx):
        ctx.use('crm.read')
        m = self.runner.get(msg.mission_id)
        contacts = _get(self.runner.urls['crm'] + '/api/contacts')
        cands = [c for c in contacts if c['name'].casefold() == m['person'].casefold()]
        return {'candidates': cands, 'unique': len(cands) == 1, 'binding': m.get('binding'),
                'source': 'crm /api/contacts', 'evidence': [c['id'] for c in cands]}

    def _role_Researcher(self, msg, ctx):
        ctx.use('docs.read')
        reports = _get(self.runner.urls['docs'] + '/api/reports')
        by_contact: dict[str, dict] = {}
        for r in reports:
            if r['contact_id'] not in by_contact or r['modified_at'] > by_contact[r['contact_id']]['modified_at']:
                by_contact[r['contact_id']] = r
        return {'reports': reports, 'latest_by_contact': by_contact, 'source': 'docs /api/reports'}

    def _role_FileWorker(self, msg, ctx):
        """Corroborates the report store's metadata against the bytes on disk (independent of the docs app API)."""
        ctx.use('fs.read')
        files = {}
        for p in sorted((self.root / 'files').glob('*.txt')):
            files[p.stem] = hashlib.sha256(p.read_bytes()).hexdigest()
        return {'disk_sha256': files, 'source': str(self.root / 'files')}

    def _role_OSObserver(self, msg, ctx):
        ctx.use('os.observe')
        import windows_adapter
        try:
            state = windows_adapter.list_windows()
            return {'available': True, 'windows': len(state.get('windows', []))}
        except Exception as exc:  # honest: no desktop here
            return {'available': False, 'reason': f'{type(exc).__name__}: {exc}', 'note': 'WINDOWS_NATIVE_TESTS_NOT_RUN'}

    def _role_Planner(self, msg, ctx):
        ents, research, files = (self._dep(msg, op) for op in ('entities', 'reports', 'files'))
        m = self.runner.get(msg.mission_id)
        if not m.get('binding'):
            raise MissionError('NEEDS_CLARIFICATION', f"{len(ents['candidates'])} candidates; refusing to choose: "
                               + '; '.join(f"{c['id']} {c['dept']}/{c['org']}" for c in ents['candidates']))
        cid = m['binding']['contact_id']
        latest = research['latest_by_contact'].get(cid)
        if not latest:
            raise MissionError('NO_REPORT', f'no report for {cid}')
        disk = files['disk_sha256'].get(latest['id'])
        if disk != latest['sha256']:  # two independent sources disagree about the version
            raise MissionError('VERSION_CONFLICT', f"docs app says {latest['sha256'][:12]}, disk says {(disk or 'missing')[:12]}")
        ctx.use('browser.read')
        ctx.use('ledger.write')
        plan = self.runner.plan(msg.mission_id)
        if plan['mission']['report']['sha256'] != disk:
            raise MissionError('VERSION_CONFLICT', 'report changed while planning')
        return {'task_action': plan['task_action'], 'send_action': plan['send_action'], 'approval_digest': plan['approval_digest'],
                'summary': plan['approval_summary'], 'corroborated_sha256': disk}

    def _role_SafetyApproval(self, msg, ctx):
        ctx.use('policy.check')
        plan = self._dep(msg, 'plan')
        verdicts = []
        for aid in (plan['task_action'], plan['send_action']):
            a = self.ledger.action(aid)
            if a['operation'] not in ALLOWED_OPERATIONS:
                verdicts.append({'action_id': aid, 'verdict': 'DENY', 'reason': 'operation not allowlisted'})
                continue
            risk = RISK[a['operation']]
            verdicts.append({'action_id': aid, 'risk': risk, 'verdict': 'REQUIRES_HUMAN_APPROVAL' if risk != 'LOCAL_REVERSIBLE' else 'AUTO_ALLOWED',
                             'status': a['status']})
        return {'verdicts': verdicts, 'spoken_summary': plan['summary'] + '. Say "approve" to authorise exactly this.'}

    def _role_BrowserExecutor(self, msg, ctx):
        m = self.runner.get(msg.mission_id)
        done = []
        for aid in m.get('actions', []):
            a = self.ledger.action(aid)
            ctx.check_active()
            try:
                if a['operation'] == 'crm_create_task' and a['status'] == 'BOUND':
                    ctx.use('browser.write')
                    done.append({'action_id': aid, 'status': self.runner.run_task(aid)['status']})
                elif a['operation'] == 'outbox_send' and a['status'] == 'AUTHORIZED':
                    ctx.use('browser.write')
                    done.append({'action_id': aid, 'status': self.runner.run_send(aid)['status']})
                else:
                    done.append({'action_id': aid, 'status': a['status'], 'skipped': 'not executable in this state (approval pending or already terminal)'})
            except MissionError as exc:
                done.append({'action_id': aid, 'refused': exc.code, 'reason': str(exc)})
        return {'executed': done, 'note': 'Playwright DOM actions; not physical cursor input'}

    def _role_DesktopExecutor(self, msg, ctx):
        lease = ctx.cursor_token()
        ctx.use('desktop.write')
        import windows_adapter
        if not windows_adapter.IS_WINDOWS:
            raise windows_adapter.UnsupportedDesktop('WINDOWS_NATIVE_TESTS_NOT_RUN: no Windows desktop on this host')
        return {'lease_generation': lease.generation}

    def _role_Recovery(self, msg, ctx):
        m = self.runner.get(msg.mission_id)
        out = []
        for aid in m.get('actions', []):
            if self.ledger.action(aid)['status'] == 'UNCERTAIN':
                ctx.use('db.read')
                ctx.use('ledger.write')
                out.append({'action_id': aid, 'status': self.runner.reconcile(aid)['status'], 'policy': 'read-only reconcile; never re-dispatch'})
        return {'reconciled': out}

    def _role_IndependentVerifier(self, msg, ctx):
        m = self.runner.get(msg.mission_id)
        claims = []
        for aid in m.get('actions', []):
            a = self.ledger.action(aid)
            if a['status'] in {'PROPOSED', 'BOUND', 'NEEDS_APPROVAL', 'AUTHORIZED', 'READY', 'SUPERSEDED', 'CANCELLED'}:
                continue
            ctx.use('db.read')
            args = a['arguments']
            res = self.verifier.task(args['op_id'], a['target_entity_id']) if a['operation'] == 'crm_create_task' \
                else self.verifier.message(args['op_id'], args['to_addr'], args['attachment_sha256'])
            claim = {'claim_id': uid('claim'), 'subject': aid, 'expected_predicate': a['expected_effect'], 'result': res['status'],
                     'independent_evidence_refs': [json.dumps(res['evidence'], sort_keys=True)], 'verifier_id': 'IndependentVerifier',
                     'executor_id': a['actor_agent_id'], 'ledger_state': a['status'], 'agrees_with_ledger': res['status'] == a['status'], 'checked_at': now()}
            ctx.use('ledger.write')
            self.ledger.write_json('missions', f"claim:{claim['claim_id']}", {**claim, 'mission_id': msg.mission_id, 'kind': 'verification_claim'})
            claims.append(claim)
        return {'claims': claims, 'all_agree': all(c['agrees_with_ledger'] for c in claims)}

    def _role_SkillCompiler(self, msg, ctx):
        if self.skills is None:
            raise RuntimeError('skill library not configured')
        ctx.use('skills.write')
        ctx.use('browser.write')
        m = self.runner.get(msg.mission_id)
        return {'skill': self.skills.record(self.runner.urls, self.root, m['binding']['contact_id'], msg.arguments.get('title', f"Follow up with {m['person']}"))}

    def _role_PerformanceOptimizer(self, msg, ctx):
        ctx.use('metrics.read')
        rows = [e for e in self.scheduler.events(msg.mission_id)]
        spans: dict[str, list[float]] = {}
        for e in rows:
            p = json.loads(e['payload'])
            if p.get('started_at') and p.get('finished_at'):
                spans[e['task_id']] = [p['started_at'], p['finished_at'], p.get('agent_id')]
        points = sorted([(s, 1) for s, f, _ in spans.values()] + [(f, -1) for s, f, _ in spans.values()])
        cur = peak = 0
        for _, d in points:
            cur += d
            peak = max(peak, cur)
        durations = {tid: round(f - s, 3) for tid, (s, f, _) in spans.items()}
        slowest = max(durations.items(), key=lambda kv: kv[1]) if durations else None
        return {'tasks_measured': len(durations), 'peak_parallelism': peak, 'durations_s': durations,
                'slowest': slowest, 'llm_calls': 0,
                'recommendation': 'browser launch dominates; reuse one browser context per mission' if slowest and 'execute' in slowest[0] else 'no action'}
