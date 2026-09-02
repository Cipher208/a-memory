from __future__ import annotations

"""
L1 ReflexBuffer - ring buffer for recent messages
"""

import contextlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# ponytail: debounced persistence — L1 is a 50-entry ring, losing the last
# few seconds on a crash is acceptable; per-add fsync is not.
_SAVE_INTERVAL_SEC = 30.0
_SAVE_EVERY_ADDS = 10


@dataclass
class ReflexEntry:
    role: str
    content: str
    tokens: int
    timestamp: float


class ReflexBuffer:
    def __init__(self, max_size: int = 50, persist_path: str | None = None):
        self.max_size = max_size
        self.persist_path = persist_path
        self._buffer: deque[ReflexEntry] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._adds_since_save = 0
        self._last_save_ts = 0.0
        if persist_path:
            self._load()

    def add(self, role: str, content: str, tokens: int = 0) -> None:
        entry = ReflexEntry(role=role, content=content, tokens=tokens, timestamp=time.time())
        with self._lock:
            self._buffer.append(entry)
            self._adds_since_save += 1
            now = time.time()
            if self._last_save_ts == 0.0 or now - self._last_save_ts >= _SAVE_INTERVAL_SEC or self._adds_since_save >= _SAVE_EVERY_ADDS:
                self._save()
                self._adds_since_save = 0
                self._last_save_ts = now

    def get_recent(self, n: int = 10) -> list[ReflexEntry]:
        with self._lock:
            return list(self._buffer)[-n:]

    def get_full(self) -> list[ReflexEntry]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._save()

    def size(self) -> int:
        return len(self._buffer)

    def to_text(self, max_entries: int = 10) -> str:
        entries = self.get_recent(max_entries)
        return "\n".join([f"{e.role}: {e.content[:100]}" for e in entries])

    def _load(self) -> None:
        if self.persist_path and Path(self.persist_path).exists():
            try:
                with Path(self.persist_path).open() as f:
                    data = json.load(f)
                for entry in data:
                    self._buffer.append(ReflexEntry(**entry))
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                pass

    def restore(self, entries: list[ReflexEntry]) -> None:
        """Bulk-restore exported entries (E4 import). Newest wins via maxlen.

        Persists immediately — the import surface must not depend on a later
        add() to hit disk (E2-audit finding).
        """
        with self._lock:
            self._buffer.extend(entries)
            self._adds_since_save = _SAVE_EVERY_ADDS  # force save on next add
        self._save()

    def _save(self) -> None:
        if not self.persist_path:
            return
        path = Path(self.persist_path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w") as f:
                json.dump([vars(e) for e in self._buffer], f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on POSIX and Windows
        except (OSError, TypeError):
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
