# Feature matrix

| Capability | Status | Evidence and boundary |
|---|---|---|
| Local dashboard/API | Implemented | `src/resolve_app.py`, `web/index.html`; loopback only, same-origin session token for POST |
| Typed mission/action contracts | Implemented for demo | `src/resolve_core.py` dataclasses, validation, durable revision history |
| Natural-language intent parsing | Narrow deterministic demo grammar | Recognizes the sample send/approval request and stop-send instruction; other revisions require clarification |
| Same-name entity ambiguity | Implemented for local data | Explicit contact ID selection; client context checked again before dispatch |
| Report freshness | Implemented for local data | Latest modification timestamp under matching client context, rechecked before dispatch |
| Mission DAG and parallel readers | Implemented | `src/agent_runtime.py`: two real read-only specialists, task events, dependency scheduler, cancellation |
| Cursor arbitration | Implemented for cooperating RESOLVE processes | SQLite lease with fencing and adapter file lock; no OS-wide arbitration |
| Durable action ledger | Implemented | SQLite WAL events, validated transitions, uncertain restart recovery and read-only local reconciliation |
| Approval integrity | Implemented for simulator | Exact action digest, mission revision, stable IDs and current version checked |
| Local simulated effect verification | Implemented | Action-scoped outbox row plus linked follow-up record checked independently |
| Win32 observation and physical input | Implemented and controlled GUI tested | Window snapshots, bounded pointer/click/type/scroll/key, emergency stop; `docs/WINDOWS_CONTROL.md` |
| Screenshot evidence | Implemented with optional Pillow | Foreground screen crop, SHA-256; controlled test screenshot under `data/evidence` |
| Voice input and demo commands | Browser-dependent | Web Speech Recognition fills an editable transcript and recognizes exact local-demo commands; browser/microphone service may be required |
| Spoken feedback | Browser-dependent | Optional SpeechSynthesis narration toggle; not an offline speech guarantee |
| Voice emergency stop | Browser-dependent | Exact “Resolve stop”/“emergency stop” command invokes the same emergency stop endpoint |
| Seeded benchmark | Implemented for 8 local simulator cases | 3 policies × 8 conditions, raw/ground-truth/aggregate files in `data/benchmarks`; not a Windows GUI benchmark |
| Capability registry | Metadata implemented | 80 named concepts routed to 10 workspaces in `capabilities/registry.json`; all adapters marked PLANNED |
| UIA element tree inspection | Implemented as optional read-only adapter | `uiautomation==2.0.29` in project-local venv; bounded tree, metadata and hash; controlled Tk/API test passed |
| UIA semantic actions and browser DOM | Not implemented | Current GUI control is coordinate scoped by live Win32 snapshot |
| External email/CRM | Not implemented | No external communication; simulator only |
| Local semantic skill | Implemented for simulator | Confirmed action compiles to versioned skill; explicit digest activation; replay re-resolves entities and prepares a fresh approval-bound draft |
| Broad workspace adapters | Not implemented | Registry metadata is a PLANNED capability catalog only |
