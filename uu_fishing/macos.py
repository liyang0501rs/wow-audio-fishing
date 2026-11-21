"""Metadata-only macOS windows and fail-closed, foreground Quartz key presses.

No local game process, image capture or application activation. AppKit is loaded lazily for frontmost-app queries.
Window titles can be unavailable without additional OS permissions: in that case
``title`` is the application name, NOT an invented remote-session title.

The ctypes window metadata helpers, framework signatures, and ANSI key table
are adapted from rsamf/wow-fishing-forever at this exact revision:
https://github.com/rsamf/wow-fishing-forever/tree/a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7
(src/wowfish/input/{_quartz.py,keys.py}, capture/_quartz.py).
No capture, activation, PID-only focus, or modifier-clearing code was copied.

MIT License
Copyright (c) 2019-2026 Richard Franklin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import ctypes
import math
import platform
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


class MacError(RuntimeError):
    """macOS input or permission checks could not be completed safely."""


class FocusError(MacError):
    """The selected window is closed, unknown, or not the foreground window."""


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int
    app_name: str
    title: str

    @property
    def label(self) -> str:
        name = self.app_name if self.title == self.app_name else f"{self.app_name} — {self.title}"
        return f"{name} [window_id={self.window_id}, pid={self.pid}]"


@dataclass(frozen=True)
class AppInfo:
    pid: int
    app_name: str
    launch_time: float | None = None


def frontmost_app_info() -> AppInfo | None:
    """Use AppKit's application identity without asking AX for a window."""
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None or app.isTerminated():
        return None
    pid = int(app.processIdentifier())
    if pid <= 0:
        return None
    launched = app.launchDate()
    return AppInfo(pid, str(app.localizedName() or app.bundleIdentifier() or "前台应用"),
                   float(launched.timeIntervalSinceReferenceDate()) if launched is not None else None)


def _app_is_focused(target: AppInfo) -> bool:
    current = frontmost_app_info()
    return (current is not None and current.pid == target.pid and current.app_name == target.app_name
            and (target.launch_time is None or current.launch_time == target.launch_time))


_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_APPLICATION_SERVICES = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_ON_SCREEN_ONLY = 1 << 0
_EXCLUDE_DESKTOP = 1 << 4
_UTF8 = 0x08000100
_SINT64 = 4
_DOUBLE = 13
_HID_SYSTEM_STATE = 1
_HID_EVENT_TAP = 0
_MIN_WIDTH = 64
_MIN_HEIGHT = 48
_MODIFIER_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)
_MODIFIER_KEYS = {
    0x38: 1 << 17, 0x3C: 1 << 17,  # left/right Shift
    0x3B: 1 << 18, 0x3E: 1 << 18,  # left/right Control
    0x3A: 1 << 19, 0x3D: 1 << 19,  # left/right Option
    0x37: 1 << 20, 0x36: 1 << 20,  # left/right Command
}
def _ax_hint() -> str:
    parts = platform.mac_ver()[0].split('.')
    version = tuple(int(p) for p in parts[:2] if p.isdecimal())
    if version >= (27, 3):
        return ('系统设置 > 隐私与安全 > System & App Protection Override（27.3 Beta 2 简中仍显示英文）。'
                '找到本次启动程序的终端/IDE；若可「选取App」，只选实际使用的目标应用。不要开启整机控制。')
    if version >= (27,):
        return '在「隐私与安全」查找设备控制与数据访问（Device Control and Data Access），以系统提示为准。'
    return '在「隐私与安全性」查看辅助功能（Accessibility）中本次启动程序的终端/IDE。'


_INPUT_HINT = '在系统设置的隐私页查找「输入监控」（Input Monitoring）。仅 F8/F9 全局热键使用；单键测试不需要。'

