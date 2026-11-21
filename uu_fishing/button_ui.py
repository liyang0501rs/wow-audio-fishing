"""Shared floating-button behavior and its native AppKit renderer."""
from __future__ import annotations

from collections.abc import Callable
from functools import cache
import signal
import sys
import threading
import time
from typing import Protocol

from .engine import Controls

class ButtonUpdate(Protocol):
    def __call__(self, color: str | None = None, *, score: float | None = None,
                 reeled: bool = False) -> None: ...


_RunSession = Callable[[Controls, ButtonUpdate], None]
_ACTIVE_COLORS = ("orange", "green")
_SCORE_INTERVAL = 0.2
_BUTTON_SIZE = 60
_FONT_SIZE = 48


def _button_position(left, top, width, height):
    size = _BUTTON_SIZE
    return (left + max(0, min(width - size, (width - size) // 2 + 4 * size)),
            top + max(0, (height - size) // 2))


class _ButtonGesture:
    """One click/drag distinction for both native renderers."""

    def __init__(self):
        self.origin: tuple[float, float, float, float] | None = None
        self.dragged = False

    def press(self, x, y, left, top) -> None:
        self.origin = (x, y, left, top)
        self.dragged = False

    def move(self, x, y) -> tuple[float, float] | None:
        if self.origin is None:
            return None
        x0, y0, left, top = self.origin
        dx, dy = x - x0, y - y0
        if self.dragged or dx * dx + dy * dy >= 16:
            self.dragged = True
            return left + dx, top + dy
        return None

    def release(self, inside: bool) -> bool:
        clicked = self.origin is not None and not self.dragged and inside
        self.origin = None
        return clicked


class _Session:
    def __init__(self, run_session: _RunSession) -> None:
        self.controls = Controls(paused=False)
        self._lock = threading.Lock()
        self._color = "orange"
        self._peak: float | None = None
        self._reel_score: float | None = None
        self._reels = 0
        self.worker = threading.Thread(target=self._run, args=(run_session,),
                                       name="uu-fishing-session", daemon=False)

    def _run(self, run_session: _RunSession) -> None:
        try:
            if not self.controls.stop_event.is_set():
                run_session(self.controls, self.show)
        except BaseException as exc:
            print(f"钓鱼运行失败：{exc}", file=sys.stderr)

    def show(self, color: str | None = None, *, score: float | None = None,
             reeled: bool = False) -> None:
        # Bounded, per-session mailbox: no AppKit calls or queuing on the worker.
        with self._lock:
            if color in _ACTIVE_COLORS and color != self._color:
                self._color = color
                self._peak = None
                self._reel_score = None
            if self._color == "green" and score is not None and score > -1:
                if self._peak is None or score > self._peak:
                    self._peak = score
            if reeled:
                self._reels += 1
                self._reel_score = score

    def snapshot(self, *, take_peak: bool = False) -> tuple[str, int, float | None, float | None]:
        with self._lock:
            peak = self._peak if take_peak else None
            if take_peak:
                self._peak = None
            return self._color, self._reels, peak, self._reel_score


class _ButtonController:
    """Click, poll and close run only on the UI thread; workers publish to their own mailbox."""

    def __init__(self, run_session: _RunSession) -> None:
        self._run_session = run_session
        self._session: _Session | None = None
        self._color = "red"
        self._reels = 0  # Completed sessions, until the GUI process exits.
        self._score_text = ""
        self._next_score_at = 0.0

    def _retire_session(self) -> None:
        if self._session is not None:
            _, reels, _, _ = self._session.snapshot()
            self._reels += reels
            self._session = None

    def click(self) -> tuple[str, str]:
        if self._color in _ACTIVE_COLORS:
            # Honor the displayed stop action even if the worker just finished.
            assert self._session is not None
            self._session.controls.stop("按钮停止")
            self._color = "red"
            return self.poll()
        if self._session is not None and self._session.worker.is_alive():
            return self.poll()  # Previous resources are still in use.
        self._retire_session()
        self._session = _Session(self._run_session)
        try:
            self._session.worker.start()
        except RuntimeError as exc:
            self._session = None
            print(f"无法启动钓鱼线程：{exc}", file=sys.stderr)
        else:
            self._color = "orange"
            return "orange", ""
        return self.poll()

    def poll(self) -> tuple[str, str]:
        session = self._session
        active_reels = 0
        reel_score = None
        if session is not None:
            if not session.worker.is_alive():
                self._retire_session()
                self._color = "red"
            else:
                now = time.monotonic()
                refresh = self._color == "green" and now >= self._next_score_at
                color, active_reels, peak, reel_score = session.snapshot(take_peak=refresh)
                if self._color != "red":
                    if color == "green":
                        if self._color != "green":
                            self._score_text = ""
                            self._next_score_at = now + _SCORE_INTERVAL
                        elif refresh:
                            self._score_text = "" if peak is None else str(round(peak * 100))
                            self._next_score_at = now + _SCORE_INTERVAL
                    self._color = color
        if self._color == "red":
            total_reels = self._reels + active_reels
            return "red", str(total_reels) if total_reels else ""
        if self._color == "green":
            return "green", self._score_text
        return "orange", "" if reel_score is None else str(round(reel_score * 100))

    def close(self, reason: str) -> None:
        self._color = "red"
        if self._session is not None:
            if not self._session.controls.stop_event.is_set():
                self._session.controls.stop(reason)
            self._session.worker.join()
            self._retire_session()


@cache
def _button_class(ak, objc):
    # ObjC class names are process-global. Reuse the class for subsequent panels.
    class UUFishingColorButton(ak.NSButton):
        def acceptsFirstMouse_(self, event):
            return True

        def needsPanelToBecomeKey(self):
            return False

        def mouseDown_(self, event):
            panel = self.window()
            point = panel.convertPointToScreen_(event.locationInWindow())
            origin = panel.frame().origin
            self._gesture.press(point.x, point.y, origin.x, origin.y)

        @objc.python_method
        def _move_for_event(self, event):
            panel = self.window()
            point = panel.convertPointToScreen_(event.locationInWindow())
            position = self._gesture.move(point.x, point.y)
            if position is not None:
                panel.setFrameOrigin_(position)

        def mouseDragged_(self, event):
            self._move_for_event(event)

        def mouseUp_(self, event):
            self._move_for_event(event)
            release = self.convertPoint_fromView_(event.locationInWindow(), None)
            if self._gesture.release(ak.NSPointInRect(release, self.bounds())):
                self.uuClick_(self)

        @objc.python_method
        def render(self, display: tuple[str, str]) -> None:
            if self._display != display:
                self._display = display
                self.setNeedsDisplay_(True)

        @objc.IBAction
        def uuClick_(self, sender):
            self.render(self._controller.click())

        def drawRect_(self, dirty_rect):
            palette = {
                "red": ak.NSColor.systemRedColor,
                "orange": ak.NSColor.systemOrangeColor,
                "green": ak.NSColor.systemGreenColor,
            }
            color, number = self._display
            palette[color]().setFill()
            path = ak.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                ak.NSInsetRect(self.bounds(), 1.0, 1.0), 8.0, 8.0,
            )
            path.fill()
            ak.NSColor.colorWithWhite_alpha_(0.0, 0.35).setStroke()
            path.stroke()
            if number:
                bounds = self.bounds()
                font_size = _FONT_SIZE
                font = ak.NSFont.monospacedDigitSystemFontOfSize_weight_
                attributes = {
                    ak.NSFontAttributeName: font(font_size, ak.NSFontWeightBold),
                    ak.NSForegroundColorAttributeName: (ak.NSColor.whiteColor() if color == "red"
                                                        else ak.NSColor.blackColor()),
                }
                text = ak.NSAttributedString.alloc().initWithString_attributes_(number, attributes)
                size = text.size()
                scale = min(1.0, (bounds.size.width - 6) / size.width,
                            (bounds.size.height - 4) / size.height)
                if scale < 1.0:
                    attributes[ak.NSFontAttributeName] = font(font_size * scale, ak.NSFontWeightBold)
                    text = ak.NSAttributedString.alloc().initWithString_attributes_(number, attributes)
                    size = text.size()
                text.drawAtPoint_(((bounds.size.width - size.width) / 2,
                                  (bounds.size.height - size.height) / 2))

    return UUFishingColorButton


def _make_panel(ak, objc, controller: _ButtonController):
    screen = ak.NSScreen.mainScreen()
    if screen is None:
        raise RuntimeError("没有可用的 macOS 桌面显示器")
    rect = screen.visibleFrame()
    size = _BUTTON_SIZE
    frame = (_button_position(rect.origin.x, rect.origin.y, rect.size.width, rect.size.height), (size, size))
    panel = ak.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, ak.NSWindowStyleMaskBorderless | ak.NSWindowStyleMaskNonactivatingPanel,
        ak.NSBackingStoreBuffered, False,
    )
    panel.setCollectionBehavior_(ak.NSWindowCollectionBehaviorCanJoinAllSpaces |
                                 ak.NSWindowCollectionBehaviorFullScreenAuxiliary |
                                 ak.NSWindowCollectionBehaviorIgnoresCycle)
    # Stay above standard floating panels without covering macOS security/lock screens.
    panel.setLevel_(ak.NSPopUpMenuWindowLevel)
    panel.setHidesOnDeactivate_(False)
    panel.setBecomesKeyOnlyIfNeeded_(True)
    panel.setIgnoresMouseEvents_(False)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(ak.NSColor.clearColor())

    button = _button_class(ak, objc).alloc().initWithFrame_(((0, 0), (size, size)))
    button._controller = controller
    button._gesture = _ButtonGesture()
    button._display = controller.poll()
    button.setTitle_("")
    button.setBordered_(False)
    button.setRefusesFirstResponder_(True)
    button.setAccessibilityLabel_("UU 钓鱼启停")
    button.setTarget_(button)
    button.setAction_("uuClick:")
    panel.setContentView_(button)
    return panel, button


def run_button_ui(run_session: _RunSession) -> None:
    """Run macOS AppKit on the main thread; call run_session(controls, show) on clicks.

    The callback must return after controls.stop_event is set, including resource cleanup.
    The worker publishes color, score and completed reel actions; red is managed by the UI.
    Terminal Ctrl+C stops the worker before propagating KeyboardInterrupt to the caller.
    """
    if sys.platform != "darwin":
        raise RuntimeError("原生启停按钮仅支持 macOS")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("原生启停按钮必须在主线程运行")
    import AppKit as ak
    import objc

    controller = _ButtonController(run_session)
    interrupted = False

    def on_interrupt(signum, frame):
        # Do not raise through AppKit's event callbacks or interrupt a thread start.
        nonlocal interrupted
        interrupted = True

    previous_sigint = signal.signal(signal.SIGINT, on_interrupt)
    panel = None
    try:
        with objc.autorelease_pool():
            app = ak.NSApplication.sharedApplication()
            if not app.setActivationPolicy_(ak.NSApplicationActivationPolicyAccessory):
                raise RuntimeError("无法设置不占用 Dock 的按钮应用策略")
            app.finishLaunching()
            panel, button = _make_panel(ak, objc, controller)
            panel.orderFrontRegardless()  # No makeKey*, activate*, or focus transfer.
        while not interrupted:
            with objc.autorelease_pool():
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    ak.NSEventMaskAny, ak.NSDate.dateWithTimeIntervalSinceNow_(0.05),
                    ak.NSDefaultRunLoopMode, True,
                )
                if interrupted:
                    break
                if event is not None:
                    app.sendEvent_(event)
                button.render(controller.poll())
                app.updateWindows()
    finally:
        try:
            if panel is not None:
                panel.orderOut_(None)
        finally:
            try:
                controller.close("Ctrl+C 停止" if interrupted else "按钮界面退出")
            finally:
                signal.signal(signal.SIGINT, previous_sigint)
    if interrupted:
        raise KeyboardInterrupt
