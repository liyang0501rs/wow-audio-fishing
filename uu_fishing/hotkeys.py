"""Optional real-run F8/F9 listener; importing this module never imports pynput.

F8 toggles once per press (OS key-repeat is ignored), F9 stops. Keys are not
suppressed or synthesized. A dead listener or a failing callback invokes the
stop callback instead of silently losing the global emergency-stop key.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from typing import Any, Callable

from .macos import MacError, accessibility_status, input_monitoring_status


_LOG = logging.getLogger(__name__)
_START_TIMEOUT = 2.0
_START_POLL = 0.01
_WATCH_INTERVAL = 0.1
_JOIN_TIMEOUT = 0.5
_PERMISSION_HINT = "本机终端的前台窗口控制与输入监控权限（运行 doctor --no-audio 查看对应系统版本的指引）"


def _load_keyboard_module() -> Any:
    try:
        return importlib.import_module("pynput.keyboard")
    except Exception as exc:
        raise MacError(f"无法加载 pynput 全局热键；请安装 macOS requirements 并确认{_PERMISSION_HINT}：{exc}") from exc


def _is_alive(listener: Any) -> bool:
    try:
        return bool(listener.is_alive()) and bool(listener.running)
    except Exception:
        return False


def _ready(listener: Any) -> bool:
    # pynput.wait() has no timeout. Inspect its bounded readiness state instead
    # of starting a helper that could wait forever and leak a thread.
    value = getattr(listener, "_ready", None)
    if value is None:
        raise RuntimeError("此 pynput 版本未提供可限时验证的 Listener 就绪状态，请安装受支持的 pynput")
    return bool(value.is_set()) if hasattr(value, "is_set") else bool(value)


class GlobalHotkeys:
    def __init__(self, on_toggle: Callable[[], None], on_stop: Callable[[], None]) -> None:
        if not callable(on_toggle) or not callable(on_stop):
            raise TypeError("on_toggle 和 on_stop 必须可调用")
        self._on_toggle = on_toggle
        self._on_stop = on_stop
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._listener: Any = None
        self._watcher: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._shutdown.set()
        self._accept_events = False
        self._started = False
        self._pressed: set[str] = set()
        self._toggle_key: Any = None
        self._stop_key: Any = None
        self._stop_notified = False
        self._error: BaseException | None = None

    def start(self) -> None:
        """Verify permissions and ready event tap within a fixed deadline.

        Call this only for a real, explicitly enabled run, not offline/dry-run.
        This method never requests or changes privacy permissions itself.
        """
        if sys.platform != "darwin":
            raise MacError("全局钓鱼热键真实 IO 仅支持 macOS；离线/dry-run 无需启用")
        if not self._start_lock.acquire(blocking=False):
            raise RuntimeError("全局热键正在启动，请勿重复并发启动")
        listener = None
        try:
            with self._lock:
                if self._started and self._listener is not None:
                    if _is_alive(self._listener):
                        return
                    raise RuntimeError("全局热键监听器已意外退出，不能继续运行")
                self._shutdown = shutdown = threading.Event()
                self._error = None
                self._pressed.clear()
                self._stop_notified = False
            for checker in (accessibility_status, input_monitoring_status):
                allowed, detail = checker(request=False)
                if not allowed:
                    raise MacError(f"无法安全启动 F8/F9 全局热键：{detail}；{_PERMISSION_HINT}")
            keyboard = _load_keyboard_module()
            listener = keyboard.Listener(on_press=self._handle_press, on_release=self._handle_release, suppress=False)
            if not getattr(listener, "IS_TRUSTED", True):
                raise MacError(f"pynput 报告全局监听未受信任；{_PERMISSION_HINT}")
            with self._lock:
                if shutdown.is_set():
                    return
                self._toggle_key, self._stop_key = keyboard.Key.f8, keyboard.Key.f9
                self._listener = listener
                self._accept_events = True
            listener.start()
            deadline = time.monotonic() + _START_TIMEOUT
            while True:
                with self._lock:
                    if self._listener is not listener or shutdown.is_set():
                        if self._error is not None:
                            raise RuntimeError(f"全局热键启动时安全停止：{self._error}") from self._error
                        return
                if not bool(listener.is_alive()):
                    raise RuntimeError(f"全局热键监听线程在就绪前退出；{_PERMISSION_HINT}")
                if _ready(listener) and bool(listener.running):
                    # Darwin pynput can mark ready even when CGEventTapCreate failed.
                    if hasattr(listener, "_tap") and not listener._tap:
                        raise MacError(f"pynput 无法创建全局事件监听 tap；{_PERMISSION_HINT}")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"全局热键启动超时（{_START_TIMEOUT:g}s），未确认 F9 可用；{_PERMISSION_HINT}")
                time.sleep(_START_POLL)
            with self._lock:
                if shutdown.is_set() or self._listener is not listener:
                    return
                self._started = True
                watcher = threading.Thread(target=self._watch_listener, args=(listener, shutdown), name="uu-fishing-hotkeys-watch", daemon=True)
                self._watcher = watcher
                watcher.start()
        except Exception as exc:
            with self._lock:
                self._error = exc
            try:
                self.stop()
            except Exception:
                _LOG.exception("停止启动失败的全局热键监听器时出错")
            # A Listener constructed before its trust check failed was not stored.
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    _LOG.exception("清理未就绪的全局热键监听器时出错")
            if isinstance(exc, (MacError, RuntimeError)):
                raise
            raise RuntimeError(f"无法启动 F8/F9 全局热键；{_PERMISSION_HINT}：{exc}") from exc
        finally:
            self._start_lock.release()

    def _handle_press(self, key: Any) -> bool | None:
        with self._lock:
            if not self._accept_events:
                return False
            if key == self._toggle_key:
                action = "toggle"
            elif key == self._stop_key:
                action = "stop"
            else:
                return None
            if action in self._pressed:
                return None
            self._pressed.add(action)
        if action == "stop":
            self._safe_stop()
            return False
        try:
            self._on_toggle()
        except BaseException as exc:
            _LOG.exception("F8 回调失败，安全停止")
            self._safe_stop(exc)
            return False
        return None

    def _handle_release(self, key: Any) -> bool | None:
        with self._lock:
            if not self._accept_events:
                return False
            if key == self._toggle_key:
                self._pressed.discard("toggle")
            elif key == self._stop_key:
                self._pressed.discard("stop")
        return None

    def _notify_stop(self) -> None:
        with self._lock:
            if self._stop_notified:
                return
            self._stop_notified = True
        try:
            self._on_stop()
        except BaseException as exc:
            with self._lock:
                self._error = self._error or exc
            _LOG.exception("全局热键停止回调失败，监听器已停用")

    def _safe_stop(self, error: BaseException | None = None, *, expected_listener: Any = None) -> None:
        with self._lock:
            if expected_listener is not None and self._listener is not expected_listener:
                return
            if error is not None:
                self._error = error
            # Disable callbacks before invoking external application code.
            self._accept_events = False
        # Notify first: even a broken listener.stop() must not delay engine stop.
        self._notify_stop()
        try:
            self.stop()
        except Exception:
            _LOG.exception("安全停止全局热键监听器时出错")

    def _watch_listener(self, listener: Any, shutdown: threading.Event) -> None:
        while not shutdown.wait(_WATCH_INTERVAL):
            if not _is_alive(listener):
                error = RuntimeError("全局热键监听线程意外退出，F9 不再可靠；已要求安全停止")
                _LOG.error("%s", error)
                self._safe_stop(error, expected_listener=listener)
                return

    def stop(self) -> None:
        """Idempotently disable callbacks and stop/join with bounded waits."""
        with self._lock:
            self._shutdown.set()
            self._accept_events = False
            self._started = False
            self._pressed.clear()
            listener, watcher = self._listener, self._watcher
            self._listener = self._watcher = None
        failure: Exception | None = None
        if listener is not None:
            try:
                listener.stop()
                if listener is not threading.current_thread() and listener.is_alive():
                    listener.join(timeout=_JOIN_TIMEOUT)
                    if listener.is_alive():
                        raise RuntimeError("全局热键监听线程未在限时内停止（回调已禁用）")
            except Exception as exc:
                failure = exc
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=_JOIN_TIMEOUT)
        if failure is not None:
            raise RuntimeError(f"停止全局热键监听器失败：{failure}") from failure
