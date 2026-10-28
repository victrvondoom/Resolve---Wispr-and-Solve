# Tasks

## Implemented and checked
- [x] Local dashboard/API, health route, SQLite WAL, startup scripts, and local demo fixtures
- [x] Typed contracts, mission revision history, narrow stop/send interpretation, same-name and report-version constraints
- [x] Approval digest, action-scoped atomic local effects, causal ledger, uncertain restart recovery, independent read-only reconciliation
- [x] Two concurrent read-only agent specialists, task DAG, cancellation, task events, cursor lease and fencing
- [x] Win32 observation snapshots, real input, controlled GUI/API physical tests, screenshot evidence, emergency stop
- [x] Browser-dependent dictation, exact demo commands, optional spoken feedback
- [x] 80-concept/10-workspace PLANNED registry and functional dashboard browser
- [x] Seeded eight-condition SQLite benchmark with separate ground truth, raw data, and aggregate report

## Added 2026-10-28 (verified on Linux; see harness/evidence)
- [x] Three separate browser fixture apps with hidden ground truth and test-mode-only faults
- [x] Playwright IEEC mission runner: ambiguity block, digest approval, stale-attachment invalidation, lost-ack reconcile, correction supersede
- [x] `/api/web/*` routes + SSE ledger stream + dashboard panel (UI E2E in headless Chromium)
- [x] Executor lease across concurrent missions with fencing check (with ablation)
- [x] Browser A/B/C benchmark (4 scenarios × 6 seeds) with oracle separate from verifier

## Added (second pass, verified on Linux)
- [x] Utterance classifier + revision protocol, spoken approval bound to presented digest, correction/cancel (W7, typed transcripts)
- [x] Supervisor with 13 tool-bounded roles, parallel fan-out, entity gate, separate verifier, retry, supersede (G5)
- [x] Recorded browser skills: record → compile → approve → replay → repair proposal (W8)
- [x] Sandboxed file worker with dry-run, stale-plan guard, undo after restart (W9)
- [x] 80-concept catalogue with validator; 11 TESTED
- [x] Ablations for six mechanisms; SIGKILL crash/restart test (W12)
- [x] 3D landing page and redesigned Mission Control, UI E2E, real screenshots
- [ ] W1 Notepad / focus-steal / UIA on real Windows (code ready, NOT_RUN)

## Outstanding gates
- [ ] UI Automation semantic element tree (Windows) — NOT_RUN here
- [ ] Notepad type+save+verify and focus-steal refusal on Windows (G2)
- [ ] Local/offline speech, microphone verification, dependable barge-in and spoken disambiguation
- [ ] Remaining specialist agents, durable handoffs, policy scopes, skill recording/replay, and resource telemetry
- [ ] Remaining eight fault conditions, GUI benchmark and component ablations
- [ ] Real adapters and representative workflows across all ten workspaces; registry entries currently remain PLANNED
- [ ] Packaging, accessibility audit, fresh second-machine install, demo video and authentic Wispr Flow Insights capture
