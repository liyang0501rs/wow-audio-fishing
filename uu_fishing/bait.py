"""Local timestamp for the last submitted bait application."""
from __future__ import annotations

from functools import cache
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Callable


BAIT_INTERVAL = 600.0
ROD_DELAY = 1.0
CAST_DELAY = 6.0


class BaitHistory:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._wall_clock = wall_clock
        self._loaded = False
        self._used_at = -math.inf

    def age(self) -> float:
        if not self._loaded:
            try:
                timestamp = float(self.path.read_text(encoding="ascii").strip())
            except (FileNotFoundError, ValueError):
                timestamp = 0.0
            except OSError as exc:
                raise OSError(f"无法读取鱼饵时间记录：{self.path}") from exc
            now = self._wall_clock()
            if 0 < timestamp <= now:
                self._used_at = self._clock() - (now - timestamp)
            self._loaded = True
        return self._clock() - self._used_at

    def record(self) -> None:
        self._used_at = self._clock()
        self._loaded = True
        timestamp = self._wall_clock()
        temp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="ascii", dir=self.path.parent,
                                    prefix=f".{self.path.name}.", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(f"{timestamp!r}\n")
            temp.replace(self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


@cache
def bait_history(path: Path) -> BaitHistory:
    """Share the lazy-loaded cache across red button stops and starts."""
    return BaitHistory(path)
