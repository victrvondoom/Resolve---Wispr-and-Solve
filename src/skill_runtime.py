"""Versioned, approval-gated skills for RESOLVE's local simulator workflow.

Skills preserve semantic constraints, not clicks or screen coordinates. Replay
prepares a new plan against current entity state and never dispatches effects.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()


class SkillError(ValueError):
    pass


class AmbiguousEntity(SkillError):
    def __init__(self, entity_kind: str, candidates: list[dict[str, Any]]):
        self.entity_kind = entity_kind
        self.candidates = candidates
        super().__init__(f'{entity_kind} selection is ambiguous; explicit entity ID required')


@dataclass(frozen=True)
class SkillStep:
    operation: str
    application_id: str
    arguments: dict[str, str]
    approval_required: bool

    def __post_init__(self):
        if self.operation != 'simulated_send_and_update_followup' or self.application_id != 'local-outbox':
            raise SkillError('unsupported skill operation or application')
        if self.arguments != {'recipient': '$selected_contact', 'attachment': '$latest_report', 'followup': '$client_followup'}:
            raise SkillError('skill step must use semantic recipient, report, and follow-up bindings')
        if self.approval_required is not True:
            raise SkillError('simulated send must require approval')


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    name: str
    version: int
    description: str
    intent_patterns: tuple[str, ...]
    input_schema: dict[str, str]
    target_applications: tuple[str, ...]
    required_permissions: tuple[str, ...]
    entity_constraints: dict[str, str]
    preconditions: tuple[str, ...]
    actions: tuple[SkillStep, ...]
    expected_effects: tuple[str, ...]
    verification_predicates: tuple[str, ...]
    recovery_policy: str
    provenance: dict[str, Any]

    def __post_init__(self):
        if not self.skill_id or not self.name or self.version < 1:
            raise SkillError('skill ID, name, and positive version are required')
        if not self.intent_patterns or not all(isinstance(pattern, str) and pattern.strip() for pattern in self.intent_patterns):
            raise SkillError('at least one intent pattern is required')
        if self.input_schema != {'contact_id': 'entity_id', 'context': 'organization'}:
            raise SkillError('unsupported skill input schema')
        if self.entity_constraints.get('version_policy') != 'latest_modified_timestamp' or not self.entity_constraints.get('context'):
            raise SkillError('skill requires an explicit context and latest-version policy')
        if self.target_applications != ('local-crm', 'local-docs', 'local-outbox'):
            raise SkillError('unexpected target applications')
        if self.required_permissions != ('simulated_send_after_approval',):
            raise SkillError('skill permission scope is invalid')
        if len(self.actions) != 1 or not isinstance(self.actions[0], SkillStep):
            raise SkillError('exactly one approved simulator step is supported')
        if self.verification_predicates != ('outbox_recipient_attachment_matches', 'followup_links_message'):
            raise SkillError('independent effect predicates are required')
        if self.recovery_policy != 'reconcile_read_only_then_escalate_uncertain':
            raise SkillError('non-idempotent skill cannot use automatic retry')
        if not self.provenance.get('source_action_id') or not self.provenance.get('source_mission_id'):
            raise SkillError('confirmed source action provenance is required')

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SkillSpec:
        expected = set(cls.__dataclass_fields__)
        if set(raw) != expected:
            raise SkillError(f'skill fields differ from schema: missing={sorted(expected - set(raw))}, extra={sorted(set(raw) - expected)}')
        try:
            values = dict(raw)
            for name in ('intent_patterns', 'target_applications', 'required_permissions', 'preconditions', 'expected_effects', 'verification_predicates'):
                if not isinstance(values[name], list):
                    raise SkillError(f'{name} must be a list')
                values[name] = tuple(values[name])
            if not isinstance(values['actions'], list):
                raise SkillError('actions must be a list')
            values['actions'] = tuple(SkillStep(**step) for step in values['actions'])
            if not isinstance(values['provenance'], dict) or not isinstance(values['entity_constraints'], dict):
                raise SkillError('provenance and entity constraints must be objects')
            return cls(**values)
        except (TypeError, KeyError) as exc:
            raise SkillError(f'invalid skill schema: {exc}') from exc


@dataclass(frozen=True)
class ReplayPlan:
    skill_id: str
    skill_version: int
    mission_id: str
    intent_revision: int
    contact: dict[str, Any]
    report: dict[str, Any]
    followup_id: str
    operation: str
    approval_required: bool
    status: str
    observed_at: str
    precondition_fingerprint: str
    expected_effects: tuple[str, ...]


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class SkillStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS skills(skill_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL, approval_digest TEXT, created_at TEXT NOT NULL, activated_at TEXT, PRIMARY KEY(skill_id,version))')
            db.execute('CREATE TABLE IF NOT EXISTS skill_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL, digest TEXT)')

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=5, factory=_ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def propose(self, skill: SkillSpec) -> str:
        encoded = asdict(skill)
        review_digest = _digest(encoded)
        with self._connect() as db:
            db.execute('INSERT INTO skills(skill_id,version,data,status,created_at) VALUES(?,?,?,?,?)', (skill.skill_id,skill.version,json.dumps(encoded,ensure_ascii=False),'PENDING_APPROVAL',_now()))
            db.execute('INSERT INTO skill_events(skill_id,version,status,at,digest) VALUES(?,?,?,?,?)', (skill.skill_id,skill.version,'PENDING_APPROVAL',_now(),review_digest))
        return review_digest

    def get(self, skill_id: str, version: int) -> tuple[SkillSpec, str]:
        with self._connect() as db:
            row = db.execute('SELECT data,status,approval_digest FROM skills WHERE skill_id=? AND version=?', (skill_id,version)).fetchone()
        if row is None:
            raise SkillError('skill version not found')
        raw = json.loads(row['data'])
        skill = SkillSpec.from_dict(raw)
        if skill.skill_id != skill_id or skill.version != version:
            raise SkillError('stored skill identity mismatch')
        if row['status'] in {'ACTIVE', 'INACTIVE'} and row['approval_digest'] != _digest(raw):
            raise SkillError('approved skill no longer matches reviewed content')
        return skill, row['status']

    def activate(self, skill_id: str, version: int, reviewed_digest: str) -> SkillSpec:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data,status FROM skills WHERE skill_id=? AND version=?', (skill_id,version)).fetchone()
            if row is None:
                raise SkillError('skill version not found')
            if row['status'] != 'PENDING_APPROVAL':
                raise SkillError('skill is not awaiting approval')
            raw = json.loads(row['data'])
            skill = SkillSpec.from_dict(raw)
            actual = _digest(raw)
            if not reviewed_digest or actual != reviewed_digest:
                raise SkillError('approval digest mismatch')
            db.execute("UPDATE skills SET status='INACTIVE' WHERE skill_id=? AND status='ACTIVE'", (skill_id,))
            db.execute("UPDATE skills SET status='ACTIVE',approval_digest=?,activated_at=? WHERE skill_id=? AND version=?", (actual,_now(),skill_id,version))
            db.execute('INSERT INTO skill_events(skill_id,version,status,at,digest) VALUES(?,?,?,?,?)', (skill_id,version,'ACTIVE',_now(),actual))
            return skill

    def active(self, skill_id: str) -> SkillSpec:
        with self._connect() as db:
            rows = db.execute("SELECT version FROM skills WHERE skill_id=? AND status='ACTIVE'", (skill_id,)).fetchall()
        if len(rows) != 1:
            raise SkillError('exactly one active skill version is required')
        return self.get(skill_id, rows[0]['version'])[0]

    def events(self, skill_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM skill_events WHERE skill_id=? ORDER BY seq', (skill_id,))]

    def list_skills(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute('SELECT skill_id,version,data,status,created_at,activated_at FROM skills ORDER BY skill_id,version DESC').fetchall()
        return [{'skill_id': row['skill_id'], 'version': row['version'], 'status': row['status'], 'created_at': row['created_at'], 'activated_at': row['activated_at'], 'review_digest': _digest(json.loads(row['data'])), 'spec': json.loads(row['data'])} for row in rows]

    def next_version(self, skill_id: str) -> int:
        with self._connect() as db:
            row = db.execute('SELECT MAX(version) AS highest FROM skills WHERE skill_id=?', (skill_id,)).fetchone()
        return int(row['highest'] or 0) + 1

    def prepare_replay(self, skill_id: str, ledger: Any, mission_id: str, expected_revision: int, inputs: dict[str, str]) -> ReplayPlan:
        skill = self.active(skill_id)
        mission = ledger.get_json('missions', mission_id)
        if mission is None or mission.get('status') != 'ACTIVE':
            raise SkillError('mission is not active')
        if mission.get('intent_revision') != expected_revision:
            raise SkillError('skill replay rejected: mission intent revision changed')
        if any(permission not in mission.get('permitted_actions', ()) for permission in skill.required_permissions):
            raise SkillError('mission does not permit the skill operation')
        context = inputs.get('context')
        if context != skill.entity_constraints['context'] or context != mission.get('constraints', {}).get('client'):
            raise SkillError('client context differs from approved skill constraints')
        contact_candidates = [row for row in ledger.all_json('entities') if row.get('kind') == 'contact' and row.get('context') == context]
        contact_id = inputs.get('contact_id')
        if not contact_id:
            raise AmbiguousEntity('contact', contact_candidates)
        contacts = [row for row in contact_candidates if row['entity_id'] == contact_id]
        if len(contacts) != 1 or not contacts[0].get('stable_id'):
            raise SkillError('selected contact has no matching stable identity in current context')
        reports = [row for row in ledger.all_json('entities') if row.get('kind') == 'document' and row.get('context') == context and row.get('modified_at')]
        if not reports:
            raise SkillError('no dated report found for the client')
        try:
            dated = []
            for row in reports:
                moment = datetime.fromisoformat(row['modified_at'])
                if moment.tzinfo is None:
                    raise SkillError('report modification timestamp has no timezone')
                dated.append((moment.astimezone(timezone.utc), row))
        except (ValueError, TypeError) as exc:
            raise SkillError('report modification timestamp is invalid') from exc
        newest = max(moment for moment, _ in dated)
        latest = [row for moment, row in dated if moment == newest]
        if len(latest) != 1 or not latest[0].get('stable_id'):
            raise AmbiguousEntity('latest report', latest)
        report = latest[0]
        if inputs.get('report_id') and inputs['report_id'] != report['entity_id']:
            raise SkillError('selected report is not current under latest-modified policy')
        followup_id = 'followup_northwind'
        followup = ledger.followup(followup_id)
        if followup is None or followup['contact_id'] != contacts[0]['entity_id']:
            raise SkillError('follow-up record does not match selected contact')
        fingerprint = _digest({'mission_id':mission_id,'intent_revision':expected_revision,'contact':contacts[0], 'report':report,'followup':followup})
        return ReplayPlan(skill.skill_id,skill.version,mission_id,expected_revision,contacts[0],report,followup_id,skill.actions[0].operation,True,'NEEDS_APPROVAL',_now(),fingerprint,skill.expected_effects)


def candidate_from_confirmed(ledger: Any, action_id: str, *, skill_id: str = 'northwind_followup', version: int = 1) -> SkillSpec:
    """Compile a confirmed local effect into an inactive semantic skill candidate."""
    action = ledger.action(action_id)
    if action is None or action.get('status') != 'CONFIRMED' or action.get('verification', {}).get('status') != 'CONFIRMED':
        raise SkillError('only independently confirmed actions may become skill candidates')
    if action.get('operation') != 'simulated_send_and_update_followup' or action.get('application_id') != 'local-outbox':
        raise SkillError('source action is not the supported simulator workflow')
    contact = ledger.get_json('entities', action.get('target_entity_id', ''))
    report = ledger.get_json('entities', action.get('arguments', {}).get('attachment_id', ''))
    if not contact or not report or contact.get('context') != report.get('context'):
        raise SkillError('source entities do not share a verified context')
    evidence = action['verification']['evidence']
    if evidence.get('recipient_id') != contact['entity_id'] or evidence.get('attachment_id') != report['entity_id']:
        raise SkillError('source evidence does not match the action entities')
    if evidence.get('followup_last_message_id') != evidence.get('message_id'):
        raise SkillError('source follow-up is not linked to the confirmed message')
    return SkillSpec(
        skill_id=skill_id,
        name='Approved client follow-up with latest report',
        version=version,
        description='Resolve a client contact and latest report, then prepare a simulator send and follow-up update for explicit approval.',
        intent_patterns=('send the latest report to the intended contact after approval',),
        input_schema={'contact_id':'entity_id','context':'organization'},
        target_applications=('local-crm','local-docs','local-outbox'),
        required_permissions=('simulated_send_after_approval',),
        entity_constraints={'context':contact['context'],'version_policy':'latest_modified_timestamp','contact_kind':'contact','report_kind':'document'},
        preconditions=('mission_active','intent_revision_current','contact_stable_id_present','latest_report_unambiguous','followup_matches_contact'),
        actions=(SkillStep('simulated_send_and_update_followup','local-outbox',{'recipient':'$selected_contact','attachment':'$latest_report','followup':'$client_followup'},True),),
        expected_effects=('one_matching_local_outbox_message','followup_links_message'),
        verification_predicates=('outbox_recipient_attachment_matches','followup_links_message'),
        recovery_policy='reconcile_read_only_then_escalate_uncertain',
        provenance={'source_action_id':action_id,'source_mission_id':action['mission_id'],'source_intent_revision':action['intent_revision'],'evidence_references':action.get('evidence_references',[])},
    )
