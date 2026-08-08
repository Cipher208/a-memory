from __future__ import annotations

"""
L1 ReflexBuffer - ring buffer for recent messages
"""

import json
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path


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
        if persist_path:
            self._load()

    def add(self, role: str, content: str, tokens: int = 0):
        import time

        entry = ReflexEntry(role=role, content=content, tokens=tokens, timestamp=time.time())
        with self._lock:
            self._buffer.append(entry)
            self._save()

    def get_recent(self, n: int = 10) -> list[ReflexEntry]:
        with self._lock:
            return list(self._buffer)[-n:]

    def get_full(self) -> list[ReflexEntry]:
        with self._lock:
            return list(self._buffer)

    def clear(self):
        with self._lock:
            self._buffer.clear()
            self._save()

    def size(self) -> int:
        return len(self._buffer)

    def to_text(self, max_entries: int = 10) -> str:
        entries = self.get_recent(max_entries)
        return "\n".join([f"{e.role}: {e.content[:100]}" for e in entries])

    def _load(self):
        if self.persist_path and Path(self.persist_path).exists():
            try:
                with Path(self.persist_path).open() as f:
                    data = json.load(f)
                for entry in data:
                    self._buffer.append(ReflexEntry(**entry))
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                pass

    def _save(self):
        if self.persist_path:
            try:
                with Path(self.persist_path, "w").open() as f:
                    json.dump([vars(e) for e in self._buffer], f)
            except (OSError, TypeError):
                pass
