"""One byte-bounded JSONL log shared by all sessions of this installation."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:
    fcntl = None


LOG_NAME = "fishing.jsonl"
MAX_BYTES = 10_000_000
_fallback_lock = Lock()


@contextmanager
def _lock_directory(directory: Path):
    if fcntl is None:
        # The live tool is macOS-only; this handles local offline threads on Windows.
        with _fallback_lock:
            yield
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        # Lock the directory instead of the log inode, which can change at rollover.
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class SessionLog:
    def __init__(self, directory: Path):
        self.path = directory / LOG_NAME
        self.session_id = uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **data: Any) -> None:
        fields = {"time": datetime.now().astimezone().isoformat(),
                  "session_id": self.session_id}

        def encode(record: dict[str, Any]) -> bytes:
            return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

        line = encode({**fields, "event": event, **data})
        if len(line) > MAX_BYTES:
            line = encode({**fields, "event": "log_entry_too_large", "original_event": event[:80],
                           "bytes": len(line)})

        with _lock_directory(self.path.parent):
            try:
                with self.path.open("r+b") as current:
                    size = current.seek(0, os.SEEK_END)
                    if size + len(line) <= MAX_BYTES:
                        current.write(line)
                        return
                    # Reclaim a little extra to avoid rewriting 10 MB for each new line.
                    keep = min(MAX_BYTES - len(line), MAX_BYTES * 9 // 10)
                    start = max(0, size - keep)
                    if start:
                        current.seek(start - 1)
                        if current.read(1) != b"\n":
                            current.readline()
                    else:
                        current.seek(0)
                    tail = current.read()
                    if tail and not tail.endswith(b"\n"):
                        tail = tail[:tail.rfind(b"\n") + 1]
            except FileNotFoundError:
                with self.path.open("xb") as stream:
                    stream.write(line)
                return

            temp = None
            try:
                with NamedTemporaryFile(dir=self.path.parent, prefix=f".{LOG_NAME}.",
                                        delete=False) as stream:
                    temp = Path(stream.name)
                    stream.write(tail)
                    stream.write(line)
                temp.replace(self.path)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)
