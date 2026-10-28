# Known limitations (current build)

- **Native Windows not verified in this build environment.** Win32 input, UIA inspection and the W1 Notepad workflow have code and opt-in tests but were not run here (`WINDOWS_NATIVE_TESTS_NOT_RUN`). Notepad window matching is by title; packaged Notepad on Windows 11 may hand off to another PID. Foreground changes between two observations can escape detection.
- **Browser actions are DOM-level** (Playwright), not physical cursor movement. A physical-cursor browser path is not implemented.
- **Intent understanding is template-based.** New goals must contain "for <First Last>"; other phrasing is recorded but not executed. No LLM provider is wired; provider-dependent generalisation does not exist yet.
- **Speech**: only the browser Web Speech API, which may use the vendor's cloud service; no offline STT/TTS; barge-in on audio is not automated. Typed transcripts exercise the same protocol.
- **Fixtures are ours.** The exactly-once property relies on the outbox's unique operation IDs. Real external systems may not offer such keys.
- **Benchmarks are small and constructed** (24 runs per policy, 4 per ablation cell); they show the mechanisms are needed for the targeted faults, not field reliability.
- **Skills** compile one demonstrated CRM flow; preconditions are limited to `contact_exists`.
- **Catalogue**: 11 of 80 concepts are TESTED through shared primitives; the rest are IDEA / SPECIFIED / BACKEND_IMPLEMENTED / PROVIDER_REQUIRED / BLOCKED.
- **Security**: the loopback token is readable by other processes of the same user; agents are trusted in-process code (capability checks are application policy, not an OS sandbox).
- **Packaging**: no installer; portable scripts only. A fresh second-machine Windows install has not been run in this session.
