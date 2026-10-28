# Progress

## Gates (2026-10-28, Linux cloud host)
G0 PASS · G1 PASS · **G2 BLOCKED** (no Windows desktop: `WINDOWS_NATIVE_TESTS_NOT_RUN`; W1 Notepad code + opt-in tests ready) · G3 PASS · G4 PASS · G5 PASS (portable scope) · G6 PASS for typed transcripts + recorded skills (mic not automated) · G7 IN_PROGRESS (11/80 TESTED) · G8 PASS (scoped) · G9 IN_PROGRESS.
Evidence: `harness/evidence/G*.md`, `harness/evidence/full_suite.txt`, `harness/evidence/screens/`.

## Last passing commands
- `python -m pytest tests -q -rs` → 108 passed, 10 skipped (3 tkinter, 7 Windows-only), exit 0
- `python benchmarks/web_bench.py` → A 21/24, B 18/24, C 0/24 unsafe-or-false
- `python benchmarks/ablations.py` → every removed mechanism lets its targeted fault through; full C 0
- `python scripts/smoke_windows.py` → `WINDOWS_NATIVE_TESTS_NOT_RUN`, exit 3 (expected on Linux)

## Verified behaviour (Linux)
Browser fixtures + IEEC runner; utterance protocol with digest-bound spoken approval and correction; 13-role supervisor (parallel reads, entity gate, separate verifier, read retry, revision supersede, capability enforcement); recorded skills with repair proposals; sandboxed files with undo; SIGKILL during DISPATCHING → UNCERTAIN → reconcile, one message; 3D landing + Mission Control driven by Playwright with live SSE.

## Fixed this session
- CRM fixture acknowledged before commit (benchmark exposed a mislabelled run).
- Concurrent first requests raced fixture seeding (500 UNIQUE constraint) → lock.
- Re-planning after a confirmed send created a new send → refused (`ALREADY_SENT`).

## Blocked
- G2 / W1 / UIA: run `powershell -File scripts\smoke-windows.ps1` on Windows 10/11 and return the log.
- Push: GitHub returns 403 (Claude GitHub App not installed on the repo).

## Next executable command
`powershell -File scripts\smoke-windows.ps1` (on Windows), then G7: DevOps/test-runner and commerce-drafting adapters through the supervisor.
