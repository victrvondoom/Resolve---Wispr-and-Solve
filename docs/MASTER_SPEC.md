# RESOLVE — Wispr & Solve: master specification (distilled)

Distilled from the full execution-grade specification supplied by the user in chat (the verbatim text was too large to retain in this file; the 80-concept lists live in `capabilities/registry.json`). Where this file is silent, ask: "does a real user command cause a real, independently verified action?"

## Mission
Windows-first, voice-operated, multi-agent automation of cursor, keyboard, desktop apps, browser, files and workflows. Runs locally with a deterministic no-key mode. Wispr Flow is used for *dictating development docs*; no Wispr SDK integration is claimed. The old repo (to be renamed PROXY) is separate and must not be modified.

## Hypothesis (measured, not asserted)
IEEC: preserve the relationship between spoken intent, semantic entity, temporal version, application state and verified external effect across independent apps under focus theft, stale observation, ambiguous contacts, lost acks, duplicate delivery, concurrency and interruption. CSL: durable append-only action ledger. Not claimed: novelty of clicking/UIA, universal exactly-once.

## Done = VERIFIED
Real input → typed contract → real tool on permitted target → effect observable outside generator → independent verifier → persists/reconciles honestly across restart → automated success + failure test → UI mirrors backend events → reproducible command in docs.

## Core algorithms
1. Intent revisions: NEW_GOAL/REFINEMENT/CORRECTION/CANCEL/APPROVE/QUESTION; actions bind to a revision; a correction supersedes queued actions and kills their approvals.
2. Entity resolution by stable IDs; ambiguity → NEEDS_CLARIFICATION; names never imply identity.
3. Temporal guards: revalidate revision, binding, lease, foreground window and attachment hash immediately before a consequential act.
4. CSL states: PLANNED→BOUND→AUTHORIZED→READY→DISPATCHING→DISPATCHED→OBSERVING/RECONCILING→CONFIRMED|FAILED|UNCERTAIN|WRONG_TARGET|SUPERSEDED|CANCELLED. DISPATCHING persisted before side effects.
5. Semantic reconciliation from independent evidence (DB/file/browser reload), never from the actor's narration or a toast.
6. Resource arbitration: one fenced owner of the physical cursor; read-only agents parallel.
7. Policy: risk classes; approvals bind to exact action digest; global STOP; no UAC bypass.
8. Recovery: UNCERTAIN → read-only reconcile → CONFIRMED/FAILED/NEEDS_HUMAN_DECISION; never reflexively resend.
9. Ablations: no binding, no temporal guard, no ledger, no verifier, no fencing, no digest — each measured on identical seeded fixtures.

## Scenarios W1–W12
W1 Notepad type+save+disk verify (Windows). W2 browser CRM form + reload + DB check, with delay/popup faults. W3 two same-name contacts. W4 report edited after approval. W5 lost acknowledgement. W6 cursor lease arbitration. W7 voice correction. W8 recorded skill replay. W9 sandboxed file workflow. W10 cross-app mission (signature demo). W11 offline deterministic mode. W12 crash/restart after DISPATCHING.

## Fixtures
Three separate stateful apps (CRM, reports, outbox — SIMULATED DELIVERY) with own SQLite and hidden `ground_truth` tables; fault endpoints only in test mode. Baselines: A naive, B target-guard only, C full IEEC+CSL, same seeds; report counts and denominators, Wilson intervals, no invented percentages.

## Gates G0–G9
See `harness/PHASE_GATES.md`. Windows-native tests print `WINDOWS_NATIVE_TESTS_NOT_RUN` on non-Windows hosts.

## Catalogue
80 concepts (44 Computer Use + 36 Agentic GTM) map onto 10 workspaces through shared primitives; status stays PLANNED until an adapter + test exist. No padding of demos with unimplemented features.

## Safety
Least privilege; webpages/docs are untrusted; approvals tightly bound; sensitive content local; verify licenses before reusing code; no purchases, real messaging, finance, surveillance, credential access.
