"""Small, durable RESOLVE kernel. No third-party dependencies."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Entity:
    entity_id: str
    display_name: str
    kind: str
    stable_id: str | None
    app_id: str
    version: str | None = None
    modified_at: str | None = None
    context: str | None = None

    def __post_init__(self):
        if not self.entity_id or not self.display_name or not self.kind or not self.app_id:
            raise ValueError('entity identity, display name, kind, and application are required')


@dataclass
class IntentContract:
    mission_id: str
    goal: str
    intent_revision: int
    original_request: str
    constraints: dict[str, Any]
    target_entities: list[str]
    permitted_actions: list[str]
    forbidden_actions: list[str]
    risk_level: str
    status: str
    created_at: str
    updated_at: str
    required_applications: list[str] = field(default_factory=list)
    approval_requirements: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    expected_effects: list[str] = field(default_factory=list)
    verification_requirements: list[str] = field(default_factory=list)
    deadline_or_budget: dict[str, Any] | None = None

    def __post_init__(self):
        if not self.mission_id or not self.goal or not self.original_request:
            raise ValueError('mission ID, goal, and original request are required')
        if self.intent_revision < 1:
            raise ValueError('intent revision must be positive')
        if self.risk_level not in {'low', 'moderate', 'consequential', 'high'}:
            raise ValueError('unknown mission risk level')
        if self.status not in {'ACTIVE', 'PAUSED', 'NEEDS_CLARIFICATION', 'COMPLETED', 'CANCELLED', 'FAILED'}:
            raise ValueError('unknown mission status')


@dataclass
class Action:
    action_id: str
    mission_id: str
    intent_revision: int
    operation: str
    target_entity_id: str
    actor_agent_id: str
    application_id: str
    window_identity: dict[str, Any] | None
    arguments: dict[str, Any]
    precondition_snapshot_id: str | None
    expected_effect: dict[str, Any]
    idempotency_class: str
    risk_class: str
    status: str = "PROPOSED"
    approval_digest: str | None = None
    verification_status: str = 'PENDING'
    evidence_references: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.intent_revision < 1 or not self.action_id or not self.mission_id or not self.target_entity_id:
            raise ValueError('action identity, target, and positive intent revision are required')
        if self.idempotency_class not in {'READ_ONLY','IDEMPOTENT_WRITE','CONDITIONALLY_IDEMPOTENT','NON_IDEMPOTENT','UNKNOWN'}:
            raise ValueError('unknown idempotency class')
        if self.risk_class not in {'low','moderate','consequential','high'}:
            raise ValueError('unknown action risk class')


ALLOWED = {
    "PROPOSED": {"BOUND", "CANCELLED", "SUPERSEDED"},
    "BOUND": {"AUTHORIZED", "NEEDS_APPROVAL", "CANCELLED", "SUPERSEDED"},
    "NEEDS_APPROVAL": {"AUTHORIZED", "CANCELLED", "SUPERSEDED"},
    "AUTHORIZED": {"READY", "CANCELLED", "SUPERSEDED"},
    "READY": {"DISPATCHING", "CANCELLED", "SUPERSEDED"},
    "DISPATCHING": {"DISPATCHED", "FAILED", "UNCERTAIN"},
    "DISPATCHED": {"OBSERVING", "UNCERTAIN"},
    "OBSERVING": {"CONFIRMED", "FAILED", "UNCERTAIN", "WRONG_TARGET"},
    "UNCERTAIN": {"OBSERVING", "CANCELLED"},
}


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS missions(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS mission_revisions(mission_id TEXT NOT NULL, revision INTEGER NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(mission_id,revision))")
            db.execute("CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, data TEXT NOT NULL, state TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, action_id TEXT, state TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, recipient_id TEXT, attachment_id TEXT, body TEXT, sent_at TEXT)")
            columns = {row['name'] for row in db.execute("PRAGMA table_info(outbox)").fetchall()}
            if 'action_id' not in columns:
                db.execute("ALTER TABLE outbox ADD COLUMN action_id TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_action_id_unique ON outbox(action_id) WHERE action_id IS NOT NULL")
            db.execute("CREATE TABLE IF NOT EXISTS followups(id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, status TEXT NOT NULL, last_message_id TEXT, updated_at TEXT)")
            # DISPATCHING without a terminal result crosses the crash window.
            # Preserve recovery as a ledger event as well as an action projection update.
            unresolved = db.execute("SELECT id,data FROM actions WHERE state='DISPATCHING'").fetchall()
            for row in unresolved:
                action_data = json.loads(row['data']); action_data['status'] = 'UNCERTAIN'
                db.execute("UPDATE actions SET state='UNCERTAIN',data=? WHERE id=?", (json.dumps(action_data),row['id']))
                self._event(db,row['id'],'UNCERTAIN',action_data)

    def connect(self) -> sqlite3.Connection:
        class ClosingConnection(sqlite3.Connection):
            def __exit__(self, exc_type, exc, tb):
                try:
                    return super().__exit__(exc_type, exc, tb)
                finally:
                    self.close()
        db = sqlite3.connect(self.path, timeout=5, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def write_json(self, table: str, key: str, data: dict[str, Any]) -> None:
        if table not in ('missions', 'entities'):
            raise ValueError('invalid table')
        with self.lock, self.connect() as db:
            db.execute(f"INSERT OR REPLACE INTO {table}(id,data) VALUES(?,?)", (key, json.dumps(data)))

    def get_json(self, table: str, key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row['data']) if row else None

    def all_json(self, table: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(f"SELECT data FROM {table}").fetchall()
        return [json.loads(r['data']) for r in rows]

    def record_mission_revision(self, mission: dict[str, Any]) -> None:
        with self.lock, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO mission_revisions VALUES(?,?,?,?)", (mission['mission_id'],mission['intent_revision'],now(),json.dumps(mission)))

    def mission_history(self, mission_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT revision,at,data FROM mission_revisions WHERE mission_id=? ORDER BY revision",(mission_id,)).fetchall()
        return [{'revision':r['revision'],'at':r['at'],'contract':json.loads(r['data'])} for r in rows]

    def revise_mission(self, mission_id: str, expected_revision: int, original_request: str) -> dict[str, Any]:
        if not isinstance(original_request,str) or not original_request.strip():
            raise ValueError('original request must be nonempty text')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT data FROM missions WHERE id=?",(mission_id,)).fetchone()
            if not row:
                raise KeyError('mission not found')
            mission = json.loads(row['data'])
            if mission['intent_revision'] != expected_revision:
                raise ValueError('stale mission revision')
            db.execute("INSERT OR IGNORE INTO mission_revisions VALUES(?,?,?,?)",(mission_id,mission['intent_revision'],now(),json.dumps(mission)))
            mission['intent_revision'] += 1
            mission['original_request'] = original_request.strip()
            mission['updated_at'] = now()
            words = original_request.casefold()
            if re.search(r"\b(?:do not|don't|never|stop|cancel)\b.{0,40}\bsend\b", words):
                mission['status'] = 'PAUSED'
                mission['permitted_actions'] = [item for item in mission['permitted_actions'] if item != 'simulated_send_after_approval']
                mission['constraints']['send_blocked_by_user'] = True
            elif all(term in words for term in ('report', 'send', 'approv')) and not ('contoso' in words and mission['constraints'].get('client') != 'Contoso account'):
                mission['status'] = 'ACTIVE'
                if 'simulated_send_after_approval' not in mission['permitted_actions']:
                    mission['permitted_actions'].append('simulated_send_after_approval')
                mission['constraints'].pop('send_blocked_by_user', None)
            else:
                mission['status'] = 'NEEDS_CLARIFICATION'
                mission['permitted_actions'] = [item for item in mission['permitted_actions'] if item != 'simulated_send_after_approval']
            db.execute("UPDATE missions SET data=? WHERE id=?",(json.dumps(mission),mission_id))
            db.execute("INSERT INTO mission_revisions VALUES(?,?,?,?)",(mission_id,mission['intent_revision'],now(),json.dumps(mission)))
            return mission

    def create_action(self, action: Action) -> None:
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO actions(id,data,state) VALUES(?,?,?)", (action.action_id, json.dumps(asdict(action)), action.status))
            self._event(db, action.action_id, action.status, asdict(action))

    def action(self, action_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM actions WHERE id=?", (action_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def actions_in_state(self, state: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM actions WHERE state=?",(state,)).fetchall()
        return [json.loads(row['data']) for row in rows]

    def all_actions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM actions ORDER BY rowid DESC LIMIT 200").fetchall()
        return [json.loads(row['data']) for row in rows]

    def transition(self, action_id: str, state: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock, self.connect() as db:
            row = db.execute("SELECT data,state FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row:
                raise KeyError('action not found')
            old = row['state']
            if state not in ALLOWED.get(old, set()):
                raise ValueError(f'illegal action transition {old} -> {state}')
            data = json.loads(row['data'])
            if updates:
                data.update(updates)
            data['status'] = state
            db.execute("UPDATE actions SET data=?,state=? WHERE id=?", (json.dumps(data), state, action_id))
            self._event(db, action_id, state, data)
            return data

    @staticmethod
    def _event(db: sqlite3.Connection, action_id: str, state: str, payload: dict[str, Any]) -> None:
        db.execute("INSERT INTO events(event_id,action_id,state,at,payload) VALUES(?,?,?,?,?)", (uid('evt'), action_id, state, now(), json.dumps(payload)))

    def events(self, action_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE (? IS NULL OR action_id=?) ORDER BY seq DESC LIMIT 200", (action_id, action_id)).fetchall()
        return [dict(r) for r in rows]

    def add_outbox(self, recipient_id: str, attachment_id: str, body: str, action_id: str | None = None) -> str:
        message_id = uid('msg')
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO outbox(id,recipient_id,attachment_id,body,sent_at,action_id) VALUES(?,?,?,?,?,?)", (message_id, recipient_id, attachment_id, body, now(), action_id))
        return message_id

    def commit_simulated_send(self, action_id: str, recipient_id: str, attachment_id: str, body: str, followup_id: str) -> str:
        """One local transaction for both effects; action_id prevents duplicate dispatch."""
        message_id = uid('msg')
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT INTO outbox(id,recipient_id,attachment_id,body,sent_at,action_id) VALUES(?,?,?,?,?,?)",(message_id,recipient_id,attachment_id,body,now(),action_id))
            db.execute("INSERT INTO followups VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET contact_id=excluded.contact_id,status=excluded.status,last_message_id=excluded.last_message_id,updated_at=excluded.updated_at",(followup_id,recipient_id,'contacted',message_id,now()))
        return message_id

    def outbox(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox ORDER BY sent_at DESC").fetchall()]

    def outbox_for_action(self, action_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox WHERE action_id=?",(action_id,)).fetchall()]

    def set_followup(self, followup_id: str, contact_id: str, status: str, message_id: str | None = None) -> None:
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO followups VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,last_message_id=excluded.last_message_id,updated_at=excluded.updated_at", (followup_id,contact_id,status,message_id,now()))

    def followup(self, followup_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM followups WHERE id=?", (followup_id,)).fetchone()
        return dict(row) if row else None

    def followups(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM followups ORDER BY id").fetchall()]


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def seed_demo(ledger: Ledger) -> dict[str, Any]:
    existing = ledger.get_json('missions', 'mission_demo')
    if existing:
        return existing
    entities = [
        Entity('contact_alex_1', 'Alex Morgan', 'contact', 'crm:contact:104', 'local-crm', context='Northwind account'),
        Entity('contact_alex_2', 'Alex Morgan', 'contact', 'crm:contact:208', 'local-crm', context='Contoso account'),
        Entity('report_old', 'Q3 Financial Report - Northwind', 'document', 'doc:report:17', 'local-docs', 'v3', '2026-10-20T09:00:00+00:00', 'Northwind account'),
        Entity('report_latest', 'Q3 Financial Report - Northwind (final)', 'document', 'doc:report:22', 'local-docs', 'v4', '2026-10-26T15:30:00+00:00', 'Northwind account'),
    ]
    for entity in entities:
        ledger.write_json('entities', entity.entity_id, asdict(entity))
    ledger.set_followup('followup_northwind','contact_alex_1','pending')
    mission = IntentContract('mission_demo', 'Update the follow-up and send the latest Northwind report to the intended Alex after approval.', 1,
        'Find the latest report for our client, update the follow-up record, and send the correct report to the intended contact after I approve.',
        {'client': 'Northwind account', 'version_policy': 'latest modified timestamp', 'send_requires_approval': True},
        [], ['read', 'prepare_draft', 'simulated_send_after_approval', 'physical_move', 'physical_click', 'physical_type'], ['external_send', 'delete', 'purchase'], 'consequential', 'ACTIVE', now(), now())
    ledger.write_json('missions', mission.mission_id, asdict(mission))
    ledger.record_mission_revision(asdict(mission))
    return asdict(mission)


def resolve_candidates(ledger: Ledger, kind: str, name: str | None = None, context: str | None = None) -> list[dict[str, Any]]:
    pool = [e for e in ledger.all_json('entities') if e['kind'] == kind]
    if name:
        needle = name.casefold()
        pool = [e for e in pool if needle in e['display_name'].casefold()]
    if context:
        needle = context.casefold()
        pool = [e for e in pool if needle in (e.get('context') or '').casefold()]
    return pool


def verify_outbox(ledger: Ledger, recipient_id: str, attachment_id: str, followup_id: str | None = None, action_id: str | None = None) -> dict[str, Any]:
    messages = ledger.outbox_for_action(action_id) if action_id else ledger.outbox()
    found = [m for m in messages if m['recipient_id'] == recipient_id and m['attachment_id'] == attachment_id]
    if action_id and messages and not found:
        return {'status':'WRONG_TARGET','evidence':{'action_id':action_id,'observed_messages':[{'id':m['id'],'recipient_id':m['recipient_id'],'attachment_id':m['attachment_id']} for m in messages]}}
    if len(found) == 1:
        followup = ledger.followup(followup_id) if followup_id else None
        followup_ok = followup is None or (followup['status'] == 'contacted' and followup['last_message_id'] == found[0]['id'])
        status = 'CONFIRMED' if followup_ok else 'UNCERTAIN'
        return {'status': status, 'evidence': {'message_id': found[0]['id'], 'action_id': action_id, 'recipient_id': recipient_id, 'attachment_id': attachment_id, 'sent_at': found[0]['sent_at'], 'source': 'local SQLite outbox', 'followup_id': followup_id, 'followup_status': followup.get('status') if followup else None, 'followup_last_message_id': followup.get('last_message_id') if followup else None}}
    if len(found) > 1:
        return {'status': 'UNCERTAIN', 'evidence': {'matching_messages': len(found), 'reason': 'duplicate effects found'}}
    return {'status': 'FAILED', 'evidence': {'matching_messages': 0}}


def reconcile_uncertain_local(ledger: Ledger) -> list[dict[str, Any]]:
    """Read-only recovery for local simulator effects; never redispatch."""
    results = []
    for action in ledger.actions_in_state('UNCERTAIN'):
        if action.get('operation') != 'simulated_send_and_update_followup':
            continue
        result = verify_outbox(ledger,action['target_entity_id'],action['arguments']['attachment_id'],action['arguments']['followup_id'],action['action_id'])
        if result['status'] in {'CONFIRMED','WRONG_TARGET'}:
            ledger.transition(action['action_id'],'OBSERVING',{'verification':result})
            ledger.transition(action['action_id'],result['status'],{'evidence_references':[result['evidence'].get('message_id','')]})
        results.append({'action_id':action['action_id'],'status':result['status'] if result['status'] != 'FAILED' else 'UNCERTAIN'})
    return results
