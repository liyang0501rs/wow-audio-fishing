"""Audio values shared by live and offline sources."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AudioChunk:
    samples: np.ndarray  # float32 mono
    sample_rate: int
    t_end: float  # end time on the source clock; live sources use monotonic()
