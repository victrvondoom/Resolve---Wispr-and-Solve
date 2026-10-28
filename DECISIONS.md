# Decisions

- Keep this implementation in the current workspace root; the only prior contents were an empty nested placeholder.
- Use Python standard library for the first runnable slice because Python 3.14 exists while Node and common Python web/automation packages do not.
- Use SQLite WAL and explicit state transitions for durable local audit history. Keep secrets out of ledger payloads.
- Treat HWND as a transient window locator, never a persistent entity ID. Every physical input requires fresh foreground validation.
- Use a local simulator for the demonstration. Do not claim that external email was sent.
- Keep the runtime Python standard-library based; browser speech is an optional browser capability and Pillow is optional for screenshots.
- Use exact demo voice commands and a narrow deterministic request interpreter. Unknown revisions enter NEEDS_CLARIFICATION; explicit stop-send requests pause the mission.
- Use a SQLite fenced cursor lease plus Win32 snapshot checks for cooperating RESOLVE processes. This does not enforce an OS-wide cursor lock.
- Keep all 80 imported concept entries PLANNED until an actual adapter and tests exist. The workspace list is a catalog, not a claim of 80 integrations.
- Report the eight-condition SQLite benchmark only as a targeted simulator experiment; separate GUI benchmark and ablations remain work to do.
