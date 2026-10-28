# Optional UI Automation observation

`src/uia_adapter.py` provides read-only inspection of a live Windows HWND through Microsoft UI Automation. It reports element name, control type, AutomationId, bounds, process ID, enabled and visible state, provenance, timestamp, and a content hash. The flat result includes parent indexes and is bounded by depth, node count, children per element, and a soft deadline. Element names can contain sensitive on-screen text; callers should keep snapshots local and avoid posting them to external services.

## Install

The default RESOLVE service has no dependency on this adapter. To enable it inside the project-local environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements-optional.txt
.\.venv\Scripts\python.exe -m unittest tests.test_uia_adapter -v
```

Pinned packages: `uiautomation==2.0.29`, `comtypes==1.4.17`. [PyPI identifies uiautomation 2.0.29 as an Apache-2.0 package released 5 August 2025](https://pypi.org/project/uiautomation/2.0.29/); the [project repository](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) documents its Windows UI Automation wrapper. No third-party source code was copied into RESOLVE.

## Use

```python
from uia_adapter import availability, inspect_window

if availability()['available']:
    snapshot = inspect_window(hwnd, max_depth=3, max_nodes=64,
                              max_children=16, timeout_seconds=3)
```

The adapter never invokes, types, clicks, or modifies a control. HWNDs and UIA runtime identifiers are transient locators, not durable entity IDs. UIA providers may omit names or AutomationIds; the caller must treat missing data as unresolved. The deadline is checked between COM calls, so a provider that blocks inside a single COM call can exceed it. Locked, elevated, secure, or inaccessible desktops are unsupported; this adapter does not bypass Windows security boundaries.

## Verified here

- Windows 11 x64, Python 3.14.7, project-local `.venv`.
- `uiautomation==2.0.29` and `comtypes==1.4.17` installed and imported in that environment.
- A controlled Tk window produced an `OBSERVED` snapshot with 3 elements (`PaneControl`, `ButtonControl`, `ImageControl`), no truncation. The provider returned empty names for those elements. This confirms real UIA inspection but also demonstrates that provider metadata can be sparse.
- Two UIA adapter tests passed in the optional environment; under default global Python, one limit test passed and the live UIA test skipped because the optional package is absent.

The dashboard's **Inspect UI elements** action and `/api/uia/inspect` route now call this read-only adapter after checking the mission revision and fresh Win32 window snapshot. The controlled HTTP-to-UIA test passed in the optional environment. Semantic UIA actions remain unsupported; physical input still uses Win32 snapshot-bounded coordinates.
