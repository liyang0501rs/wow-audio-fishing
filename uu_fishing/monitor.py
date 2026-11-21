"""Rate-limited diagnostic peaks, independent of bite detection and input."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class NearMiss:
    clip: str
    score: float
    threshold: float


class NearMissMonitor:
    def __init__(self) -> None:
        self._peak: NearMiss | None = None
        self._deadline = math.inf
        self._last_report = -math.inf

    def discard(self) -> None:
        self._peak = None
        self._deadline = math.inf

    def finish(self, now: float) -> NearMiss | None:
        peak = self._peak
        self.discard()
        if peak is not None and now >= self._last_report + 1.0:
            self._last_report = now
            return peak
        return None

    def observe(self, now: float, detector) -> NearMiss | None:
        report = self.finish(now) if now >= self._deadline else None
        take_peaks = getattr(detector, "take_peaks", None)
        if take_peaks is None:
            return report
        for clip, score in take_peaks().items():
            threshold = detector.threshold_for(clip)
            if 0.60 <= score < threshold and (self._peak is None or score > self._peak.score):
                if self._peak is None:
                    self._deadline = now + 1.0
                self._peak = NearMiss(clip, score, threshold)
        return report
