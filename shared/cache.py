from __future__ import annotations

"""
Memory Cache - LRU cache for hot data
"""

import threading
import time
from collections import OrderedDict
from typing import Any


class MemoryCache:
    def __init__(self, max_size: int = 1000, ttl: int = 300):
        self._max_size = max_size
        self._ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._cache:
                return None
            if time.time() - self._timestamps[key] > self._ttl:
                del self._cache[key]
                del self._timestamps[key]
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def set(self, key: str, value: Any):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            self._timestamps[key] = time.time()
            if len(self._cache) > self._max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
                del self._timestamps[oldest]

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                del self._timestamps[key]
                return True
            return False

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()

    def size(self) -> int:
        return len(self._cache)
