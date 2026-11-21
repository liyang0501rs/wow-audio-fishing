"""Fail-closed macOS input capture, with lazy SoundCard loading.

BlackHole is an input device, not a request to fall back to the default mic.
No native recorder buffer size is specified on CoreAudio. Only the public
record(numframes=...) read size is chosen here. All recorder lifetime operations
happen in the capture thread; close has a bounded join even if a driver hangs.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import math
import platform
import queue
import threading
import time
from typing import Any

import numpy as np

from .detection import AudioChunk


class AudioError(RuntimeError):
    """Input selection, capture, backlog, or stream health failure."""


@dataclass(frozen=True)
class AudioDevice:
    id: str
    name: str
    channels: int


def _soundcard() -> Any:
    if platform.system() != "Darwin":
        raise AudioError("Live audio capture is supported only on macOS; use a WAV for offline testing")
    try:
        return importlib.import_module("soundcard")
    except Exception as exc:
        raise AudioError("Cannot load SoundCard; install the macOS audio dependencies and allow audio capture") from exc


def _describe(microphone: Any) -> AudioDevice:
    channels = microphone.channels
    if not isinstance(channels, (int, np.integer)):
        channels = len(channels)
    return AudioDevice(str(microphone.id), str(microphone.name), int(channels))


def _microphones(soundcard: Any) -> list[Any]:
    try:
        # Do not list speaker loopbacks or substitute a system default input.
        return [mic for mic in soundcard.all_microphones(include_loopback=False) if _describe(mic).channels > 0]
    except Exception as exc:
        raise AudioError(f"Cannot enumerate audio input devices: {exc}") from exc


def list_devices() -> list[AudioDevice]:
    """List real input devices; live capture is only available on macOS."""
    return [_describe(mic) for mic in _microphones(_soundcard())]


class _StreamingResampler:
    """Linear interpolator with integer-rational phase across arbitrary blocks.

    The previous input sample is retained for interpolation across the boundary.
    Output index k always maps to input k * src_rate / dst_rate, independently
    of capture block lengths. It adds at most one input sample of lookahead.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._received = 0
        self._next_output = 0
        self._last: np.ndarray | None = None

    def process(self, samples: np.ndarray) -> np.ndarray:
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim != 1 or not np.all(np.isfinite(data)):
            raise AudioError("Capture returned invalid mono samples")
        if not len(data):
            return np.empty(0, dtype=np.float32)
        if self._last is None:
            start = self._received
            joined = data
        else:
            start = self._received - 1
            joined = np.concatenate((self._last, data))
        end = self._received + len(data) - 1
        last_output = (end * self.dst_rate) // self.src_rate
        indexes = np.arange(self._next_output, last_output + 1, dtype=np.int64)
        numerators = indexes * self.src_rate
        left = numerators // self.dst_rate - start
        fraction = (numerators % self.dst_rate).astype(np.float64) / self.dst_rate
        right = np.minimum(left + 1, len(joined) - 1)
        values = joined[left] * (1.0 - fraction) + joined[right] * fraction
        self._next_output = last_output + 1
        self._received += len(data)
        self._last = data[-1:].copy()
        return values.astype(np.float32)


def _positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


