# 2-minute demo script (record on your own machine)

Setup: `.\scripts\bootstrap.ps1`, then `.\scripts\dev.ps1`. Open http://127.0.0.1:8765, then **Open Mission Control**. Keep hands off the mouse; use the keyboard or the mic.

| Time | Do / say | What the viewer sees |
|---|---|---|
| 0:00 | Landing page | The problem in one line; the live benchmark table comes from `/api/web/benchmark`. |
| 0:10 | Mission Control, say or type: *"Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve."* | A new mission appears; status NEEDS_CLARIFICATION. |
| 0:20 | Run agents (supervise) | Four read-only roles run in parallel (real durations); the Planner refuses: two Jordan Lees with their departments. |
| 0:35 | *"The one from Operations."* | Binding C-104; re-run agents → plan + safety verdicts; the exact send summary with sha256. |
| 0:50 | *"Approve."* | Send AUTHORIZED for that digest only. |
| 0:55 | *"No, I mean Jordan from Sales."* (optional) | Revision 2; the old send shows SUPERSEDED; *"approve"* now fails until re-planned. Switch back to Operations. |
| 1:10 | Arm **lost-acknowledgement fault**, run send | Live stream: DISPATCHING → UNCERTAIN → OBSERVING → CONFIRMED; exactly one outbox row. |
| 1:30 | Execute phase | IndependentVerifier claims agree with the ledger; the verifier is not the executor. |
| 1:40 | Skills: record → approve → replay with layout shift | CONFIRMED via fallback locators, or a repair proposal as v2 pending review. |
| 1:55 | Benchmark panel | A 21/24, B 18/24, C 0/24 unsafe/false — labelled controlled baselines. |

Windows-only segment (optional, only if `scripts/smoke-windows.ps1` passed on your machine): the W1 Notepad run with the real cursor/keyboard. Never present the browser (DOM) run as physical cursor control.
