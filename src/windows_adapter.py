"""Foreground-scoped Win32 observation and physical input.

HWNDs and snapshot IDs are short-lived locators, never semantic entity IDs.
Physical input needs an unlocked interactive desktop and the target window in
the foreground. This module does not bypass UIPI, UAC, or secure desktops.
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

IS_WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parents[1]
_input_lock = threading.RLock()
_state_lock = threading.RLock()
_snapshots: dict[str, dict] = {}
_foreground_generation = 0
_last_foreground_hwnd: int | None = None
_paused = threading.Event()
_stopped = threading.Event()
SNAPSHOT_MAX_AGE = 3.0


class UnsupportedDesktop(RuntimeError):
    """The requested desktop capability cannot run in the current state."""


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise UnsupportedDesktop("Windows control is unavailable on this operating system")


class _INPUT(ctypes.Structure):
    class U(ctypes.Union):
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", U)]


def _api():
    _require_windows()
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    if hasattr(user32, "GetDpiForWindow"):
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
    return user32


def _window_info(user32, hwnd: int, foreground: int, generation: int) -> dict:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise UnsupportedDesktop("could not read target window bounds")
    length = min(user32.GetWindowTextLengthW(hwnd), 4095)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, len(title))
    dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else None
    return {
        "hwnd": int(hwnd), "title": title.value, "pid": int(pid.value),
        "foreground": int(hwnd) == int(foreground),
        "foreground_generation": generation,
        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "dpi": int(dpi) if dpi else None,
    }


def _generation(foreground: int) -> int:
    global _foreground_generation, _last_foreground_hwnd
    with _state_lock:
        if foreground != _last_foreground_hwnd:
            _last_foreground_hwnd = foreground
            _foreground_generation += 1
        return _foreground_generation


def list_windows() -> dict:
    """Enumerate visible titled top-level windows and issue short-lived snapshots."""
    user32 = _api()
    foreground = int(user32.GetForegroundWindow() or 0)
    generation = _generation(foreground)
    observed_at = time.time()
    rows: list[dict] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        info = _window_info(user32, int(hwnd), foreground, generation)
        if not info["title"]:
            return True
        snapshot_id = uuid.uuid4().hex
        info["snapshot_id"] = snapshot_id
        info["observed_at"] = observed_at
        with _state_lock:
            _snapshots[snapshot_id] = info.copy()
        rows.append(info)
        return True

    user32.EnumWindows(callback_type(callback), 0)
    with _state_lock:
        # Bound retained observation metadata. The IDs are process local.
        for key, value in list(_snapshots.items()):
            if observed_at - value["observed_at"] > SNAPSHOT_MAX_AGE:
                _snapshots.pop(key, None)
        while len(_snapshots) > 512:
            _snapshots.pop(next(iter(_snapshots)))
    return {
        "observed_at": observed_at, "foreground_hwnd": foreground,
        "foreground_generation": generation, "windows": rows,
        "provenance": "Win32 EnumWindows/GetForegroundWindow/GetWindowRect",
        "coordinate_space": "Win32 screen coordinates in the process DPI context",
        "semantic_uia": "UNSUPPORTED",
    }


def _guard(hwnd: int, snapshot_id: str | None = None) -> dict:
    user32 = _api()
    if not hwnd or not user32.IsWindow(hwnd):
        raise UnsupportedDesktop("target window is no longer valid")
    foreground = int(user32.GetForegroundWindow() or 0)
    generation = _generation(foreground)
    if foreground != int(hwnd):
        raise UnsupportedDesktop(
            f"foreground changed (expected HWND {hwnd}, current {foreground}); no input sent"
        )
    current = _window_info(user32, hwnd, foreground, generation)
    if not current["visible"] or current["minimized"]:
        raise UnsupportedDesktop("target window is hidden or minimized")
    if snapshot_id is not None:
        with _state_lock:
            observed = _snapshots.get(snapshot_id)
        if observed is None:
            raise UnsupportedDesktop("window snapshot is unknown or expired; observe again")
        if time.time() - observed["observed_at"] > SNAPSHOT_MAX_AGE:
            raise UnsupportedDesktop("window snapshot is stale; observe again")
        for key in ("hwnd", "pid", "bounds", "foreground_generation"):
            if observed[key] != current[key]:
                raise UnsupportedDesktop(f"window {key} changed; observe again")
    return current


def validate_snapshot(hwnd: int, snapshot_id: str) -> dict:
    """Read-only guard useful before planning a physical action."""
    return _guard(int(hwnd), snapshot_id)


def validate_target(
    hwnd: int, snapshot_id: str, x: int | None = None, y: int | None = None
) -> dict:
    """Preflight a desktop action without dispatching physical input."""
    _check_control()
    info = _guard(int(hwnd), snapshot_id)
    if (x is None) != (y is None):
        raise ValueError("provide both screen coordinates or neither")
    if x is not None and not _inside(info, int(x), int(y)):
        raise ValueError("coordinates are outside the selected window bounds")
    return info


def pause() -> dict:
    _paused.set()
    return control_state()


def resume() -> dict:
    _paused.clear()
    return control_state()


def stop() -> dict:
    """Emergency stop: cancel in-flight input between atomic SendInput calls."""
    _stopped.set()
    _paused.clear()
    return control_state()


def reset_stop() -> dict:
    """Explicitly rearm the local adapter after an emergency stop."""
    _stopped.clear()
    return control_state()


def control_state() -> dict:
    return {"paused": _paused.is_set(), "stopped": _stopped.is_set()}


def _check_control() -> None:
    if _stopped.is_set():
        raise UnsupportedDesktop("emergency stop is active; no input sent")
    deadline = time.monotonic() + 10
    while _paused.is_set():
        if _stopped.is_set():
            raise UnsupportedDesktop("emergency stop cancelled the input")
        if time.monotonic() >= deadline:
            raise TimeoutError("input remained paused for 10 seconds")
        time.sleep(0.02)


@contextlib.contextmanager
def _desktop_turn():
    """Cooperative RESOLVE process lock; other software is outside its scope."""
    _require_windows()
    import msvcrt

    lock_path = ROOT / "data" / "cursor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _input_lock, open(lock_path, "a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + 2
        while True:
            _check_control()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another RESOLVE process owns the cursor")
                time.sleep(0.01)
        try:
            yield
        finally:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _send(*events: _INPUT) -> None:
    if not events:
        return
    _check_control()
    user32 = _api()
    batch = (_INPUT * len(events))(*events)
    if user32.SendInput(len(batch), batch, ctypes.sizeof(_INPUT)) != len(batch):
        raise UnsupportedDesktop("SendInput failed; target may be elevated or desktop may be protected")


def _mouse(flags: int, data: int = 0) -> _INPUT:
    value = _INPUT()
    value.type = 0
    value.mi = _INPUT.U.MOUSEINPUT(0, 0, data, flags, 0, 0)
    return value


def _keyboard(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    value = _INPUT()
    value.type = 1
    value.ki = _INPUT.U.KEYBDINPUT(vk, scan, flags, 0, 0)
    return value


def _point() -> tuple[int, int]:
    point = wintypes.POINT()
    if not _api().GetCursorPos(ctypes.byref(point)):
        raise UnsupportedDesktop("could not read cursor position")
    return point.x, point.y


def _inside(info: dict, x: int, y: int) -> bool:
    left, top, right, bottom = info["bounds"]
    return left <= x < right and top <= y < bottom


def move_pointer(
    hwnd: int, x: int, y: int, snapshot_id: str | None = None, *, smooth: bool = True
) -> dict:
    x, y = int(x), int(y)
    with _desktop_turn():
        info = _guard(hwnd, snapshot_id)
        if not _inside(info, x, y):
            raise ValueError("pointer coordinates are outside the selected window bounds")
        start_x, start_y = _point()
        steps = min(24, max(1, int(max(abs(x - start_x), abs(y - start_y)) / 32))) if smooth else 1
        last = (start_x, start_y)
        for step in range(1, steps + 1):
            _check_control()
            info = _guard(hwnd, snapshot_id)
            next_x = round(start_x + (x - start_x) * step / steps)
            next_y = round(start_y + (y - start_y) * step / steps)
            if not _inside(info, next_x, next_y) and step == steps:
                raise UnsupportedDesktop("window moved before pointer reached target")
            current = _point()
            if step > 1 and max(abs(current[0] - last[0]), abs(current[1] - last[1])) > 8:
                raise UnsupportedDesktop("cursor moved outside RESOLVE control; user takeover detected")
            if not _api().SetCursorPos(next_x, next_y):
                raise UnsupportedDesktop("SetCursorPos failed")
            last = (next_x, next_y)
            if step < steps:
                time.sleep(0.008)
    return {"status": "DISPATCHED", "hwnd": hwnd, "x": x, "y": y, "steps": steps}


def click(
    hwnd: int, x: int | None = None, y: int | None = None,
    snapshot_id: str | None = None, *, button: str = "left", count: int = 1
) -> dict:
    if x is None or y is None:
        raise ValueError("click requires explicit screen coordinates inside the selected window")
    if button not in {"left", "right"} or count not in {1, 2}:
        raise ValueError("unsupported click button or count")
    x, y = int(x), int(y)
    with _desktop_turn():
        info = _guard(hwnd, snapshot_id)
        if not _inside(info, x, y):
            raise ValueError("click coordinates are outside the selected window bounds")
        if not _api().SetCursorPos(x, y):
            raise UnsupportedDesktop("SetCursorPos failed")
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        for _ in range(count):
            _check_control()
            _guard(hwnd, snapshot_id)
            if _point() != (x, y):
                raise UnsupportedDesktop("cursor moved before click; user takeover detected")
            _send(_mouse(down), _mouse(up))
    return {"status": "DISPATCHED", "hwnd": hwnd, "button": button, "count": count, "x": x, "y": y}


def type_text(hwnd: int, value: str, snapshot_id: str | None = None) -> dict:
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("text must be a string of at most 2000 characters")
    with _desktop_turn():
        for char in value:
            _check_control()
            _guard(hwnd, snapshot_id)
            units = char.encode("utf-16-le")
            for i in range(0, len(units), 2):
                code_unit = int.from_bytes(units[i:i + 2], "little")
                _send(_keyboard(scan=code_unit, flags=0x0004), _keyboard(scan=code_unit, flags=0x0004 | 0x0002))
    return {"status": "DISPATCHED", "hwnd": hwnd, "characters": len(value)}


KEYS = {"ENTER": 0x0D, "TAB": 0x09, "ESC": 0x1B, "BACKSPACE": 0x08,
        "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
        "HOME": 0x24, "END": 0x23, "DELETE": 0x2E}


def press_key(hwnd: int, key: str, snapshot_id: str | None = None) -> dict:
    vk = KEYS.get(key.upper())
    if vk is None:
        raise ValueError("unsupported key")
    with _desktop_turn():
        _guard(hwnd, snapshot_id)
        _send(_keyboard(vk=vk), _keyboard(vk=vk, flags=0x0002))
    return {"status": "DISPATCHED", "hwnd": hwnd, "key": key.upper()}


MODIFIERS = {"CTRL": 0x11, "SHIFT": 0x10, "ALT": 0x12}


def press_chord(hwnd: int, keys: list[str], snapshot_id: str | None = None) -> dict:
    """Modifier chord such as ["CTRL", "S"]; only letters/digits plus allowlisted modifiers."""
    if not keys or len(keys) > 3:
        raise ValueError("chord needs 1-3 keys")
    codes = []
    for key in keys:
        key = key.upper()
        if key in MODIFIERS:
            codes.append(MODIFIERS[key])
        elif len(key) == 1 and key.isalnum():
            codes.append(ord(key))
        elif key in KEYS:
            codes.append(KEYS[key])
        else:
            raise ValueError(f"unsupported chord key {key}")
    with _desktop_turn():
        _check_control()
        _guard(hwnd, snapshot_id)
        _send(*[_keyboard(vk=c) for c in codes], *[_keyboard(vk=c, flags=0x0002) for c in reversed(codes)])
    return {"status": "DISPATCHED", "hwnd": hwnd, "chord": [k.upper() for k in keys]}


def focus_window(hwnd: int) -> bool:
    """Ask Windows to foreground a window. Windows may refuse (foreground lock); callers must re-check."""
    user32 = _api()
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


def close_window(hwnd: int) -> None:
    _api().PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def scroll(hwnd: int, steps: int, snapshot_id: str | None = None) -> dict:
    if not -5 <= steps <= 5 or steps == 0:
        raise ValueError("scroll steps must be between -5 and 5, excluding zero")
    with _desktop_turn():
        _guard(hwnd, snapshot_id)
        _send(_mouse(0x0800, 120 * steps))
    return {"status": "DISPATCHED", "hwnd": hwnd, "steps": steps}


def capture_window(hwnd: int, snapshot_id: str | None = None, output: str | Path | None = None) -> dict:
    """Capture visible screen pixels in the foreground window rectangle.

    Screen crop provenance is explicit: overlapping popups can appear in the image.
    Evidence is written only inside this repository's data/evidence directory.
    """
    info = _guard(hwnd, snapshot_id)
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise UnsupportedDesktop("screenshot capture requires optional Pillow") from exc
    evidence_dir = (ROOT / "data" / "evidence").resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = Path(output).resolve() if output else evidence_dir / f"window-{uuid.uuid4().hex}.png"
    if not destination.is_relative_to(evidence_dir) or destination.suffix.lower() != ".png":
        raise ValueError("screenshot output must be a PNG inside data/evidence")
    _guard(hwnd, snapshot_id)
    image = ImageGrab.grab(bbox=tuple(info["bounds"]), all_screens=True)
    after = _guard(hwnd, snapshot_id)
    if after["bounds"] != info["bounds"]:
        raise UnsupportedDesktop("window moved during screenshot capture")
    image.save(destination, "PNG")
    raw = destination.read_bytes()
    return {
        "status": "CAPTURED", "hwnd": hwnd, "snapshot_id": snapshot_id,
        "path": str(destination), "sha256": hashlib.sha256(raw).hexdigest(),
        "size": list(image.size), "bytes": len(raw),
        "provenance": "Pillow ImageGrab screen crop of foreground Win32 window bounds",
    }
