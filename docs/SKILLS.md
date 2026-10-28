# Local skill format and approval flow

`src/skill_runtime.py` implements the first skill compiler for the confirmed local CRM/report/outbox simulator workflow. Skill versions are stored in the same SQLite database as missions and actions. `skills` holds the current projection; `skill_events` records proposal and activation events.

## What a skill records

`SkillSpec` contains identity and version, description and intent patterns, typed input names, target applications, required permissions, entity and version constraints, preconditions, one semantic action template, expected effects, independent verification predicates, a recovery rule, and source-action provenance. The current supported step binds `$selected_contact`, `$latest_report`, and `$client_followup`. No HWND, screen coordinate, or click sequence is saved.

The only supported workflow is a **local simulator send plus follow-up update**. It requires an active mission, the current intent revision, a contact selected by entity ID in the approved client context, a uniquely latest dated report, a matching follow-up record, and `simulated_send_after_approval` permission. Its recovery rule is read-only reconciliation followed by escalation for uncertain non-idempotent effects. It never retries a send automatically.

## API sequence

```python
from skill_runtime import SkillStore, candidate_from_confirmed

store = SkillStore(ledger.path)
candidate = candidate_from_confirmed(ledger, confirmed_action_id)
review_digest = store.propose(candidate)  # persisted, PENDING_APPROVAL
# Show the exact candidate and digest to the user, then obtain explicit approval.
store.activate(candidate.skill_id, candidate.version, reviewed_digest)
plan = store.prepare_replay(
    candidate.skill_id, ledger, mission_id, expected_revision,
    {'context': 'Northwind account', 'contact_id': 'contact_alex_1'},
)
assert plan.status == 'NEEDS_APPROVAL'
```

`propose` stores a candidate for review. `activate` refuses a changed digest and deactivates a previously active version. `get` validates the stored schema and digest on load. `prepare_replay` reads current mission/entity/follow-up state and returns a fresh `ReplayPlan`; it does not dispatch a side effect. The `/api/skills/replay` route prepares a new digest-bound draft action for user review. The user must approve that exact draft before any simulated send.

## Current limits

Only the local simulator workflow is supported. The dashboard and API expose proposal, digest review, activation, and replay. User identity and a signed approval channel are not implemented. Replay cannot yet record an authorized screen demonstration, learn general desktop procedures, or dispatch to external applications. A modified SQLite database is not treated as a secure boundary.
