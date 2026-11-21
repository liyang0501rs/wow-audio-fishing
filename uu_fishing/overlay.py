"""Read-only macOS status panel. Importing/constructing the parent uses no AppKit."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import warnings

_START_TIMEOUT = 5.0
_CLOSE_TIMEOUT = 0.5
_INITIAL = "UU 钓鱼 · 准备中"


class StatusOverlay:
    """Single-use context. Entry confirms visibility; show posts latest text, not a receipt."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._changed = threading.Condition()
        self._started = threading.Event()
        self._closed = False
        self._text: str | None = None
        self._error: str | None = None

    def __enter__(self) -> StatusOverlay:
        if sys.platform != "darwin":
            raise RuntimeError("原生状态条仅支持 macOS")
        if self._closed or self._proc is not None:
            raise RuntimeError("状态条上下文不可重复使用")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--panel"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"无法启动状态条辅助进程：{exc}") from exc
        try:
            self._thread = threading.Thread(target=self._exchange, name="uu-status-pipe", daemon=True)
            self._thread.start()
            if not self._started.wait(_START_TIMEOUT):
                raise RuntimeError("状态条启动超时：未确认窗口可见，请在已登录的 Mac 桌面运行")
            self._check()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _exchange(self) -> None:
        # Only this thread touches either pipe; neither show() nor close() can
        # block on a slow pipe or on another thread's buffered file lock.
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        try:
            with proc.stdin, proc.stdout:
                reply = proc.stdout.readline(2048).decode("utf-8", "replace").strip()
                if reply != "READY":
                    raise RuntimeError(reply or "辅助进程未确认窗口可见便退出")
                self._started.set()
                sent = None
                while True:
                    with self._changed:
                        self._changed.wait_for(lambda: self._closed or self._text != sent)
                        if self._closed:
                            return  # stdin EOF, including when the parent process dies
                        sent = self._text
                    proc.stdin.write((sent + "\n").encode("utf-8"))
                    proc.stdin.flush()
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._started.set()

    def _check(self) -> None:
        if self._error is not None:
            raise RuntimeError(f"状态条不可用：{self._error}")
        if self._closed or self._proc is None or not self._started.is_set():
            raise RuntimeError("状态条不在活动上下文中")
        code = self._proc.poll()
        if code is not None:
            raise RuntimeError(f"状态条辅助进程已退出（{code}）")

    def show(self, text: str) -> None:
        """Coalesce into one bounded slot; identical states cost no IPC. Check health even on repeats."""
        self._check()
        text = " ".join(text[:160].split())
        with self._changed:
            if self._text != text:
                self._text = text
                self._changed.notify()

    def close(self) -> None:
        """Signal EOF; escalate to terminate/kill on bounded waits if AppKit stalls."""
        with self._changed:
            if self._closed:
                return
            self._closed = True
            self._changed.notify()
        proc = self._proc
        if proc is None:
            return
        for stop in (None, proc.terminate, proc.kill):
            if stop is not None:
                stop()
            try:
                code = proc.wait(timeout=_CLOSE_TIMEOUT)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            raise RuntimeError("状态条辅助进程未在限时内退出")
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=_CLOSE_TIMEOUT)
        else:  # thread creation itself failed
            proc.stdin.close()
            proc.stdout.close()
        if self._error or code != 0 and stop is None:
            raise RuntimeError(f"状态条异常退出：{self._error or code}")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(str(cleanup))


def _make_panel(ak):
    screen = ak.NSScreen.mainScreen()
    if screen is None:
        raise RuntimeError("没有可用的 macOS 桌面显示器")
    rect = screen.visibleFrame()
    width, height, margin = min(380, rect.size.width - 24), 30, 12
    frame = ((rect.origin.x + rect.size.width - width - margin,
              rect.origin.y + rect.size.height - height - margin), (width, height))
    panel = ak.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, ak.NSWindowStyleMaskBorderless | ak.NSWindowStyleMaskNonactivatingPanel,
        ak.NSBackingStoreBuffered, False,
    )
    panel.setTitle_("UU Fishing HUD")
    panel.setCollectionBehavior_(ak.NSWindowCollectionBehaviorCanJoinAllSpaces |
                                 ak.NSWindowCollectionBehaviorFullScreenAuxiliary |
                                 ak.NSWindowCollectionBehaviorIgnoresCycle)
    panel.setLevel_(ak.NSStatusWindowLevel)
    panel.setHidesOnDeactivate_(False)
    panel.setIgnoresMouseEvents_(True)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(ak.NSColor.clearColor())
    view = panel.contentView()
    view.setWantsLayer_(True)
    view.layer().setCornerRadius_(8)
    with warnings.catch_warnings():
        # NSColor.CGColor() returns an untyped CGColorRef; PyObjC warns even though it renders fine.
        warnings.simplefilter("ignore")
        view.layer().setBackgroundColor_(ak.NSColor.colorWithWhite_alpha_(0.08, 0.88).CGColor())
    label = ak.NSTextField.labelWithString_(_INITIAL)
    label.setFont_(ak.NSFont.systemFontOfSize_(14))
    label.setTextColor_(ak.NSColor.whiteColor())
    label.setLineBreakMode_(ak.NSLineBreakByTruncatingTail)
    label.setFrame_(((10, 5), (width - 20, 20)))
    view.addSubview_(label)
    panel.orderFrontRegardless()  # never make key/main or activate an application
    panel.displayIfNeeded()
    return panel, label


def _run_panel() -> None:
    # Executed only via this file's private child entry point, on the main thread.
    import AppKit as ak
    import objc

    app = ak.NSApplication.sharedApplication()
    if not app.setActivationPolicy_(ak.NSApplicationActivationPolicyAccessory):
        raise RuntimeError("无法设置不占用 Dock 的状态条应用策略")
    app.finishLaunching()
    panel, label = _make_panel(ak)
    fd = sys.stdin.fileno()
    os.set_blocking(fd, False)
    buffer = b""
    ready = False
    deadline = time.monotonic() + 3
    while True:
        with objc.autorelease_pool():
            event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                ak.NSEventMaskAny, ak.NSDate.dateWithTimeIntervalSinceNow_(0.08), ak.NSDefaultRunLoopMode, True,
            )
            if event is not None:
                app.sendEvent_(event)
            app.updateWindows()
            if not ready:
                if panel.isVisible() and panel.occlusionState() & ak.NSWindowOcclusionStateVisible:
                    print("READY", flush=True)
                    ready = True
                elif time.monotonic() >= deadline:
                    raise RuntimeError("WindowServer 未确认状态条可见，请检查显示器与桌面会话")
            try:
                data = os.read(fd, 8192)
            except BlockingIOError:
                continue
            if not data:  # parent closed or died, no explicit shutdown message required
                panel.orderOut_(None)
                return
            *lines, buffer = (buffer + data).split(b"\n")
            if lines:
                label.setStringValue_(lines[-1].decode("utf-8"))


if __name__ == "__main__" and sys.argv[1:] == ["--panel"]:
    try:
        _run_panel()
    except Exception as err:
        # stdout is the startup handshake; stderr also reports failures after READY.
        message = f"状态条启动或显示失败：{err}"
        sys.stdout.buffer.write((message + "\n").encode("utf-8"))
        print(message, file=sys.stderr)
        sys.exit(1)
