"""Optional, read-only Windows UI Automation inspection.

The adapter imports ``uiautomation`` only when used. It never persists element
names, invokes controls, or treats RuntimeIds/HWNDs as stable entity identity.
Traversal limits bound the number of COM calls made by normal UI trees; a
provider that blocks inside one COM call can still exceed the soft deadline.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
import uuid
from collections import deque
from ctypes import wintypes
from importlib.metadata import PackageNotFoundError, version
from typing import Any


class UIAUnavailable(RuntimeError):
    pass


def availability() -> dict[str, Any]:
    if os.name != 'nt':
        return {'available': False, 'reason': 'Windows is required'}
    try:
        import uiautomation  # noqa: F401
    except (ImportError, OSError) as exc:
        return {'available': False, 'reason': f'optional uiautomation package unavailable: {exc}'}
    try:
        installed_version = version('uiautomation')
    except PackageNotFoundError:
        installed_version = 'unknown'
    return {'available': True, 'package': 'uiautomation', 'version': installed_version}


def _property(control: Any, name: str, errors: list[str]) -> Any:
    try:
        return getattr(control, name)
    except Exception as exc:
        errors.append(f'{name}: {type(exc).__name__}')
        return None


def _describe(control: Any, index: int, parent_index: int | None, depth: int) -> dict[str, Any]:
    errors: list[str] = []
    rect = _property(control, 'BoundingRectangle', errors)
    bounds = None
    if rect is not None:
        try:
            bounds = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
        except Exception as exc:
            errors.append(f'BoundingRectangle conversion: {type(exc).__name__}')
    offscreen = _property(control, 'IsOffscreen', errors)
    return {
        'index': index,
        'parent_index': parent_index,
        'depth': depth,
        'name': _property(control, 'Name', errors),
        'control_type': _property(control, 'ControlTypeName', errors),
        'automation_id': _property(control, 'AutomationId', errors),
        'native_window_handle': _property(control, 'NativeWindowHandle', errors),
        'process_id': _property(control, 'ProcessId', errors),
        'bounds': bounds,
        'enabled': _property(control, 'IsEnabled', errors),
        'visible': None if offscreen is None else not bool(offscreen),
        'property_errors': errors,
    }


def inspect_window(
    hwnd: int, *, max_depth: int = 4, max_nodes: int = 96,
    max_children: int = 24, timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Inspect the UIA subtree rooted at a live HWND with bounded breadth/depth.

    Returns a flat parent-indexed tree. Names and AutomationIds are observation
    evidence, not persistent semantic IDs. This function performs no UI action.
    """
    if not 0 <= max_depth <= 8:
        raise ValueError('max_depth must be between 0 and 8')
    if not 1 <= max_nodes <= 256:
        raise ValueError('max_nodes must be between 1 and 256')
    if not 1 <= max_children <= 64:
        raise ValueError('max_children must be between 1 and 64')
    if not 0.1 <= timeout_seconds <= 10.0:
        raise ValueError('timeout_seconds must be between 0.1 and 10')
    if os.name != 'nt':
        raise UIAUnavailable('Windows is required')
    try:
        import uiautomation as automation
    except (ImportError, OSError) as exc:
        raise UIAUnavailable('optional uiautomation package is not installed') from exc

    hwnd = int(hwnd)
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    if not hwnd or not user32.IsWindow(hwnd):
        raise ValueError('HWND is not a live window')

    started = time.monotonic()
    observed_at = time.time()
    try:
        root = automation.ControlFromHandle(hwnd)
    except Exception as exc:
        raise UIAUnavailable(f'UI Automation could not attach: {type(exc).__name__}: {exc}') from exc
    if root is None:
        raise UIAUnavailable('UI Automation provider returned no element for HWND')

    queue: deque[tuple[Any, int | None, int]] = deque([(root, None, 0)])
    nodes: list[dict[str, Any]] = []
    truncated: set[str] = set()
    while queue:
        if time.monotonic() - started >= timeout_seconds:
            truncated.add('deadline')
            break
        if len(nodes) >= max_nodes:
            truncated.add('max_nodes')
            break
        control, parent_index, depth = queue.popleft()
        index = len(nodes)
        nodes.append(_describe(control, index, parent_index, depth))
        if depth >= max_depth:
            truncated.add('max_depth')
            continue
        if len(nodes) + len(queue) >= max_nodes:
            truncated.add('max_nodes')
            continue
        try:
            child = control.GetFirstChildControl()
        except Exception as exc:
            nodes[-1]['property_errors'].append(f'GetFirstChildControl: {type(exc).__name__}')
            continue
        child_count = 0
        while child is not None:
            if time.monotonic() - started >= timeout_seconds:
                truncated.add('deadline')
                break
            if child_count >= max_children:
                truncated.add('max_children')
                break
            if len(nodes) + len(queue) >= max_nodes:
                truncated.add('max_nodes')
                break
            queue.append((child, index, depth + 1))
            child_count += 1
            try:
                child = child.GetNextSiblingControl()
            except Exception as exc:
                nodes[-1]['property_errors'].append(f'GetNextSiblingControl: {type(exc).__name__}')
                break

    canonical = json.dumps(nodes, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    return {
        'status': 'OBSERVED',
        'snapshot_id': uuid.uuid4().hex,
        'observed_at': observed_at,
        'application_identity': {'process_id': nodes[0]['process_id'] if nodes else None},
        'window_identity': {'hwnd': hwnd},
        'provenance': f'Microsoft UI Automation via uiautomation {availability()["version"]}',
        'nodes': nodes,
        'node_count': len(nodes),
        'truncated_by': sorted(truncated),
        'content_sha256': hashlib.sha256(canonical).hexdigest(),
        'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
    }
