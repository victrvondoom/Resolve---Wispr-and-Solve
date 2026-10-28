# Architecture

One loopback process (`src/resolve_app.py`, stdlib HTTP) hosts the API, the SSE stream and the static UI. Missions, actions, events, verification claims, skills, file-operation journals and task events all live in one SQLite WAL database (`RESOLVE_DB`). The three fixture apps (`apps/web_fixtures.py`) run as separate servers with separate SQLite files and hidden `ground_truth` tables.

## Modules

| Module | Responsibility |
|---|---|
| `resolve_core.py` | Typed `Action` / `IntentContract`, central transition table, append-only event log, crash-window classification on start (`DISPATCHING → UNCERTAIN`). |
| `web_mission.py` | IEEC mission runner: deterministic intent template, candidate discovery from the live CRM DOM, binding by stable ID, version policy, approval digests, temporal guards, executor lease, DISPATCHING-before-effect, read-only reconcile, `WebVerifier` (reads app DBs). |
| `intent.py` | Utterance classifier (NEW_GOAL / REFINEMENT / CORRECTION / CANCEL / APPROVE / QUESTION). Qualifiers match department/org/ID/email, never display names; negation ("not Jordan from Sales") excludes candidates. |
| `supervisor.py` | 13 tool-bounded roles on `agent_runtime.MissionScheduler` (DAG, budgets, deadlines, revision checks, cursor lease). |
| `web_skills.py` | Record DOM demonstrations → typed, versioned, digest-sealed skills → approval → replay with fallbacks → repair proposals as new pending versions. |
| `file_worker.py` | Sandboxed inventory / duplicates / dry-run plan / apply / undo with journal; realpath + symlink refusal. |
| `windows_adapter.py`, `uia_adapter.py`, `notepad_w1.py` | Win32 enumeration, foreground/PID/bounds/generation guards, SendInput pointer/keys/chords, capture, emergency stop, Notepad W1. Windows only. |
| `web_api.py` | `/api/web/*` routes and the SSE stream. |
| `catalog.py` | 80-concept catalogue loader/validator; TESTED entries must point at existing tests. |

## Guarantees and their limits

- **Exactly one fixture message** under lost acknowledgement or process kill: the outbox enforces a unique `op_id`, the ledger persists `DISPATCHING` first, and recovery only reads. This is a property of these fixtures, not of arbitrary websites.
- **Stale approval cannot authorize a different effect**: the digest covers revision, target ID, recipient, attachment ID and sha256, and body; corrections and re-plans supersede every pre-dispatch action. In-flight actions (`DISPATCHING`…) cannot be recalled and are reported, not hidden.
- **Version continuity**: the planner requires the docs app's sha256 to equal the sha256 of the bytes on disk; the sender re-checks the sha256 just before dispatch.
- **Exclusive execution**: a SQLite compare-and-swap lease with a generation fence; the fence is validated immediately before the side effect.

## Events (SSE `/api/stream`)

`action.<state>` `{seq, action_id, state, at}` and `task.<status>` `{seq, task_id, mission_id, status, at}`; frame id `<action_seq>.<task_seq>` so `Last-Event-ID` resumes losslessly. Every frame is read from SQLite; the UI never invents progress.
