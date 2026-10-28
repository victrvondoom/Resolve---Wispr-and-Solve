# Local demo and recording runbook

## Reproduce the current build

1. Run `python .\src\resolve_app.py` from the project root and open `http://127.0.0.1:8765`.
2. In **Agents**, run the resolver specialists. Show their returned local records and durable task events. The two reads execute concurrently.
3. In **Entities**, show the two Alex Morgan contacts and two Northwind report versions. The wrong-client contact and older report are rejected by the draft action.
4. Choose Northwind Alex and v4. Prepare the draft; read the recipient, attachment and digest. No simulator message exists yet for this action.
5. Revise the mission to “Do not send the report.” Its status becomes `PAUSED`; the old approval is refused. Revise again with “Send the latest report after approval.”
6. Prepare a fresh draft and approve it. The simulator writes an action-scoped outbox record and linked follow-up record. The independent verifier reports `CONFIRMED` only after checking persisted IDs and link.
7. In **Live Computer**, inspect a safe foreground test window and use a fresh snapshot for bounded pointer movement, click, and typing. The standalone `.\scripts\windows_demo.ps1` is the controlled physical-input reproduction. The live mission simulator itself is not yet driven through three separate GUI applications.
8. In **Benchmarks**, show the recorded seed-11 SQLite comparison and its simulator label. Raw data and ground truth are in `data/benchmarks/`.

Browser speech dictation can fill the command field where Web Speech Recognition is available. The user reviews and submits the transcript. It may require the browser's speech service. Text input is the reliable fallback.

## Two-minute honest recording outline

| Time | Show |
|---|---|
| 0:00–0:15 | Same-name and version risk in local fixtures. |
| 0:15–0:30 | Dictate the sample request if browser speech is available; show transcript review. |
| 0:30–0:45 | Run two read-only specialists; inspect candidates. |
| 0:45–1:00 | Demonstrate real pointer/click/type in the controlled Tk window. |
| 1:00–1:18 | Show old report refusal and changed-instruction approval refusal. |
| 1:18–1:38 | Approve the exact local simulator action; show outbox and follow-up evidence. |
| 1:38–2:00 | Show the labeled simulator benchmark and authentic Wispr Flow development evidence if available. |

Record actual interactions. Keep the **local simulator** and **SQLite benchmark** labels visible; do not call the outbox an external email. Capture Wispr Flow Insights from the real account if submitting to the hackathon; this repository does not generate that card. Do not show credentials or unrelated private windows. The demo video and Insights screenshot have not been created in this repository.
