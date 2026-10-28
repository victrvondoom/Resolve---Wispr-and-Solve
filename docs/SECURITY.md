# Security and privacy

- **Network surface**: binds 127.0.0.1 only; rejects foreign Host/Origin; mutations need a per-process token and JSON content type. Another process running as the same user can read the token — this is not a malware boundary.
- **Untrusted content**: page text, documents and fixture data are treated as data. No instruction found in content is executed; plans come only from user utterances through the deterministic interpreter.
- **Consequential actions**: external communication requires an approval bound to the exact digest; corrections, cancels and re-plans invalidate pending approvals. Real email, payments, trading, purchases and credential access are not implemented. The outbox is labelled SIMULATED DELIVERY.
- **Fault injection** (`/_fault/*`) is refused with 403 unless the fixtures run in test mode (`RESOLVE_TEST_MODE=1`).
- **Files**: the file worker refuses absolute paths, `..`, any symlink component and anything outside its sandbox after realpath; it never overwrites.
- **Windows**: no elevation, no UAC or secure-desktop bypass. Input is refused when the foreground HWND, PID, bounds or focus generation changed since observation; emergency stop revokes the cursor lease. User mouse movement during a pointer move is treated as takeover.
- **Data**: SQLite under `data/`; no telemetry leaves the machine. Browser speech recognition may send audio to the browser vendor — the UI says so before enabling the mic.
- **Code reuse**: no third-party hackathon code is copied.