# Physical US ANSI key positions, not Unicode characters or key combinations.
_KEYCODES: dict[str, int] = {
    **dict(zip("asdfhgzxcv", range(0x00, 0x0A))),
    **dict(zip("bqwerty", range(0x0B, 0x12))),
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18, "9": 0x19,
    "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21,
    "i": 0x22, "p": 0x23, "enter": 0x24, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29,
    "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "tab": 0x30,
    "space": 0x31, "`": 0x32, "backspace": 0x33, "escape": 0x35,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62,
    "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79, "delete": 0x75,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}
_KEY_ALIASES = {
    "backtick": "`", "grave": "`", "backquote": "`", "grave_accent": "`",
    "return": "enter", "esc": "escape", "spacebar": "space",
    "page_up": "pageup", "page_down": "pagedown", "forward_delete": "delete",
}


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise MacError("真实窗口与键盘 IO 仅支持 macOS（darwin）；离线测试不应启动 Mac IO")


def _bind(lib: Any, name: str, args: list[Any], result: Any, *, optional: bool = False) -> Any:
    fn = getattr(lib, name, None)
    if fn is None:
        if optional:
            return None
        raise MacError(f"macOS framework 缺少必要 API：{name}")
    fn.argtypes, fn.restype = args, result
    return fn


class _MacFrameworks:
    """Small, lazy IO boundary; tests replace it with a plain Python fake."""

    def __init__(self) -> None:
        _require_darwin()
        try:
            self._cg = ctypes.CDLL(_CORE_GRAPHICS)
            self._cf = ctypes.CDLL(_CORE_FOUNDATION)
            self._ax = ctypes.CDLL(_APPLICATION_SERVICES)
        except OSError as exc:
            raise MacError(f"无法加载 macOS 系统 frameworks：{exc}") from exc
        vp, idx = ctypes.c_void_p, ctypes.c_ssize_t
        for lib, name, args, result in (
            (self._cf, "CFArrayGetCount", [vp], idx),
            (self._cf, "CFArrayGetValueAtIndex", [vp, idx], vp),
            (self._cf, "CFDictionaryGetValue", [vp, vp], vp),
            (self._cf, "CFDictionaryCreate", [vp, vp, vp, idx, vp, vp], vp),
            (self._cf, "CFStringCreateWithCString", [vp, ctypes.c_char_p, ctypes.c_uint32], vp),
            (self._cf, "CFStringGetLength", [vp], idx),
            (self._cf, "CFStringGetMaximumSizeForEncoding", [idx, ctypes.c_uint32], idx),
            (self._cf, "CFStringGetCString", [vp, ctypes.c_char_p, idx, ctypes.c_uint32], ctypes.c_bool),
            (self._cf, "CFNumberGetValue", [vp, idx, vp], ctypes.c_bool),
            (self._cf, "CFBooleanGetValue", [vp], ctypes.c_bool),
            (self._cf, "CFRelease", [vp], None),
            (self._cg, "CGWindowListCopyWindowInfo", [ctypes.c_uint32, ctypes.c_uint32], vp),
            (self._cg, "CGRectMakeWithDictionaryRepresentation", [vp, ctypes.POINTER(_CGRect)], ctypes.c_bool),
            (self._cg, "CGEventSourceFlagsState", [ctypes.c_int32], ctypes.c_uint64),
            (self._cg, "CGEventSourceKeyState", [ctypes.c_int32, ctypes.c_uint16], ctypes.c_bool),
            (self._cg, "CGEventCreateKeyboardEvent", [vp, ctypes.c_uint16, ctypes.c_bool], vp),
            (self._cg, "CGEventPost", [ctypes.c_uint32, vp], None),
            (self._ax, "AXIsProcessTrusted", [], ctypes.c_bool),
            (self._ax, "AXUIElementCreateSystemWide", [], vp),
            (self._ax, "AXUIElementCreateApplication", [ctypes.c_int], vp),
            (self._ax, "AXUIElementCopyAttributeValue", [vp, vp, ctypes.POINTER(vp)], ctypes.c_int32),
            (self._ax, "AXUIElementGetPid", [vp, ctypes.POINTER(ctypes.c_int)], ctypes.c_int32),
        ):
            _bind(lib, name, args, result)
        self._trusted_prompt = _bind(self._ax, "AXIsProcessTrustedWithOptions", [vp], ctypes.c_bool, optional=True)
        self._listen_preflight = _bind(self._cg, "CGPreflightListenEventAccess", [], ctypes.c_bool, optional=True)
        self._listen_request = _bind(self._cg, "CGRequestListenEventAccess", [], ctypes.c_bool, optional=True)
        self._post_preflight = _bind(self._cg, "CGPreflightPostEventAccess", [], ctypes.c_bool, optional=True)
        self._post_request = _bind(self._cg, "CGRequestPostEventAccess", [], ctypes.c_bool, optional=True)
        self._post_to_pid = _bind(self._cg, "CGEventPostToPid", [ctypes.c_int, vp], None, optional=True)
        # Exact AX -> CG window identity. Never substitute a PID/title/stack-order guess.
        self._ax_window_id = _bind(self._ax, "_AXUIElementGetWindow", [vp, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int32, optional=True)
        self._ax_timeout = _bind(self._ax, "AXUIElementSetMessagingTimeout", [vp, ctypes.c_float], ctypes.c_int32, optional=True)
        self._ax_value_type = _bind(self._ax, "AXValueGetType", [vp], ctypes.c_int32, optional=True)
        self._ax_value_get = _bind(self._ax, "AXValueGetValue", [vp, ctypes.c_int32, vp], ctypes.c_bool, optional=True)

    @staticmethod
    def _constant(lib: Any, name: str) -> int:
        return ctypes.c_void_p.in_dll(lib, name).value or 0

    def _string(self, ref: Any) -> str | None:
        if not ref:
            return None
        size = self._cf.CFStringGetMaximumSizeForEncoding(self._cf.CFStringGetLength(ref), _UTF8) + 1
        if size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        if not self._cf.CFStringGetCString(ref, buf, size, _UTF8):
            return None
        return buf.value.decode("utf-8", "replace")

    def _number(self, ref: Any, kind: int = _SINT64) -> float | int | None:
        if not ref:
            return None
        out = ctypes.c_double() if kind == _DOUBLE else ctypes.c_int64()
        if not self._cf.CFNumberGetValue(ref, kind, ctypes.byref(out)):
            return None
        return out.value

    def window_records(self) -> list[dict[str, Any]]:
        keys = {
            key: self._constant(self._cg, f"kCGWindow{key}")
            for key in ("Number", "OwnerPID", "OwnerName", "Name", "Layer", "Alpha", "Bounds", "IsOnscreen")
        }
        array = self._cg.CGWindowListCopyWindowInfo(_ON_SCREEN_ONLY | _EXCLUDE_DESKTOP, 0)
        if not array:
            raise MacError("CoreGraphics 无法读取窗口列表（没有返回窗口元数据）")
        records: list[dict[str, Any]] = []
        try:
            for index in range(self._cf.CFArrayGetCount(array)):
                item = self._cf.CFArrayGetValueAtIndex(array, index)
                if not item:
                    continue
                def value(key: str) -> Any:
                    return self._cf.CFDictionaryGetValue(item, keys[key])
                bounds, rect = value("Bounds"), _CGRect()
                if not bounds or not self._cg.CGRectMakeWithDictionaryRepresentation(bounds, ctypes.byref(rect)):
                    continue
                onscreen = value("IsOnscreen")
                records.append({
                    "window_id": self._number(value("Number")),
                    "pid": self._number(value("OwnerPID")),
                    "app_name": self._string(value("OwnerName")),
                    "title": self._string(value("Name")),
                    "layer": self._number(value("Layer")),
                    "alpha": self._number(value("Alpha"), _DOUBLE),
                    "onscreen": bool(onscreen and self._cf.CFBooleanGetValue(onscreen)),
                    "x": rect.origin.x,
                    "y": rect.origin.y,
                    "width": rect.size.width,
                    "height": rect.size.height,
                })
        finally:
            self._cf.CFRelease(array)
        return records

    def _copy_ax_attribute(self, element: Any, name: str) -> int | None:
        key = self._cf.CFStringCreateWithCString(None, name.encode("utf-8"), _UTF8)
        if not key:
            return None
        value = ctypes.c_void_p()
        try:
            status = self._ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
            if status == 0 and value.value:
                return value.value
            if value.value:
                self._cf.CFRelease(value.value)
            return None
        finally:
            self._cf.CFRelease(key)

    def _focused_window_from_metadata(self, window: Any, pid: int) -> tuple[int, int] | None:
        """Public-API fallback: require unique real title AND complete geometry.

        A missing CG title (e.g. OS privacy restrictions) cannot be replaced by
        the app name here. Ambiguous candidates never prove window identity.
        """
        if self._ax_value_get is None or self._ax_value_type is None:
            return None
        title_ref = position_ref = size_ref = None
        try:
            title_ref = self._copy_ax_attribute(window, "AXTitle")
            position_ref = self._copy_ax_attribute(window, "AXPosition")
            size_ref = self._copy_ax_attribute(window, "AXSize")
            title = self._string(title_ref)
            if not title or not position_ref or not size_ref:
                return None
            if self._ax_value_type(position_ref) != 1 or self._ax_value_type(size_ref) != 2:
                return None
            position, size = _CGPoint(), _CGSize()
            if not self._ax_value_get(position_ref, 1, ctypes.byref(position)):
                return None
            if not self._ax_value_get(size_ref, 2, ctypes.byref(size)):
                return None
            expected = (position.x, position.y, size.width, size.height)
            if not all(math.isfinite(value) for value in expected):
                return None
            records = self.window_records()
            eligible = {(item.window_id, item.pid) for item in _windows_from_records(records)}
            matches: set[tuple[int, int]] = set()
            for record in records:
                identity = record.get("window_id"), record.get("pid")
                if identity not in eligible or identity[1] != pid or record.get("title") != title:
                    continue
                try:
                    actual = tuple(float(record[name]) for name in ("x", "y", "width", "height"))
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if all(math.isclose(left, right, rel_tol=0.0, abs_tol=0.5) for left, right in zip(actual, expected)):
                    matches.add(identity)
            return next(iter(matches)) if len(matches) == 1 else None
        finally:
            for ref in (size_ref, position_ref, title_ref):
                if ref:
                    self._cf.CFRelease(ref)

    @staticmethod
    def _workspace_frontmost_pid() -> int | None:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app is not None else None

    def focused_window(self) -> tuple[int, int] | None:
        """Read the exact focused window, including with app-scoped AX access."""
        identity = self._system_focused_window()
        if identity is not None or not self._ax.AXIsProcessTrusted():
            return identity
        # Scoped grants may allow the app's AX object but not the system-wide one.
        # NSWorkspace supplies only a PID; never infer a window from it.
        pid = self._workspace_frontmost_pid()
        if pid is None or pid <= 0:
            return None
        app = self._ax.AXUIElementCreateApplication(pid)
        if not app:
            return None
        try:
            focused = self._focused_window_for_app(app, expected_pid=pid)
            return focused if focused is not None and self._workspace_frontmost_pid() == pid else None
        finally:
            self._cf.CFRelease(app)

    def _system_focused_window(self) -> tuple[int, int] | None:
        system = self._ax.AXUIElementCreateSystemWide()
        if not system:
            return None
        app = None
        try:
            if self._ax_timeout is not None:
                self._ax_timeout(system, 0.2)
            app = self._copy_ax_attribute(system, "AXFocusedApplication")
            return self._focused_window_for_app(app) if app else None
        finally:
            if app:
                self._cf.CFRelease(app)
            self._cf.CFRelease(system)

    def _focused_window_for_app(self, app: Any, *, expected_pid: int | None = None) -> tuple[int, int] | None:
        window = None
        try:
            if self._ax_timeout is not None:
                self._ax_timeout(app, 0.2)
            app_pid = ctypes.c_int()
            if self._ax.AXUIElementGetPid(app, ctypes.byref(app_pid)) != 0 or app_pid.value <= 0:
                return None
            if expected_pid is not None and app_pid.value != expected_pid:
                return None
            window = self._copy_ax_attribute(app, "AXFocusedWindow")
            if not window:
                return None
            window_id, window_pid = ctypes.c_uint32(), ctypes.c_int()
            if self._ax.AXUIElementGetPid(window, ctypes.byref(window_pid)) != 0:
                return None
            if window_pid.value != app_pid.value:
                return None
            if self._ax_window_id is not None:
                if self._ax_window_id(window, ctypes.byref(window_id)) == 0 and window_id.value:
                    return window_id.value, app_pid.value
            return self._focused_window_from_metadata(window, app_pid.value)
        finally:
            if window:
                self._cf.CFRelease(window)

    def accessibility_status(self, request: bool = False) -> tuple[bool, str]:
        extra = ""
        if request and self._trusted_prompt is not None:
            key = self._constant(self._ax, "kAXTrustedCheckOptionPrompt")
            true = self._constant(self._cf, "kCFBooleanTrue")
            keys, values = (ctypes.c_void_p * 1)(key), (ctypes.c_void_p * 1)(true)
            # Temporary unretained key/value pointers remain valid for this call.
            options = self._cf.CFDictionaryCreate(None, keys, values, 1, None, None)
            if not options:
                raise MacError("无法构造辅助功能授权请求")
            try:
                allowed = bool(self._trusted_prompt(options))
            finally:
                self._cf.CFRelease(options)
        else:
            allowed = bool(self._ax.AXIsProcessTrusted())
            if request:
                extra = "；系统不支持 AXIsProcessTrustedWithOptions 授权请求 API，请手动设置"
        return allowed, ("当前进程 AX 检查通过（不代表目标已收到按键）" if allowed else f"当前进程未通过 AX 信任检查。{_ax_hint()}") + extra

    def input_monitoring_status(self, request: bool = False) -> tuple[bool, str]:
        if self._listen_preflight is None:
            return False, f"系统不支持 CGPreflightListenEventAccess，无法验证输入监控；请检查{_INPUT_HINT}"
        allowed = bool(self._listen_preflight())
        extra = ""
        if request and not allowed:
            if self._listen_request is None:
                extra = "；系统不支持 CGRequestListenEventAccess 授权请求 API，请手动设置"
            else:
                allowed = bool(self._listen_request())
        return allowed, ("输入监控（Input Monitoring）已授权" if allowed else f"输入监控未授权：{_INPUT_HINT}") + extra

    def keyboard_post_status(self, request: bool = False) -> tuple[bool, str]:
        if self._post_preflight is None:
            return False, "系统缺少 CGPreflightPostEventAccess，无法确认 Quartz 发键权限"
        allowed = bool(self._post_preflight())
        if not allowed and request and self._post_request is not None:
            allowed = bool(self._post_request())
        if allowed:
            return True, "macOS 发键预检通过；不代表目标已收到按键"
        return False, "macOS 未放行当前进程的 Quartz 发键（不同于 AX 焦点和输入监控）；" + _ax_hint()

    def modifier_flags(self) -> int:
        flags = int(self._cg.CGEventSourceFlagsState(_HID_SYSTEM_STATE))
        for keycode, mask in _MODIFIER_KEYS.items():
            if self._cg.CGEventSourceKeyState(_HID_SYSTEM_STATE, keycode):
                flags |= mask
        return flags

    def create_key_event(self, keycode: int, down: bool) -> Any:
        # No CGEventSetFlags: never erase or synthesize releases for user modifiers.
        event = self._cg.CGEventCreateKeyboardEvent(None, keycode, down)
        if not event:
            raise MacError("CGEventCreateKeyboardEvent 失败；没有发送按键")
        return event

    def post_key_event(self, event: Any) -> None:
        # Global foreground event, NOT targeted/background delivery to a PID.
        self._cg.CGEventPost(_HID_EVENT_TAP, event)

    def post_key_event_to_app(self, pid: int, event: Any) -> None:
        if self._post_to_pid is None:
            raise MacError("此系统不提供定向发键 API CGEventPostToPid")
        self._post_to_pid(pid, event)

    def release_event(self, event: Any) -> None:
        if event:
            self._cf.CFRelease(event)


_backend: _MacFrameworks | None = None
_backend_lock = threading.Lock()
_keyboard_lock = threading.Lock()


def _get_backend() -> _MacFrameworks:
    _require_darwin()
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = _MacFrameworks()
        return _backend


def _windows_from_records(records: list[dict[str, Any]]) -> list[WindowInfo]:
    windows: list[WindowInfo] = []
    seen: set[tuple[int, int]] = set()
    for record in records:
        try:
            window_id, pid = record.get("window_id"), record.get("pid")
            if type(window_id) is not int or type(pid) is not int or window_id <= 0 or pid <= 0:
                continue
            width, height, alpha = (float(record[name]) for name in ("width", "height", "alpha"))
            if not all(math.isfinite(value) for value in (width, height, alpha)):
                continue
            if record.get("layer") != 0 or record.get("onscreen") is not True or alpha <= 0:
                continue
            if width < _MIN_WIDTH or height < _MIN_HEIGHT:
                continue
            app_name = str(record.get("app_name") or "").strip()
            title = str(record.get("title") or "").strip()
            if not app_name:
                app_name = f"PID {pid}"
            title = title or app_name
            identity = window_id, pid
            if identity not in seen:
                windows.append(WindowInfo(window_id, pid, app_name, title))
                seen.add(identity)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return windows


def list_windows() -> list[WindowInfo]:
    """List normal, visible windows >=64x48 points; only window metadata is read."""
    try:
        return _windows_from_records(_get_backend().window_records())
    except MacError:
        raise
    except Exception as exc:
        raise MacError(f"读取 macOS 窗口元数据失败：{exc}") from exc


def focused_window_info() -> WindowInfo | None:
    """Resolve one AX focus snapshot to a visible window, never by title or PID alone."""
    backend = _get_backend()
    identity = backend.focused_window()
    if (not isinstance(identity, tuple) or len(identity) != 2
            or not all(type(n) is int and n > 0 for n in identity)):
        return None
    return next((w for w in _windows_from_records(backend.window_records())
                 if (w.window_id, w.pid) == identity), None)


def _window_is_open(backend: _MacFrameworks, window: WindowInfo) -> bool:
    return any(
        item.window_id == window.window_id and item.pid == window.pid
        for item in _windows_from_records(backend.window_records())
    )


def is_window_open(window: WindowInfo) -> bool:
    """Unknown, hidden, invalid, or failed queries are closed for input purposes."""
    try:
        return _window_is_open(_get_backend(), window)
    except Exception:
        return False


def _window_is_focused(backend: _MacFrameworks, window: WindowInfo) -> bool:
    try:
        focused = backend.focused_window()
        if not isinstance(focused, tuple) or len(focused) != 2:
            return False
        window_id, pid = focused
        return type(window_id) is int and type(pid) is int and (window_id, pid) == (window.window_id, window.pid)
    except Exception:
        return False


def is_focused(target: WindowInfo | AppInfo) -> bool:
    """Check the selected application's identity or the exact AX window."""
    try:
        if isinstance(target, AppInfo):
            return _app_is_focused(target)
        return _window_is_focused(_get_backend(), target)
    except Exception:
        return False


def accessibility_status(request: bool = False) -> tuple[bool, str]:
    try:
        return _get_backend().accessibility_status(request)
    except Exception as exc:
        return False, f"无法检查辅助功能 API（AX）：{exc}；{_ax_hint()}"


def keyboard_post_status(request: bool = False) -> tuple[bool, str]:
    try:
        return _get_backend().keyboard_post_status(request)
    except Exception as exc:
        return False, f"无法检查 Quartz 发键权限：{exc}"


def input_monitoring_status(request: bool = False) -> tuple[bool, str]:
    try:
        return _get_backend().input_monitoring_status(request)
    except Exception as exc:
        return False, f"无法检查输入监控：{exc}；{_INPUT_HINT}"


class MacKeyboard:
    """Press one physical key only while the selected app or window is frontmost.

    Quartz cannot atomically lock user focus and post an event. We check again
    immediately before key-down and poll during the hold; any observed change
    fails closed. A finally key-up is still attempted after a focus change, to
    avoid leaving our key held. No background-delivery guarantee is made.
    """

    def __init__(self, target: WindowInfo | AppInfo, key: str = "`", hold_seconds: float = 0.06) -> None:
        if not isinstance(target, (WindowInfo, AppInfo)) or type(target.pid) is not int or target.pid <= 0:
            raise ValueError("目标应用无有效进程编号")
        if isinstance(target, WindowInfo) and (type(target.window_id) is not int or target.window_id <= 0):
            raise ValueError("目标窗口无有效编号")
        if not isinstance(key, str):
            raise ValueError("按键必须是单个物理键名称，例如 backtick/grave/`，不支持组合键")
        name = key.strip().lower()
        name = _KEY_ALIASES.get(name, name)
        if name not in _KEYCODES:
            raise ValueError(f"不支持的物理按键 {key!r}；可用 backtick/grave/`、字母数字、F1-F12 或常用导航键，不支持组合键")
        if isinstance(hold_seconds, bool):
            raise ValueError("hold_seconds 必须是有限且非负的秒数")
        try:
            hold = float(hold_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("hold_seconds 必须是有限且非负的秒数") from exc
        if not math.isfinite(hold) or hold < 0:
            raise ValueError("hold_seconds 必须是有限且非负的秒数")
        self.target = target
        self.key = name
        self.hold_seconds = hold
        self._keycode = _KEYCODES[name]

    def _check_ready(self, backend: _MacFrameworks, *, targeted: bool = False) -> None:
        allowed, detail = backend.accessibility_status(False)
        if not allowed:
            raise MacError(detail)
        if not targeted:
            allowed, detail = backend.keyboard_post_status()
            if not allowed:
                raise MacError(detail)
        if isinstance(self.target, WindowInfo) and not _window_is_open(backend, self.target):
            raise FocusError("目标窗口已关闭、不可见或无法确认；不会发送按键")
        self._check_focus_and_modifiers(backend)

    def _check_focus_and_modifiers(self, backend: _MacFrameworks) -> None:
        try:
            focused = (_app_is_focused(self.target) if isinstance(self.target, AppInfo)
                       else _window_is_focused(backend, self.target))
        except Exception as exc:
            raise FocusError("无法确认目标焦点") from exc
        if not focused:
            raise FocusError("目标不在前台或焦点无法确认")
        if backend.modifier_flags() & _MODIFIER_MASK:
            raise MacError("检测到用户按住 Ctrl/Shift/Alt/Option/Cmd，拒绝发键；请自行松开修饰键")

    def _hold_key(self, backend: _MacFrameworks) -> None:
        deadline = time.monotonic() + self.hold_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.01))
            self._check_focus_and_modifiers(backend)
        self._check_focus_and_modifiers(backend)

    def press(self, *, targeted: bool = False, cancelled=lambda: False,
              allow_app_system_event: bool = False,
              on_submitted: Callable[[], None] | None = None) -> None:
        if isinstance(self.target, AppInfo) and not targeted and not allow_app_system_event:
            raise MacError("应用锁定只允许定向发键")
        with _keyboard_lock:
            backend = _get_backend()
            # Global preflight does not check authorizations for a target app.
            # The explicit targeted route goes through the public per-PID API
            # and OS authorization. Never retry via the global route.
            post = (lambda event: backend.post_key_event_to_app(self.target.pid, event)) if targeted else backend.post_key_event
            down = up = None
            try:
                self._check_ready(backend, targeted=targeted)
                down = backend.create_key_event(self._keycode, True)
                up = backend.create_key_event(self._keycode, False)
                # Creation/permission checks are not allowed to leave a stale focus decision.
                self._check_ready(backend, targeted=targeted)
                if cancelled():
                    raise FocusError("已停止；未发送按键")
                try:
                    post(down)
                    if on_submitted is not None:
                        on_submitted()
                    self._hold_key(backend)
                finally:
                    # Even a failed key-down call may already have reached the OS.
                    post(up)
            except MacError:
                raise
            except Exception as exc:
                raise MacError(f"Quartz 发键失败，已在可能按下后尝试松键：{exc}") from exc
            finally:
                for event in (up, down):
                    if event is not None:
                        backend.release_event(event)
