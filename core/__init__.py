"""
Core Memory Module — L1-L4 async
Two-layer: user facts + agent identity
"""

from typing import Optional

from config import config
from shared.connection import AsyncConnectionManager, connection_manager

from .episodic import EpisodicMemory
from .memory import CoreMemory
from .reflex import ReflexBuffer
from .session import SessionStore


class MemoryLayer:
    """Unified async memory layer for both user and agent."""

    def __init__(self, layer_type: str, user_id: str = "default", cm: AsyncConnectionManager | None = None, cache=None):
        self.layer_type = layer_type
        self.user_id = user_id
        self._cm = cm or connection_manager
        self._cache = cache
        self.l1 = ReflexBuffer(max_size=config.get_limit("l1_buffer_size"))
        self.l2 = SessionStore(cm=self._cm)
        self.l3 = EpisodicMemory(cm=self._cm)
        self.l4 = CoreMemory(cm=self._cm)

    async def remember(self, key: str, value: str, importance: float = 0.5) -> int:
        return await self.l4.save(self.user_id, key, value, importance)

    async def recall(self, query: str, limit: int = 10) -> list[dict]:
        cache_key = f"recall:{self.user_id}:{query}:{limit}"
        cached = self._cache.get(cache_key) if self._cache else None
        if cached is not None:
            return cached

        results = []
        results.extend(await self.l4.search(self.user_id, query, limit))
        episodes = await self.l3.search(self.user_id, query, limit)
        results.extend([{"summary": e.summary, "weight": e.emotional_weight} for e in episodes])
        final = results[:limit]

        if self._cache:
            self._cache.set(cache_key, final)

        return final

    async def forget(self, key: str) -> bool:
        return await self.l4.delete(self.user_id, key)

    async def get_context(self) -> str:
        parts = []
        recent = self.l1.get_recent(5)
        if recent:
            parts.append("RECENT: " + "; ".join([r.content[:50] for r in recent]))
        facts = await self.l4.get_all(self.user_id, limit=10)
        if facts:
            parts.append("FACTS: " + "; ".join([f"{f.key}={f.value[:30]}" for f in facts]))
        return "\n".join(parts)

    async def cleanup(self) -> dict:
        archived = await self.l3.archive_old(self.user_id)
        return {"archived": archived}


class MemoryManager:
    def __init__(self, cm: AsyncConnectionManager | None = None, cache=None):
        self._cm = cm or connection_manager
        self._cache = cache
        self.layers: dict[str, MemoryLayer] = {}

    def get_layer(self, layer_type: str, user_id: str = "default") -> MemoryLayer:
        key = f"{layer_type}:{user_id}"
        if key not in self.layers:
            self.layers[key] = MemoryLayer(layer_type, user_id, cm=self._cm, cache=self._cache)
        return self.layers[key]

    def user_memory(self, user_id: str = "default") -> MemoryLayer:
        return self.get_layer("user", user_id)

    def agent_memory(self, user_id: str = "default") -> MemoryLayer:
        return self.get_layer("agent", user_id)

    async def cleanup_all(self) -> dict:
        results = {}
        for key, layer in self.layers.items():
            results[key] = await layer.cleanup()
        return results


# Global instance (no cache — use MemoryManager(cache=MemoryCache()) for caching)
memory_manager = MemoryManager()
