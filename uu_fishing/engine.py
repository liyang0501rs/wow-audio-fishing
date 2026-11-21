"""Hardware-independent, audio-only cast/listen/reel state machine.

No screen pixels or game process are needed. All I/O and time are injectable for tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np

from .bait import BAIT_INTERVAL, CAST_DELAY, ROD_DELAY, BaitHistory
from .config import AppConfig
from .detection import BiteEvent
from .monitor import NearMiss, NearMissMonitor


class ActionBlocked(RuntimeError):
    """A focus/pause guard refused a key; pause rather than sending it elsewhere."""


class State(str, Enum):
    PAUSED = "paused"
    RESYNC = "resync"
    READY = "ready"
    BAIT_WAIT_ROD = "bait_wait_rod"
    BAIT_SETTLE = "bait_settle"
    SETTLE = "settle"
    LISTEN = "listen"
    REEL_WAIT = "reel_wait"
    LOOT_WAIT = "loot_wait"
    RETRY_WAIT = "retry_wait"
    STOPPED = "stopped"


class Source(Protocol):
    def read(self, timeout: float | None = None) -> Any: ...
    def flush(self) -> None: ...


class Controls:
    """Hotkey callbacks touch only these thread-safe controls, never the audio engine."""
    def __init__(self, *, paused: bool = True):
        self.stop_event = threading.Event()
        self._paused = paused
        self._generation = 0
        self._lock = threading.Lock()
        self.reason = "等待 F8 开始" if paused else "开始"
        self.stop_reason = ""

    def snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._paused, self._generation

    def pause(self, reason: str) -> None:
        with self._lock:
            if not self._paused:
                self._generation += 1
            self._paused = True
            self.reason = reason

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._generation += 1
            self.reason = "用户继续"

    def toggle(self) -> None:
        with self._lock:
            self._paused = not self._paused
            self._generation += 1
            self.reason = "用户暂停" if self._paused else "用户继续"

    def stop(self, reason: str = "用户停止") -> None:
        self.stop_reason = reason
        self.stop_event.set()


@dataclass
class Stats:
    casts: int = 0
    bite_events: int = 0
    reel_actions: int = 0
    timeouts: int = 0
    consecutive_timeouts: int = 0
    pauses: int = 0
    elapsed_seconds: float = 0.0
    stop_reason: str = ""


class FishingEngine:
    def __init__(
        self,
        config: AppConfig,
        source: Source,
        detector: Any,
        action: Callable[[], None],
        focused: Callable[[], bool],
        controls: Controls,
        *,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
        preview_score: Callable[[float], None] | None = None,
        bait: tuple[BaitHistory, Callable[[str, Callable[[], None] | None], None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg, self.source, self.detector = config, source, detector
        self.action, self.focused, self.controls = action, focused, controls
        self.emit = emit or (lambda _event, _data: None)
        self.preview_score = preview_score
        self.bait = bait
        self.clock = clock
        self.state = State.PAUSED
        self.stats = Stats()
        self._generation = -1
        self._deadline = 0.0
        self._cast_safe_after = 0.0
        self._last_audible = 0.0
        self._last_status = -math.inf
        self._near_miss = NearMissMonitor()

    def _event(self, event: str, **data: Any) -> None:
        self.emit(event, {"state": self.state.value, **data})

    def _set_state(self, state: State, **data: Any) -> None:
        self.state = state
        self._event("state", **data)

    def _clear_audio(self) -> None:
        self.source.flush()
        self.detector.reset()
        self._report_near_miss(self._near_miss.finish(self.clock()))

    def _report_near_miss(self, peak: NearMiss | None) -> None:
        if peak is not None:
            self._event("near_miss", clip=peak.clip, score=peak.score, threshold=peak.threshold)

    def _focus_ok(self) -> bool:
        try:
            return self.focused() is True
        except Exception:
            return False

    def _pause(self, reason: str) -> None:
        self.controls.pause(reason)
        self._clear_audio()
        self._generation = self.controls.snapshot()[1]
        self.stats.pauses += 1
        self._set_state(State.PAUSED, reason=reason)

    def _tap(self, action: Callable[[], None] | None = None) -> bool:
        paused, generation = self.controls.snapshot()
        if self.controls.stop_event.is_set():
            return False
        if paused or generation != self._generation:
            return False
        if not self._focus_ok():
            raise ActionBlocked("目标不在前台或焦点无法确认")
        (self.action if action is None else action)()
        return True

    def run(self) -> Stats:
        started = self.clock()
        self._last_audible = started
        limit = self.cfg.limits.duration_minutes
        session_deadline = started + limit * 60 if limit > 0 else math.inf
        try:
            while not self.controls.stop_event.is_set():
                if self.clock() >= session_deadline:
                    self.controls.stop("达到运行时长限制（包含暂停时间）")
                    break
                # Drain while paused/settling too: old audio must not fill a queue or trigger later.
                chunk = self.source.read()
                now = self.clock()
                samples = np.asarray(chunk.samples)
                if samples.size and not np.isfinite(samples).all():
                    raise RuntimeError("音频包含非有限数值，已停止")
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if samples.size else 0.0
                level = 20 * math.log10(rms + 1e-12)
                if level > -75.0:
                    self._last_audible = now
                if now >= session_deadline:
                    self.controls.stop("达到运行时长限制（包含暂停时间）")
                    break
                if self.controls.stop_event.is_set():
                    break
                paused, generation = self.controls.snapshot()
                if generation != self._generation:
                    self._generation = generation
                    self._clear_audio()
                    if paused:
                        self._set_state(State.PAUSED, reason=self.controls.reason)
                    else:
                        self.stats.consecutive_timeouts = 0
                        self._last_audible = now
                        # A paused cast may still be channeling remotely. Do not tap its interact
                        # key as if it were a new cast until the configured old cast has expired.
                        state = State.RESYNC if now < self._cast_safe_after else State.READY
                        self._set_state(state, reason="继续；丢弃旧音频并重新同步",
                                        wait_seconds=max(0.0, self._cast_safe_after - now))
                    continue
                if paused:
                    continue
                if not self._focus_ok():
                    self._pause("目标失焦或无法确认")
                    continue
                silence = self.cfg.limits.silence_timeout_seconds
                if silence > 0 and now - self._last_audible >= silence:
                    self._pause("持续近静音；请检查声音路由")
                    continue
                try:
                    self._step(now, chunk)
                except ActionBlocked as exc:
                    self._pause(str(exc))
                if now - self._last_status >= 0.3:
                    self._last_status = now
                    best = getattr(self.detector, "best_heard", None)
                    self._event("status", level_dbfs=round(level, 1),
                                score=float(best.score) if best is not None else -1.0,
                                clip=str(best.name) if best is not None else "",
                                casts=self.stats.casts, reels=self.stats.reel_actions)
        except KeyboardInterrupt:
            self.controls.stop("Ctrl+C 停止")
        finally:
            self.stats.elapsed_seconds = max(0.0, self.clock() - started)
            self.stats.stop_reason = self.controls.stop_reason or "错误中止，参见日志"
            self._report_near_miss(self._near_miss.finish(self.clock()))
            self.state = State.STOPPED
            self._event("summary", **vars(self.stats))
        return self.stats

    def _reel(self, bite: BiteEvent | None = None) -> None:
        """Send the reel action once, then apply the post-reel wait."""
        timing = self.cfg.timing
        try:
            if not self._tap():
                return
            completed_at = self.clock()
        finally:
            # Keep bite before action in the output, without writing it before the key.
            if bite is not None:
                self._event("bite", clip=bite.clip, score=float(bite.score))
        self._event("action", action="reel")
        self.stats.reel_actions += 1
        self.stats.consecutive_timeouts = 0
        # The old cast has ended. Preserve the post-reel wait even across F8 resume.
        self._cast_safe_after = self._deadline = completed_at + timing.loot_wait_seconds
        self._clear_audio()
        if (self.cfg.limits.max_reels > 0
                and self.stats.reel_actions >= self.cfg.limits.max_reels):
            self.controls.stop("达到收杆动作次数限制；未额外抛竿")
            return
        self._set_state(State.LOOT_WAIT, seconds=timing.loot_wait_seconds)

    def _bait_ready(self, now: float) -> bool:
        if self.bait is None:
            return True
        history, press_key = self.bait
        if self.state == State.BAIT_WAIT_ROD:
            if now < self._deadline:
                return False
            submitted = False

            def mark_submitted() -> None:
                nonlocal submitted
                submitted = True

            try:
                if not self._tap(lambda: press_key("9", mark_submitted)):
                    return False
            finally:
                if submitted:
                    # The key may already be sent even if focus changed during the hold.
                    self._deadline = self.clock() + CAST_DELAY
                    try:
                        history.record()
                    except OSError as exc:
                        self._event("bait_error", detail=str(exc))
                    self._event("bait_rod", key="9")
            self._set_state(State.BAIT_SETTLE, seconds=CAST_DELAY)
            return False
        if self.state == State.BAIT_SETTLE and now < self._deadline:
            return False
        age = history.age()
        if age < CAST_DELAY:
            wait = CAST_DELAY - age
            self._deadline = self.clock() + wait
            self._set_state(State.BAIT_SETTLE, seconds=wait)
            return False
        if age >= BAIT_INTERVAL:
            if self._tap(lambda: press_key("0", None)):
                self._deadline = self.clock() + ROD_DELAY
                self._event("bait_start", key="0")
                self._set_state(State.BAIT_WAIT_ROD, seconds=ROD_DELAY)
            return False
        return True

    def _step(self, now: float, chunk: Any) -> None:
        timing = self.cfg.timing
        if self.state == State.RESYNC:
            if now >= self._cast_safe_after:
                self._set_state(State.READY)
            return
        if self.state == State.LOOT_WAIT:
            if now < self._deadline:
                return
            # Recast on the first eligible chunk, not a whole audio block later.
            self._set_state(State.READY)
        if self.state in (State.READY, State.BAIT_WAIT_ROD, State.BAIT_SETTLE):
            if not self._bait_ready(now):
                return
            self._clear_audio()
            if not self._tap():
                return
            self._event("action", action="cast")
            self.stats.casts += 1
            self._cast_safe_after = now + timing.settle_seconds + timing.bite_timeout_seconds
            self._deadline = now + timing.settle_seconds
            self._set_state(State.SETTLE, seconds=timing.settle_seconds)
            return
        if self.state == State.SETTLE:
            if now >= self._deadline:
                self._clear_audio()
                self._deadline = now + timing.bite_timeout_seconds
                self._set_state(State.LISTEN, timeout=timing.bite_timeout_seconds)
            return
        if self.state == State.LISTEN:
            if now >= self._deadline:
                self.stats.timeouts += 1
                self.stats.consecutive_timeouts += 1
                self._event("timeout", consecutive=self.stats.consecutive_timeouts)
                self._clear_audio()
                if self.stats.consecutive_timeouts >= self.cfg.limits.max_consecutive_timeouts:
                    self._pause("连续超时")
                else:
                    self._deadline = now + timing.retry_wait_seconds
                    self._set_state(State.RETRY_WAIT)
                return
            events = self.detector.feed(chunk)
            if events:
                self._near_miss.discard()
                # Several templates can match the same sound. One cast permits only one reel.
                event = max(events, key=lambda item: item.score)
                self.stats.bite_events += 1
                if timing.reel_delay_seconds <= 0:
                    # Zero delay means reel in this same block, not one block later.
                    self._reel(event)
                else:
                    # Time from detection, not the estimated onset or the pre-feed chunk time.
                    self._deadline = self.clock() + timing.reel_delay_seconds
                    self._event("bite", clip=event.clip, score=float(event.score))
                    self._set_state(State.REEL_WAIT, seconds=timing.reel_delay_seconds)
            else:
                if self.preview_score is not None:
                    self.preview_score(self.detector.pending_peak)
                self._report_near_miss(self._near_miss.observe(now, self.detector))
            return
        if self.state == State.REEL_WAIT:
            if now >= self._deadline:
                self._reel()
            return
        if self.state == State.RETRY_WAIT and now >= self._deadline:
            self._set_state(State.READY)
