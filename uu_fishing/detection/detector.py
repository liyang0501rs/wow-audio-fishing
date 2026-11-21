"""Streaming log-mel bite detector (no vision, device, or application config).

Adapted from rsamf/wow-fishing-forever, a6c4da4fd8dfbdf9bc80c20e8ed656701f6d2ea7.
Copyright (c) 2019-2026 Richard Franklin. MIT; see third_party/rsamf/LICENSE.
Local changes: source-clock timestamps, validation, complete reset, and shared
refractory arbitration so correlated templates cannot emit duplicate bites.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

import numpy as np

from ._types import AudioChunk
from .features import DEFAULT_MEL, MelParams, log_mel
from .fingerprint import Fingerprint, score_sequence


@dataclass(frozen=True)
class ClipScore:
    name: str
    score: float
    t: float


@dataclass(frozen=True)
class BiteEvent:
    clip: str
    score: float
    t: float  # estimated onset, on the AudioChunk.t_end clock


PEAK_HOLD_S = 0.02


class BiteDetector:
    def __init__(
        self,
        fingerprints: Sequence[Fingerprint],
        p: MelParams = DEFAULT_MEL,
        *,
        threshold: float = 0.66,
        threshold_floors: Mapping[str, float] | None = None,
        refractory_s: float = 3.0,
        min_consecutive: int = 1,
    ) -> None:
        self.fingerprints = tuple(fingerprints)
        if not self.fingerprints:
            raise ValueError("At least one audio fingerprint is required")
        if not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        if not math.isfinite(refractory_s) or refractory_s < 0:
            raise ValueError("refractory_s must be finite and nonnegative")
        if isinstance(min_consecutive, bool) or not isinstance(min_consecutive, int) or min_consecutive < 1:
            raise ValueError("min_consecutive must be a positive integer")
        names = [fp.name for fp in self.fingerprints]
        if len(set(names)) != len(names):
            raise ValueError("Fingerprint names must be unique")
        for fp in self.fingerprints:
            if fp.feats.ndim != 2 or fp.length < 2 or fp.feats.shape[1] != p.n_mels or not np.all(np.isfinite(fp.feats)):
                raise ValueError(f"Invalid fingerprint: {fp.name!r}")
        self.p = p
        self.threshold = float(threshold)
        self._thresholds = dict.fromkeys(names, self.threshold)
        for name, floor in (threshold_floors or {}).items():
            if name not in self._thresholds or not math.isfinite(floor) or not 0 < floor <= 1:
                raise ValueError(f"Invalid threshold floor for {name!r}: {floor!r}")
            self._thresholds[name] = max(self.threshold, float(floor))
        self.refractory_s = float(refractory_s)
        self.min_consecutive = min_consecutive
        self._max_len = max(fp.length for fp in self.fingerprints)
        self._hold_frames = max(1, round(PEAK_HOLD_S / p.frame_s))
        self._run = dict.fromkeys(names, 0)
        self._armed: dict[str, tuple[float, float, int]] = {}
        self._peaks = dict.fromkeys(names, -1.0)
        self._scores: dict[str, ClipScore] = {}
        self.reset()

    def reset(self) -> None:
        """Forget buffered audio, diagnostic scores, timestamps and refractory."""
        self._pending = np.zeros(0, dtype=np.float32)
        self._feats = np.zeros((0, self.p.n_mels), dtype=np.float32)
        self._frames_total = 0
        self._origin: float | None = None
        self._run = dict.fromkeys(self._run, 0)
        self._armed.clear()
        self._quiet_until = -math.inf
        self._best = ClipScore("", -1.0, 0.0)
        self._peaks = dict.fromkeys(self._peaks, -1.0)
        self._scores = {name: ClipScore(name, -1.0, 0.0) for name in self._run}
        self._level_dbfs = -120.0

    def threshold_for(self, clip: str) -> float:
        return self._thresholds[clip]

    @property
    def last_scores(self) -> tuple[ClipScore, ...]:
        return tuple(self._scores[fp.name] for fp in self.fingerprints)

    @property
    def best_heard(self) -> ClipScore:
        """Highest-scoring clip since reset (empty name until a window scores)."""
        return self._best

    def take_peaks(self) -> dict[str, float]:
        """Per-clip maximum since the previous call; -1 means no audible match."""
        peaks = self._peaks
        self._peaks = dict.fromkeys(peaks, -1.0)
        return peaks

    @property
    def pending_peak(self) -> float:
        """Best unread score, without consuming the diagnostic peaks."""
        return max(self._peaks.values())

    @property
    def level_dbfs(self) -> float:
        return self._level_dbfs

    def feed(self, chunk: AudioChunk) -> list[BiteEvent]:
        """Consume contiguous audio and return chronological, deduplicated bites."""
        p = self.p
        if chunk.sample_rate != p.sample_rate:
            raise ValueError(f"chunk sample rate {chunk.sample_rate} != {p.sample_rate}; resample in the source, not per chunk")
        samples = np.asarray(chunk.samples, dtype=np.float32)
        if samples.ndim != 1 or not np.all(np.isfinite(samples)):
            raise ValueError("AudioChunk must contain finite, one-dimensional mono samples")
        if not math.isfinite(chunk.t_end):
            raise ValueError("AudioChunk.t_end must be finite")
        if not len(samples):
            return []
        if self._origin is None:
            self._origin = chunk.t_end - len(samples) / p.sample_rate
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        self._level_dbfs = 20.0 * math.log10(max(rms, 1e-6))
        buf = np.concatenate([self._pending, samples])
        n_new = p.frames_for(len(buf))
        if n_new == 0:
            self._pending = buf
            return []
        new = log_mel(buf[: (n_new - 1) * p.hop + p.n_fft], p)
        self._pending = buf[n_new * p.hop :]
        feats = np.concatenate([self._feats, new])
        self._frames_total += n_new

        per_clip: dict[str, np.ndarray] = {}
        for fp in self.fingerprints:
            avail = min(n_new, len(feats) - fp.length + 1)
            col = np.full(n_new, np.nan, dtype=np.float32)
            if avail > 0:
                col[n_new - avail :] = score_sequence(feats[-(avail + fp.length - 1) :], fp)[fp.length - 1 :]
            per_clip[fp.name] = col

        events: list[BiteEvent] = []
        for j in range(n_new):
            frame = self._frames_total - n_new + j
            t_end = self._origin + (frame * p.hop + p.n_fft) / p.sample_rate
            candidates: list[tuple[str, tuple[float, float, int]]] = []
            for fp in self.fingerprints:
                name = fp.name
                score = float(per_clip[name][j])
                if math.isnan(score):
                    continue
                duration = ((fp.length - 1) * p.hop + p.n_fft) / p.sample_rate
                t_onset = t_end - duration
                self._scores[name] = ClipScore(name, score, t_onset)
                if score > self._best.score:
                    self._best = self._scores[name]
                self._peaks[name] = max(self._peaks[name], score)
                armed = self._armed.get(name)
                if score < self._thresholds[name]:
                    self._run[name] = 0
                    if armed is not None:
                        candidates.append((name, self._armed.pop(name)))
                    continue
                self._run[name] += 1
                if armed is not None:
                    peak, peak_onset, since = armed
                    armed = (score, t_onset, 0) if score > peak else (peak, peak_onset, since + 1)
                    self._armed[name] = armed
                    if armed[2] >= self._hold_frames:
                        candidates.append((name, self._armed.pop(name)))
                elif self._run[name] == self.min_consecutive and t_end >= self._quiet_until:
                    self._armed[name] = (score, t_onset, 0)
            if candidates and t_end >= self._quiet_until:
                # An already-armed correlated template is the same physical bite,
                # not a second click. Select the strongest peak and disarm all.
                candidates.extend(self._armed.items())
                name, (peak, onset, _) = max(candidates, key=lambda item: item[1][0])
                events.append(BiteEvent(name, peak, onset))
                self._quiet_until = t_end + self.refractory_s
                self._armed.clear()
        self._feats = feats[-(2 * self._max_len) :]
        return events
