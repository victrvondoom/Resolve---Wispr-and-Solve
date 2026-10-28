# Audit (session 2026-10-28, Linux cloud host, no GUI desktop)

Platform: Linux 6.18, Python 3.13.17, Node 22.22, `DISPLAY` empty (no desktop), no tkinter. Original repo: 55 files, ~4.7k lines Python, stdlib `http.server` backend (not FastAPI — kept; see DECISIONS.md), single-file `web/index.html`.

Baseline before edits: `python -m unittest discover -s tests` → 45 run, **3 import errors** (`tkinter` missing: test_api_gui, test_demo_controller, test_demo_suite), 5 skipped.

| feature | evidence in code | executed test | status | blocker | repair target |
|---|---|---|---|---|---|
| Ledger / action state machine (SQLite WAL) | src/resolve_core.py | test_core, API integration | tested (simulator, internal DB) | outbox lived in the same DB as the ledger, so "independent" verification was not independent | web fixtures with separate DBs (done) |
| Intent revisions | Ledger.revise_mission (keyword rules, demo mission only) | test_core | tested, narrow | generic mission model absent | web_mission.correct (done for web missions) |
| Approval digest | resolve_app.prepare_draft | test_api_integration | tested | — | extended to exact recipient+sha256 (done) |
| Agents / DAG / lease | src/agent_runtime.py | test_agent_runtime | tested; 2 read-only specialists only | 13 roles absent | G5 |
| Win32 input / capture / E-stop | src/windows_adapter.py | opt-in tests | Windows-only; **NOT_RUN here** | no Windows host | scripts/smoke-windows.ps1 |
| UIA | src/uia_adapter.py | test_uia_adapter | Windows-only; NOT_RUN | no Windows host | G2 |
| Tk demo apps | apps/demo_suite.py | tests (skip w/o tkinter) | Windows/desktop-only | not browser-operable | browser fixtures (done) |
| Browser automation | absent | — | absent → **implemented** | — | apps/web_fixtures.py + src/web_mission.py |
| Streaming events | absent (polling UI) | — | absent → **implemented** SSE `/api/stream` | — | done |
| Voice | browser Web Speech only | none | mock-adjacent (typed fallback real) | no STT engine | G6 |
| Skills | src/skill_runtime.py | test_skill_runtime | backend-only, confirmed-run candidates | no recorder | G6 |
| Benchmark | src/benchmark_lab.py | test_benchmark_lab | simulator on internal SQLite | not browser/GUI | benchmarks/web_bench.py |
| Capability registry | capabilities/registry.json (80 PLANNED) | test_capability_registry | catalogue only, all PLANNED | no adapters | G7 |
| Fake integrations found | none constant-return; but dashboard "agents" are 2 deterministic readers | — | — | — | documented |
