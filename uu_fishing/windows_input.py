"""Windows foreground-window verification and physical keyboard input."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
from functools import cache
import math
import os
import threading
import time


class FocusError(RuntimeError):
    """The locked window is not safe to send input to."""


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int
    app_name: str
    title: str

    @property
    def label(self) -> str:
        return f"{self.app_name} — {self.title}"


class _KeyboardInput(c.Structure):
    _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", w.WPARAM)]


class _MouseInput(c.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("data", w.DWORD),
                ("flags", w.DWORD), ("time", w.DWORD), ("extra", w.WPARAM)]


class _InputPayload(c.Union):
    _fields_ = [("keyboard", _KeyboardInput), ("mouse", _MouseInput)]


class _Input(c.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("kind", w.DWORD), ("payload", _InputPayload)]


def _bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


@cache
def _api():
    user = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    enum_proc = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    return _WinAPI(user, kernel, enum_proc)


class _WinAPI:
    def __init__(self, user, kernel, enum_proc):
        self.enum_proc = enum_proc
        self.enumerate = _bind(user, "EnumWindows", w.BOOL, enum_proc, w.LPARAM)
        self.foreground = _bind(user, "GetForegroundWindow", w.HWND)
        self.visible = _bind(user, "IsWindowVisible", w.BOOL, w.HWND)
        self.minimized = _bind(user, "IsIconic", w.BOOL, w.HWND)
        self.window_pid = _bind(user, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
        self.title = _bind(user, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        self.class_name = _bind(user, "GetClassNameW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        self.key_state = _bind(user, "GetAsyncKeyState", c.c_short, c.c_int)
        self.map_key = _bind(user, "MapVirtualKeyW", w.UINT, w.UINT, w.UINT)
        self.send = _bind(user, "SendInput", w.UINT, w.UINT, c.POINTER(_Input), c.c_int)
        self.open_process = _bind(kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        self.path = _bind(kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD,
                          w.LPWSTR, c.POINTER(w.DWORD))
        self.close = _bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)


def _pid(handle: int) -> int:
    pid = w.DWORD()
    return int(pid.value) if _api().window_pid(handle, c.byref(pid)) else 0


def _app_name(pid: int) -> str:
    api = _api()
    process = api.open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ""
    try:
        text = c.create_unicode_buffer(32768)
        length = w.DWORD(len(text))
        return os.path.basename(text.value) if api.path(process, 0, text, c.byref(length)) else ""
    finally:
        api.close(process)


def _window(handle: int | None) -> WindowInfo | None:
    api = _api()
    if not handle or not api.visible(handle) or api.minimized(handle):
        return None
    pid = _pid(handle)
    if not pid or pid == os.getpid():
        return None
    kind = c.create_unicode_buffer(256)
    api.class_name(handle, kind, len(kind))
    if kind.value in {"Progman", "WorkerW", "Shell_TrayWnd", "ConsoleWindowClass"}:
        return None
    title = c.create_unicode_buffer(1024)
    api.title(handle, title, len(title))
    if not title.value.strip():
        return None
    return WindowInfo(handle, pid, _app_name(pid) or title.value, title.value)


def focused_window_info() -> WindowInfo | None:
    return _window(_api().foreground())


def list_windows() -> list[WindowInfo]:
    api = _api()
    handles: list[int] = []

    @api.enum_proc
    def collect(handle, _):
        handles.append(handle)
        return True

    if not api.enumerate(collect, 0):
        raise c.WinError(c.get_last_error())
    return [item for handle in handles if (item := _window(handle)) is not None]


def is_focused(target: WindowInfo) -> bool:
    api = _api()
    handle = target.window_id
    return (api.foreground() == handle and _pid(handle) == target.pid
            and bool(api.visible(handle)) and not api.minimized(handle))


_SCANS = {
    **dict(zip("1234567890-=", range(0x02, 0x0E))),
    **dict(zip("qwertyuiop[]", range(0x10, 0x1C))),
    **dict(zip("asdfghjkl;'`", range(0x1E, 0x2A))),
    **dict(zip("\\zxcvbnm,./", range(0x2B, 0x36))),
    **{f"f{i}": 0x3A + i for i in range(1, 11)},
    "f11": 0x57, "f12": 0x58,
    "escape": 0x01, "backspace": 0x0E, "tab": 0x0F, "enter": 0x1C, "space": 0x39,
    "home": 0xE047, "up": 0xE048, "pageup": 0xE049, "left": 0xE04B,
    "right": 0xE04D, "end": 0xE04F, "down": 0xE050, "pagedown": 0xE051,
    "insert": 0xE052, "delete": 0xE053,
}
_ALIASES = {"backtick": "`", "grave": "`", "esc": "escape", "return": "enter"}
_MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, Windows keys
_key_lock = threading.Lock()


def _send_scan(scan: int, *, up: bool) -> None:
    flags = 0x0008 | (0x0002 if up else 0) | (0x0001 if scan >> 8 else 0)
    event = _Input(kind=1)
    event.keyboard = _KeyboardInput(0, scan & 0xff, flags, 0, 0)
    c.set_last_error(0)
    if _api().send(1, c.byref(event), c.sizeof(event)) != 1:
        raise FocusError(f"Windows 未接受按键（SendInput，错误码 {c.get_last_error()}）；请确认远程客户端没有以管理员身份运行")


class WindowsKeyboard:
    def __init__(self, target: WindowInfo, key: str = "`", hold_seconds: float = 0.06):
        name = key.strip().casefold()
        scan = _SCANS.get(_ALIASES.get(name, name))
        if scan is None:
            raise ValueError(f"Windows 不支持按键 {key!r}")
        if not math.isfinite(hold_seconds) or not 0 <= hold_seconds <= 0.5:
            raise ValueError("按住时间必须在 0～0.5 秒之间")
        self.target, self.scan, self.hold_seconds = target, scan, hold_seconds

    def press(self, *, targeted: bool = True, cancelled=lambda: False, on_submitted=None) -> None:
        if not targeted:
            raise ValueError("Windows 发键必须启用前台窗口校验")
        with _key_lock:
            api = _api()
            if any(api.key_state(mod) & 0x8000 for mod in _MODIFIERS):
                raise FocusError("请松开 Ctrl、Alt、Shift 和 Windows 键")
            vk = api.map_key(self.scan, 3)  # MAPVK_VSC_TO_VK_EX
            if vk and api.key_state(vk) & 0x8000:
                raise FocusError("目标按键正被手动按住")
            if cancelled() or not is_focused(self.target):
                raise FocusError("已停止或目标窗口失焦；未发键")
            # SendInput goes to the current foreground, not directly to an HWND.
            _send_scan(self.scan, up=False)
            try:
                if on_submitted is not None:
                    on_submitted()
                end = time.monotonic() + self.hold_seconds
                while not cancelled() and is_focused(self.target):
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.01, remaining))
            finally:
                # Release an already-sent down event even if the user switched windows.
                _send_scan(self.scan, up=True)
            if cancelled() or not is_focused(self.target):
                raise FocusError("已停止或目标窗口失焦；已松键")
