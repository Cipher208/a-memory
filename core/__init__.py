from __future__ import annotations

"""
Core Memory Module — L1-L4 async
Two-layer: user facts + agent identity
"""

from typing import Optional, Any

from config import config
from shared.connection import AsyncConnectionManager, connection_manager

from .episodic import EpisodicMemory
from .memory import CoreMemory
from .reflex import ReflexBuffer
from .session import SessionStore


from shared.constants import DEFAULT_USER, DEFAULT_LAYER, AGENT_LAYER, UTF8


class MemoryLayer:
    """Unified async memory layer for both user and agent."""

    def __init__(self, layer_type: str, user_id: str = DEFAULT_USER, cm: AsyncConnectionManager | None = None, cache: Any = None) -> None:
        self.layer_type = layer_type
        self.user_id = user_id
        self._cm = cm or connection_manager
        self._cache: Any = cache
        self.l1 = ReflexBuffer(max_size=config.get_limit("l1_buffer_size"))
        self.l2 = SessionStore(cm=self._cm)
        self.l3 = EpisodicMemory(cm=self._cm, layer=layer_type)
        self.l4 = CoreMemory(cm=self._cm, layer=layer_type)

    async def remember(self, key: str, value: str, importance: float = 0.5) -> int:
        return await self.l4.save(self.user_id, key, value, importance)

    async def recall(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        cache_key = f"recall:{self.user_id}:{query}:{limit}"
        cached: Any = self._cache.get(cache_key) if self._cache else None
        if cached is not None:
            return list(cached) if isinstance(cached, list) else []

        results: list[dict[str, Any]] = []
        l4_hits = await self.l4.search(self.user_id, query, limit)
        results.extend(l4_hits)
        episodes = await self.l3.search(self.user_id, query, limit)
        results.extend([{"summary": e.summary, "weight": e.emotional_weight} for e in episodes])
        final = results[:limit]

        if self._cache:
            self._cache.set(cache_key, final)

        return final

    async def forget(self, key: str) -> bool:
        return await self.l4.delete(self.user_id, key)

    async def get_context(self) -> str:
        parts: list[str] = []
        recent = self.l1.get_recent(5)
        if recent:
            parts.append("RECENT: " + "; ".join([str(r.content)[:50] for r in recent]))
        facts = await self.l4.get_all(self.user_id, limit=10)
        if facts:
            parts.append("FACTS: " + "; ".join([f"{f.key}={str(f.value)[:30]}" for f in facts]))
        return "\n".join(parts)

    async def cleanup(self) -> dict[str, int]:
        archived = await self.l3.archive_old(self.user_id)
        return {"archived": archived}


class MemoryManager:
    def __init__(self, cm: AsyncConnectionManager | None = None, cache: Any = None) -> None:
        self._cm = cm or connection_manager
        self._cache: Any = cache
        self.layers: dict[str, MemoryLayer] = {}

    def get_layer(self, layer_type: str, user_id: str = DEFAULT_USER) -> MemoryLayer:
        key = f"{layer_type}:{user_id}"
        if key not in self.layers:
            self.layers[key] = MemoryLayer(layer_type, user_id, cm=self._cm, cache=self._cache)
        return self.layers[key]

    def user_memory(self, user_id: str = DEFAULT_USER) -> MemoryLayer:
        return self.get_layer(DEFAULT_LAYER, user_id)

    def agent_memory(self, user_id: str = DEFAULT_USER) -> MemoryLayer:
        return self.get_layer(AGENT_LAYER, user_id)

    async def cleanup_all(self) -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        for key, layer in self.layers.items():
            results[key] = await layer.cleanup()
        return results


# Global instance (no cache — use MemoryManager(cache=MemoryCache()) for caching)
memory_manager = MemoryManager()
