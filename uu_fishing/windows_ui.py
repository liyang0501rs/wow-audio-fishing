"""Win32 rendering and event plumbing for the shared floating button."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import signal
import threading
import time

from .button_ui import _BUTTON_SIZE, _FONT_SIZE, _ButtonController, _ButtonGesture, _RunSession, _button_position
from .windows_input import _bind


class _Paint(c.Structure):
    _fields_ = [("dc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                ("restore", w.BOOL), ("update", w.BOOL), ("reserved", w.BYTE * 32)]


def _rgb(r, g, b):
    return r | g << 8 | b << 16


def run_button_ui(run_session: _RunSession) -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Windows 浮窗必须在主线程运行")
    controller = _ButtonController(run_session)
    try:
        _run_window(controller)
    finally:
        controller.close("按钮界面退出")


def _run_window(controller: _ButtonController) -> None:
    user = c.WinDLL("user32", use_last_error=True)
    gdi = c.WinDLL("gdi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    ptr = c.c_ssize_t
    wnd_proc = c.WINFUNCTYPE(ptr, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WindowClass(c.Structure):
        _fields_ = [("style", w.UINT), ("proc", wnd_proc), ("class_extra", c.c_int), ("window_extra", c.c_int),
                    ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE),
                    ("background", w.HBRUSH), ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

    instance = _bind(kernel, "GetModuleHandleW", w.HINSTANCE, w.LPCWSTR)(None)
    register = _bind(user, "RegisterClassW", w.ATOM, c.POINTER(WindowClass))
    unregister = _bind(user, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
    create = _bind(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                   c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    default = _bind(user, "DefWindowProcW", ptr, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    destroy = _bind(user, "DestroyWindow", w.BOOL, w.HWND)
    place = _bind(user, "SetWindowPos", w.BOOL, w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT)
    screen_area = _bind(user, "SystemParametersInfoW", w.BOOL, w.UINT, w.UINT, c.POINTER(w.RECT), w.UINT)
    cursor_pos = _bind(user, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
    key_state = _bind(user, "GetAsyncKeyState", c.c_short, c.c_int)
    get_rect = _bind(user, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
    capture = _bind(user, "SetCapture", w.HWND, w.HWND)
    release_capture = _bind(user, "ReleaseCapture", w.BOOL)
    repaint = _bind(user, "InvalidateRect", w.BOOL, w.HWND, c.POINTER(w.RECT), w.BOOL)
    begin_paint = _bind(user, "BeginPaint", w.HDC, w.HWND, c.POINTER(_Paint))
    end_paint = _bind(user, "EndPaint", w.BOOL, w.HWND, c.POINTER(_Paint))
    fill = _bind(user, "FillRect", c.c_int, w.HDC, c.POINTER(w.RECT), w.HBRUSH)
    draw_text = _bind(user, "DrawTextW", c.c_int, w.HDC, w.LPCWSTR, c.c_int, c.POINTER(w.RECT), w.UINT)
    brush = _bind(gdi, "CreateSolidBrush", w.HBRUSH, w.DWORD)
    delete = _bind(gdi, "DeleteObject", w.BOOL, w.HANDLE)
    select = _bind(gdi, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
    text_color = _bind(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
    background = _bind(gdi, "SetBkMode", c.c_int, w.HDC, c.c_int)
    font = _bind(gdi, "CreateFontW", w.HANDLE, *([c.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    peek = _bind(user, "PeekMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
    dispatch = _bind(user, "DispatchMessageW", ptr, c.POINTER(w.MSG))

    size = _BUTTON_SIZE
    work = w.RECT()
    if not screen_area(0x0030, 0, c.byref(work), 0):  # SPI_GETWORKAREA
        raise c.WinError(c.get_last_error())
    x, y = _button_position(work.left, work.top, work.right - work.left, work.bottom - work.top)
    display = controller.poll()
    palette = {"red": _rgb(223, 57, 62), "orange": _rgb(248, 157, 34), "green": _rgb(54, 197, 103)}
    gesture = _ButtonGesture()
    right_double = False
    done = interrupted = False
    failure = None

    def render(hwnd, state):
        nonlocal display
        if state != display:
            display = state
            repaint(hwnd, None, False)

    def paint(hwnd):
        ps = _Paint()
        dc = begin_paint(hwnd, c.byref(ps))
        color, number = display
        rect = w.RECT(0, 0, size, size)
        bg = brush(palette[color])
        try:
            fill(dc, c.byref(rect), bg)
            if number:
                height = min(_FONT_SIZE, 86 // len(number))
                handle = font(-height, 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 0, 0, "Segoe UI")
                previous = select(dc, handle) if handle else None
                try:
                    background(dc, 1)  # TRANSPARENT
                    text_color(dc, 0xffffff if color == "red" else 0)
                    draw_text(dc, number, -1, c.byref(rect), 0x25)  # center | vcenter | singleline
                finally:
                    if previous:
                        select(dc, previous)
                    if handle:
                        delete(handle)
        finally:
            delete(bg)
            end_paint(hwnd, c.byref(ps))

    @wnd_proc
    def handle(hwnd, message, wp, lp):
        nonlocal done, failure, right_double
        try:
            if message == 0x0021:  # WM_MOUSEACTIVATE: receive the click without taking focus.
                return 3
            if message == 0x000F:
                paint(hwnd)
                return 0
            if message == 0x0010:  # WM_CLOSE
                done = True
                return 0
            if message == 0x0215:  # WM_CAPTURECHANGED
                gesture.release(False)
                right_double = False
                return 0
            if message == 0x0204:  # WM_RBUTTONDOWN
                right_double = False
                return 0
            if message == 0x0206:  # WM_RBUTTONDBLCLK (second down)
                right_double = True
                return 0
            if message == 0x0205:  # WM_RBUTTONUP (finish inside the button)
                x = c.c_short(lp & 0xffff).value
                y = c.c_short((lp >> 16) & 0xffff).value
                if right_double and 0 <= x < size and 0 <= y < size:
                    controller.request_exit()
                    done = True
                right_double = False
                return 0
            if message in (0x0200, 0x0201, 0x0202, 0x0203):
                point, rect = w.POINT(), w.RECT()
                if cursor_pos(c.byref(point)) and get_rect(hwnd, c.byref(rect)):
                    if message in (0x0201, 0x0203):  # left down or second down with CS_DBLCLKS
                        gesture.press(point.x, point.y, rect.left, rect.top)
                        capture(hwnd)
                    elif gesture.origin is not None:
                        position = gesture.move(point.x, point.y)
                        if position is not None:
                            place(hwnd, None, *position, 0, 0, 0x0015)  # NOSIZE | NOZORDER | NOACTIVATE
                        if message == 0x0202:
                            click = gesture.release(rect.left <= point.x < rect.right and rect.top <= point.y < rect.bottom)
                            release_capture()
                            if click:
                                render(hwnd, controller.click())
                elif message == 0x0202:
                    gesture.release(False)
                    release_capture()
                return 0
        except Exception as exc:
            failure = exc
            done = True
            return 0
        return default(hwnd, message, wp, lp)

    def interrupt(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    class_name = "UUFishingFloatingButton"
    arrow = _bind(user, "LoadCursorW", w.HANDLE, w.HINSTANCE, w.LPVOID)(None, 32512)
    # CS_DBLCLKS lets Win32 recognize the user's configured double-click timing.
    if not register(c.byref(WindowClass(0x0008, handle, 0, 0, instance, None, arrow, None, None, class_name))):
        raise c.WinError(c.get_last_error())
    hwnd = None
    previous_handler = signal.signal(signal.SIGINT, interrupt)
    try:
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW; show separately with SWP_NOACTIVATE.
        hwnd = create(0x08000080, class_name, "UU 钓鱼启停", 0x80000000,
                      x, y, size, size, None, None, instance, None)
        if not hwnd or not place(hwnd, w.HWND(-1), x, y, size, size, 0x0050):
            raise c.WinError(c.get_last_error())
        message = w.MSG()
        while not done and not interrupted:
            while not done and peek(c.byref(message), None, 0, 0, 1):
                if message.message == 0x0012:  # WM_QUIT
                    done = True
                    break
                dispatch(c.byref(message))
            if not done and not interrupted:
                # A nonactivating window might not receive mouse messages outside its bounds.
                if gesture.origin is not None:
                    handle(hwnd, 0x0200 if key_state(0x01) & 0x8000 else 0x0202, 0, 0)
                render(hwnd, controller.poll())
                time.sleep(0.02)
    finally:
        if hwnd:
            destroy(hwnd)
        unregister(class_name, instance)
        signal.signal(signal.SIGINT, previous_handler)
    if failure is not None:
        raise failure
    if interrupted:
        raise KeyboardInterrupt
