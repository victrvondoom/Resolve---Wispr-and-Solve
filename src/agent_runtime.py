"""Typed mission DAG and specialist runtime for RESOLVE.

Specialists are registered Python callables. Read-only tasks may run concurrently;
cursor tasks acquire a durable SQLite lease with a fencing generation.
Cancellation is cooperative because Python cannot safely terminate a running thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


class Cancelled(RuntimeError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=3, factory=_ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    mission_id: str
    intent_revision: int
    agent_id: str
    operation: str
    arguments: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    read_only: bool = True
    resource: str | None = None
    tool_budget: int = 20
    time_budget_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not all((self.task_id, self.mission_id, self.agent_id, self.operation)):
            raise ValueError('task, mission, agent, and operation are required')
        if self.intent_revision < 1 or self.tool_budget < 0 or self.time_budget_seconds <= 0:
            raise ValueError('invalid task revision or budget')
        if self.read_only and self.resource == 'cursor':
            raise ValueError('read-only task cannot acquire the cursor')
        if not self.read_only and self.resource == 'cursor' and 'desktop.write' not in self.capabilities:
            raise ValueError('cursor task requires desktop.write capability')


@dataclass(frozen=True)
class AgentMessage:
    task_id: str
    mission_id: str
    intent_revision: int
    operation: str
    arguments: dict[str, Any]
    granted_capabilities: frozenset[str]
    tool_budget: int
    time_budget_seconds: float


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    agent_id: str
    status: str
    result: dict[str, Any] = field(default_factory=dict)
    evidence_references: tuple[str, ...] = ()
    confidence: float | None = None
    blockers: tuple[str, ...] = ()
    suggested_next_action: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        if self.status not in {'SUCCEEDED', 'FAILED', 'CANCELLED', 'BLOCKED', 'TIMED_OUT', 'LEASE_LOST', 'SUPERSEDED'}:
            raise ValueError('unknown task result status')
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError('confidence must be between zero and one')


@dataclass(frozen=True)
class CursorLease:
    owner: str
    generation: int
    expires_at: float


class CursorLeaseManager:
    """Cross-process cursor ownership through SQLite compare-and-swap transactions."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cursor_lease(resource TEXT PRIMARY KEY, owner TEXT, generation INTEGER NOT NULL, expires_at REAL NOT NULL)')
            db.execute("INSERT OR IGNORE INTO cursor_lease VALUES('desktop',NULL,0,0)")

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    def acquire(self, owner: str, duration_seconds: float = 5.0, wait_seconds: float = 5.0, stop: threading.Event | None = None) -> CursorLease:
        if not owner or duration_seconds <= 0 or wait_seconds < 0:
            raise ValueError('invalid cursor lease request')
        deadline = time.monotonic() + wait_seconds
        while True:
            if stop and stop.is_set():
                raise Cancelled('cursor acquisition cancelled')
            now = time.time()
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute("SELECT * FROM cursor_lease WHERE resource='desktop'").fetchone()
                if row['owner'] is None or row['expires_at'] <= now:
                    generation = row['generation'] + 1
                    expires = now + duration_seconds
                    db.execute("UPDATE cursor_lease SET owner=?,generation=?,expires_at=? WHERE resource='desktop'", (owner, generation, expires))
                    return CursorLease(owner, generation, expires)
            if time.monotonic() >= deadline:
                raise TimeoutError('desktop cursor is owned by another task')
            time.sleep(min(0.025, max(0.001, deadline - time.monotonic())))

    def validate(self, lease: CursorLease) -> None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cursor_lease WHERE resource='desktop'").fetchone()
        if row['owner'] != lease.owner or row['generation'] != lease.generation or row['expires_at'] <= time.time():
            raise LeaseLost('cursor lease expired or was fenced')

    def renew(self, lease: CursorLease, duration_seconds: float = 5.0) -> CursorLease:
        if duration_seconds <= 0:
            raise ValueError('lease duration must be positive')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM cursor_lease WHERE resource='desktop'").fetchone()
            if row['owner'] != lease.owner or row['generation'] != lease.generation or row['expires_at'] <= time.time():
                raise LeaseLost('cursor lease expired or was fenced')
            expires = time.time() + duration_seconds
            db.execute("UPDATE cursor_lease SET expires_at=? WHERE resource='desktop'", (expires,))
            return CursorLease(lease.owner, lease.generation, expires)

    def release(self, lease: CursorLease) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE cursor_lease SET owner=NULL,expires_at=0 WHERE resource='desktop' AND owner=? AND generation=?", (lease.owner, lease.generation))
            return cur.rowcount == 1

    def revoke(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE cursor_lease SET owner=NULL,expires_at=0,generation=generation+1 WHERE resource='desktop'")


class TaskContext:
    def __init__(self, stop: threading.Event, deadline: float, capabilities: frozenset[str], tool_budget: int, leases: CursorLeaseManager, lease: CursorLease | None):
        self.stop = stop
        self.deadline = deadline
        self.capabilities = capabilities
        self.tool_budget = tool_budget
        self.leases = leases
        self.lease = lease
        self.tools_used = 0

    def check_active(self) -> None:
        if self.stop.is_set():
            raise Cancelled('task cancelled')
        if time.monotonic() >= self.deadline:
            raise DeadlineExceeded('task time budget exceeded')
        if self.lease:
            self.leases.validate(self.lease)

    def use(self, capability: str) -> None:
        self.check_active()
        if capability not in self.capabilities:
            raise PermissionError(f'capability denied: {capability}')
        if self.tools_used >= self.tool_budget:
            raise RuntimeError('tool budget exhausted')
        self.tools_used += 1

    def cursor_token(self) -> CursorLease:
        self.check_active()
        if self.lease is None:
            raise PermissionError('task does not own desktop cursor')
        return self.lease


Specialist = Callable[[AgentMessage, TaskContext], TaskResult | dict[str, Any]]


class MissionScheduler:
    def __init__(self, db_path: str | Path, max_workers: int = 4, revision_check: Callable[[str, int], bool] | None = None):
        if max_workers < 1:
            raise ValueError('max_workers must be positive')
        self.db_path = str(db_path)
        self.max_workers = max_workers
        self.revision_check = revision_check
        self.leases = CursorLeaseManager(db_path)
        self.specialists: dict[str, Specialist] = {}
        self._stop = threading.Event()
        with _connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS task_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, mission_id TEXT NOT NULL, status TEXT NOT NULL, at REAL NOT NULL, payload TEXT NOT NULL)')

    def register(self, agent_id: str, specialist: Specialist) -> None:
        if not agent_id or not callable(specialist):
            raise ValueError('agent ID and callable are required')
        self.specialists[agent_id] = specialist

    def cancel(self) -> None:
        self._stop.set()
        self.leases.revoke()

    def reset_cancellation(self) -> None:
        self._stop.clear()

    def events(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.db_path) as db:
            rows = db.execute('SELECT * FROM task_events WHERE (? IS NULL OR mission_id=?) ORDER BY seq', (mission_id, mission_id)).fetchall()
        return [dict(row) for row in rows]

    def _record(self, spec: TaskSpec, status: str, payload: dict[str, Any]) -> None:
        with _connect(self.db_path) as db:
            db.execute('INSERT INTO task_events(task_id,mission_id,status,at,payload) VALUES(?,?,?,?,?)', (spec.task_id, spec.mission_id, status, time.time(), json.dumps(payload, default=str)))

    @staticmethod
    def _validate_graph(specs: list[TaskSpec]) -> dict[str, TaskSpec]:
        tasks = {spec.task_id: spec for spec in specs}
        if len(tasks) != len(specs):
            raise ValueError('duplicate task ID')
        for spec in specs:
            if any(dep not in tasks for dep in spec.dependencies):
                raise ValueError(f'unknown dependency for {spec.task_id}')
            if any(tasks[dep].mission_id != spec.mission_id for dep in spec.dependencies):
                raise ValueError('dependencies cannot cross missions')
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(task_id: str):
            if task_id in visiting:
                raise ValueError('task graph contains a cycle')
            if task_id in visited:
                return
            visiting.add(task_id)
            for dep in tasks[task_id].dependencies:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)
        for task_id in tasks:
            visit(task_id)
        return tasks

    def _run_task(self, spec: TaskSpec) -> TaskResult:
        started = time.time()
        self._record(spec, 'RUNNING', {'agent_id': spec.agent_id, 'operation': spec.operation})
        lease: CursorLease | None = None
        try:
            if self._stop.is_set():
                raise Cancelled('mission cancelled')
            if self.revision_check and not self.revision_check(spec.mission_id, spec.intent_revision):
                return TaskResult(spec.task_id, spec.agent_id, 'SUPERSEDED', error='mission revision changed', started_at=started, finished_at=time.time())
            deadline = time.monotonic() + spec.time_budget_seconds
            if spec.resource == 'cursor':
                lease = self.leases.acquire(spec.task_id, duration_seconds=min(5.0, spec.time_budget_seconds), wait_seconds=spec.time_budget_seconds, stop=self._stop)
            context = TaskContext(self._stop, deadline, spec.capabilities, spec.tool_budget, self.leases, lease)
            context.check_active()
            message = AgentMessage(spec.task_id, spec.mission_id, spec.intent_revision, spec.operation, spec.arguments, spec.capabilities, spec.tool_budget, spec.time_budget_seconds)
            raw = self.specialists[spec.agent_id](message, context)
            context.check_active()
            if self.revision_check and not self.revision_check(spec.mission_id, spec.intent_revision):
                result = TaskResult(spec.task_id, spec.agent_id, 'SUPERSEDED', error='mission revision changed', started_at=started, finished_at=time.time())
            elif isinstance(raw, TaskResult):
                if raw.task_id != spec.task_id or raw.agent_id != spec.agent_id:
                    raise ValueError('specialist result identity mismatch')
                result = raw
            elif isinstance(raw, dict):
                result = TaskResult(spec.task_id, spec.agent_id, 'SUCCEEDED', result=raw, started_at=started, finished_at=time.time())
            else:
                raise TypeError('specialist must return TaskResult or dict')
        except Cancelled as exc:
            result = TaskResult(spec.task_id, spec.agent_id, 'CANCELLED', error=str(exc), started_at=started, finished_at=time.time())
        except DeadlineExceeded as exc:
            result = TaskResult(spec.task_id, spec.agent_id, 'TIMED_OUT', error=str(exc), started_at=started, finished_at=time.time())
        except LeaseLost as exc:
            result = TaskResult(spec.task_id, spec.agent_id, 'LEASE_LOST', error=str(exc), started_at=started, finished_at=time.time())
        except TimeoutError as exc:
            result = TaskResult(spec.task_id, spec.agent_id, 'TIMED_OUT', error=str(exc), started_at=started, finished_at=time.time())
        except Exception as exc:
            result = TaskResult(spec.task_id, spec.agent_id, 'FAILED', error=f'{type(exc).__name__}: {exc}', started_at=started, finished_at=time.time())
        finally:
            if lease is not None:
                self.leases.release(lease)
        self._record(spec, result.status, asdict(result))
        return result

    def run(self, specs: list[TaskSpec]) -> dict[str, TaskResult]:
        tasks = self._validate_graph(specs)
        if not specs:
            return {}
        if len({spec.mission_id for spec in specs}) != 1:
            raise ValueError('one scheduler run handles one mission')
        for spec in specs:
            if spec.agent_id not in self.specialists:
                raise ValueError(f'no specialist registered for {spec.agent_id}')
            self._record(spec, 'PENDING', {'dependencies': spec.dependencies, 'agent_id': spec.agent_id})
        pending = set(tasks)
        results: dict[str, TaskResult] = {}
        in_flight = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix='resolve-agent') as executor:
            while pending or in_flight:
                for task_id in list(pending):
                    spec = tasks[task_id]
                    failed = [d for d in spec.dependencies if d in results and results[d].status != 'SUCCEEDED']
                    if failed:
                        result = TaskResult(task_id, spec.agent_id, 'BLOCKED', blockers=tuple(failed), error='dependency did not succeed', finished_at=time.time())
                        results[task_id] = result
                        self._record(spec, result.status, asdict(result))
                        pending.remove(task_id)
                    elif all(dep in results for dep in spec.dependencies):
                        in_flight[executor.submit(self._run_task, spec)] = task_id
                        pending.remove(task_id)
                if in_flight:
                    done, _ = wait(tuple(in_flight), return_when=FIRST_COMPLETED)
                    for future in done:
                        task_id = in_flight.pop(future)
                        results[task_id] = future.result()
        return results


def register_demo_readers(scheduler: MissionScheduler, ledger: Any) -> None:
    """Register two real, independent read-only specialists over local demo state."""
    def contact_reader(message: AgentMessage, context: TaskContext) -> dict[str, Any]:
        context.use('entities.read')
        rows = [row for row in ledger.all_json('entities') if row['kind'] == 'contact' and 'Alex' in row['display_name']]
        context.check_active()
        return {'candidates': rows, 'ambiguous': len(rows) != 1}

    def report_reader(message: AgentMessage, context: TaskContext) -> dict[str, Any]:
        context.use('entities.read')
        rows = [row for row in ledger.all_json('entities') if row['kind'] == 'document' and row.get('context') == message.arguments.get('context')]
        latest = max(rows, key=lambda row: row.get('modified_at') or '') if rows else None
        context.check_active()
        return {'candidates': rows, 'latest': latest, 'version_policy': 'latest modified timestamp'}

    scheduler.register('contact-resolver', contact_reader)
    scheduler.register('report-resolver', report_reader)