class LiveAudioSource:
    """Bounded, non-replaying BlackHole capture source.

    A queue overflow, invalid/empty frame, failed device, or read timeout poisons
    the source permanently: old queued audio is discarded and subsequent reads
    raise AudioError. Digital silence is valid audio, not a stream failure.
    """

    def __init__(
        self,
        device: str = "BlackHole",
        capture_rate: int = 48000,
        sample_rate: int = 22050,
        block_seconds: float = 0.1,
        read_timeout: float = 3.0,
        queue_seconds: float = 2.0,
    ) -> None:
        if not isinstance(device, str) or not device.strip():
            raise ValueError("device must be a nonempty device name or id")
        for rate, label in ((capture_rate, "capture_rate"), (sample_rate, "sample_rate")):
            if isinstance(rate, bool) or not isinstance(rate, (int, np.integer)) or rate <= 0:
                raise ValueError(f"{label} must be a positive integer")
        self.device = device
        self.capture_rate = int(capture_rate)
        self.sample_rate = int(sample_rate)
        self.block_seconds = _positive(block_seconds, "block_seconds")
        self.read_timeout = _positive(read_timeout, "read_timeout")
        self.queue_seconds = _positive(queue_seconds, "queue_seconds")
        self._frames = max(1, round(self.capture_rate * self.block_seconds))
        self._queue: queue.Queue[AudioChunk] = queue.Queue(maxsize=max(1, math.ceil(self.queue_seconds / self.block_seconds)))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: AudioError | None = None
        self._closed = False
        self._name = device

    @property
    def name(self) -> str:
        return self._name

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _fail(self, error: AudioError) -> None:
        with self._lock:
            if self._error is None:
                self._error = error
        self._stop.set()
        self._clear_queue()
        self._ready.set()

    def _check(self) -> None:
        if self._error is not None:
            raise self._error
        if self._closed:
            raise AudioError("Audio source is closed")

    def _start(self) -> None:
        self._check()
        if self._thread is None:
            soundcard = _soundcard()
            microphones = _microphones(soundcard)
            requested = self.device.casefold()
            exact = [mic for mic in microphones if requested in (_describe(mic).id.casefold(), _describe(mic).name.casefold())]
            matches = exact or [mic for mic in microphones if requested in _describe(mic).name.casefold() or requested in _describe(mic).id.casefold()]
            if not matches:
                names = ", ".join(_describe(mic).name for mic in microphones) or "none"
                raise AudioError(
                    f"Audio input '{self.device}' not found (available inputs: {names}); "
                    "no microphone fallback is allowed. Install BlackHole 2ch, route UU audio "
                    "into it, and see README.md"
                )
            if len(matches) != 1:
                names = ", ".join(f"{_describe(mic).name} [{_describe(mic).id}]" for mic in matches)
                raise AudioError(f"Audio input '{self.device}' is ambiguous: {names}; select an exact name or id")
            microphone = matches[0]
            self._name = _describe(microphone).name
            self._thread = threading.Thread(target=self._produce, args=(microphone,), name="uu-fishing-audio", daemon=True)
            self._thread.start()
        if not self._ready.wait(self.read_timeout):
            self._fail(AudioError(f"Audio input did not open within {self.read_timeout:g} seconds"))
        self._check()

    def _produce(self, microphone: Any) -> None:
        resampler = _StreamingResampler(self.capture_rate, self.sample_rate)
        try:
            # Deliberately omit blocksize: forcing 4800 into CoreAudio's low-level
            # buffer configuration can break the device on macOS/SoundCard.
            with microphone.recorder(samplerate=self.capture_rate) as recorder:
                self._ready.set()
                while not self._stop.is_set():
                    captured = np.asarray(recorder.record(numframes=self._frames), dtype=np.float32)
                    captured_at = time.monotonic()
                    if self._stop.is_set():
                        break
                    if captured.ndim == 2 and captured.shape[1] > 0:
                        captured = captured.mean(axis=1, dtype=np.float64).astype(np.float32)
                    if captured.ndim != 1 or not captured.size or not np.all(np.isfinite(captured)):
                        raise AudioError("Audio input returned an empty or invalid frame (stream disconnected)")
                    samples = resampler.process(captured)
                    if not samples.size:
                        continue
                    chunk = AudioChunk(samples=samples, sample_rate=self.sample_rate, t_end=captured_at)
                    try:
                        self._queue.put_nowait(chunk)
                    except queue.Full as exc:
                        raise AudioError("Audio capture queue overflowed; refusing stale audio (restart capture)") from exc
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc if isinstance(exc, AudioError) else AudioError(f"Audio capture failed: {exc}"))
        finally:
            self._ready.set()

    def __enter__(self) -> "LiveAudioSource":
        try:
            self._start()
        except Exception:
            try:
                self.close()
            except AudioError:
                pass
            raise
        return self

    def read(self, timeout: float | None = None) -> AudioChunk:
        """Read fresh audio; timeout is a fatal stream-health failure, not silence."""
        self._start()
        wait = self.read_timeout if timeout is None else float(timeout)
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("timeout must be nonnegative and finite")
        deadline = time.monotonic() + wait
        while True:
            self._check()
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail(AudioError(f"Audio input stalled: no frame within {wait:g} seconds"))
                    self._check()
                try:
                    chunk = self._queue.get(timeout=min(remaining, 0.05))
                except queue.Empty:
                    continue
            self._check()
            return chunk

    def flush(self) -> None:
        """Discard queued chunks only; never reset/resample/reopen the stream."""
        self._check()
        self._clear_queue()

    def close(self) -> None:
        self._closed = True
        self._stop.set()
        self._clear_queue()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            timeout = min(self.read_timeout, 1.0)
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise AudioError(f"Audio capture did not stop within {timeout:g} seconds; the input driver is blocked (source closed)")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.close()
        except AudioError:
            if exc_type is None:
                raise
