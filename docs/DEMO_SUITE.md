# Three-window instrumented demo suite

Run `python apps\demo_suite.py` from the repository root. It opens **RESOLVE Demo CRM**, **RESOLVE Demo Documents**, and **RESOLVE Demo Outbox** as three real Tk windows. It requires Python's standard Tkinter module and an unlocked interactive Windows desktop. No API credentials are needed. Close any window to stop the suite.

The suite seeds two contacts with the same display name and different account/email IDs, two report versions with different modified timestamps and content hashes, one pending Northwind follow-up, and an empty local outbox. Business state persists in `data/demo_suite.sqlite3`; a separate `data/demo_suite_truth.sqlite3` records UI-triggered effects. Both are local simulator data. The Outbox never sends external email.

`data/demo_suite_observation.json` is atomically refreshed every 500 ms and after UI events. It includes an observation time/ID, HWNDs, titles, window rectangles, widget screen centers, selected entities, follow-up state, document selection, draft IDs, outbox records, and popup state. It is a read-only **observation protocol**, not an action endpoint. A controller should read it, bind a target entity, obtain a fresh Win32 snapshot for the target HWND, then call the guarded Windows actuator with that snapshot ID and screen coordinate. The application keeps all follow-up, report-selection, draft, and send mutations in Tk widget callbacks.

Suggested physical workflow:

1. Inspect the two Alex entries in CRM; click the Northwind row and **Update selected follow-up**.
2. Inspect report timestamps in Documents; click v4 and **Use selected report**.
3. For focus-fault testing, click **Inject focus-stealing popup**. A modal window appears. A controller using an old HWND or snapshot must stop and reobserve. Click **Dismiss popup** to continue.
4. In Outbox, click the Northwind recipient and v4 attachment, review the message, click **Prepare draft**, and inspect the persisted draft.
5. After separate RESOLVE approval, click the local approval checkbox and **Send approved draft to local outbox**. Read the outbox and linked CRM follow-up from SQLite, and the `simulated_send` ground-truth event from the truth database.

The suite can be launched with `--db`, `--truth`, and `--observation` paths for isolated experiments. Existing databases are preserved; fixtures seed only when contacts are absent. `python -m unittest tests.test_demo_suite -v` checks fixtures without opening windows. To exercise all three Tk callbacks and the focus popup, set `RESOLVE_DEMO_GUI_TEST=1` and run the same test module.
