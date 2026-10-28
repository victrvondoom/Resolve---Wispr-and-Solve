# Progress

## Current gate
G0 PASS · G1 PASS · G2 BLOCKED (no Windows host: `WINDOWS_NATIVE_TESTS_NOT_RUN`) · G3 PASS (browser scope) · G4 PASS (browser-fixture scope, gaps in evidence) · G5 IN_PROGRESS · G8 IN_PROGRESS · G6, G7, G9 NOT_STARTED.
Evidence: `harness/evidence/G*.md`, full run in `harness/evidence/full_suite.txt`.

## Last passing commands (Linux, 2026-10-28)
- `python -m pytest tests -q -rs` → 52 passed, 8 skipped (tkinter ×3, Windows-only ×5), exit 0
- `python benchmarks/web_bench.py` → data/benchmarks/web_report.md (72 runs)
- `python scripts/smoke_windows.py` → `WINDOWS_NATIVE_TESTS_NOT_RUN`, exit 3 (expected here)

## Verified this session
- Three separate stateful web apps (CRM / reports / outbox, own SQLite + hidden ground_truth) driven by Playwright; independent DB verification; fault endpoints gated to test mode.
- IEEC web mission: ambiguity block, bind by stable ID, latest-version policy, exact-digest approval, stale-attachment invalidation, voice-style correction superseding approvals, lost-ack UNCERTAIN → read-only reconcile with exactly one message, misleading-toast caught, restart classification.
- HTTP API `/api/web/*` + SSE `/api/stream` from the persisted ledger; dashboard panel clicked by headless Chromium; state survives reload and server restart.
- Executor lease serializes concurrent missions (ablation without it fails); fenced worker cannot dispatch.
- Browser benchmark: A 21/24, B 18/24, C 0/24 unsafe-or-false outcomes (controlled baselines; see G8.md caveats).

## Known failing / not run
- tkinter-based Tk fixtures and all Win32/UIA tests: NOT_RUN on this host.
- Nothing verified for Notepad automation, cursor movement, focus-steal on Windows this session.

## Blocked
- G2: needs an interactive Windows session. Run `powershell -File scripts/smoke-windows.ps1` and return the log.

## Next executable command
`python -m pytest tests -q` then continue G5: add remaining tool-bounded roles (FileWorker, IndependentVerifier as separate worker from executor) and W9 sandboxed file workflow.

## Do not repeat
Playwright works with `executable_path=/opt/pw-browsers/chromium`; do not reinstall browsers. The web fixture design and oracle separation are settled (DECISIONS.md).
