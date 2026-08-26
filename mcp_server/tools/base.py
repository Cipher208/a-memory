from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable  # noqa: TC003 — runtime dataclass field types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp_server.context import AppContext
    from graph import EpistemicGraph
    from wiki import WikiManager
    from hooks import AgentHooks, UserHooks

logger = logging.getLogger(__name__)


class _DedupCache:
    """SHA-256 dedup with TTL and periodic cleanup."""

    def __init__(self, ttl: int = 300, max_size: int = 10000) -> None:
        self._cache: dict[str, float] = {}
        self._ttl = ttl
        self._max_size = max_size
        self._last_cleanup = time.time()

    def _cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        expired = [k for k, v in self._cache.items() if now - v > self._ttl]
        for k in expired:
            del self._cache[k]
        if len(self._cache) > self._max_size:
            oldest = sorted(self._cache.keys(), key=lambda k: self._cache[k])[: len(self._cache) // 4]
            for k in oldest:
                del self._cache[k]

    def is_duplicate(self, session_id: str, tool: str, input_text: str) -> bool:
        self._cleanup()
        key = hashlib.sha256(f"{session_id}:{tool}:{input_text[:500]}".encode()).hexdigest()
        now = time.time()
        if key in self._cache and now - self._cache[key] < self._ttl:
            return True
        self._cache[key] = now
        return False


_dedup_cache = _DedupCache(ttl=300, max_size=10000)

# Token budget configuration
DEFAULT_TOKEN_BUDGET = 2000
CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text))
    remaining_chars = len(text) - cjk_count
    non_cjk_tokens = remaining_chars // CHARS_PER_TOKEN
    return cjk_count + non_cjk_tokens


def _truncate_to_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    estimated = _estimate_tokens(text)
    if estimated <= max_tokens:
        return text, False
    char_limit = max_tokens * CHARS_PER_TOKEN
    lines = text.split("\n")
    result_lines = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > char_limit:
            break
        result_lines.append(line)
        current_len += line_len
    truncated = "\n".join(result_lines)
    truncated += "\n[...truncated to token budget]"
    return truncated, True


# ── Layer registry ──
# A layer is one bundle of backends. Primitives and tools never branch on
# layer names; a new layer (skill, tool, persona-graph) registers here once
# and every tool/primitive accepts it immediately.
@dataclass(frozen=True)
class LayerBinding:
    memory: Callable[[AppContext, str], Any]  # (app, user_id) -> MemoryManager (L3 + L4)
    graph: Callable[[AppContext], EpistemicGraph]
    wiki: Callable[[AppContext], WikiManager]
    hooks: Callable[[AppContext], AgentHooks | UserHooks]
    rag: Callable[[AppContext], Any]  # hybrid multi-RAG for dream/audit


_LAYER_BINDINGS: dict[str, LayerBinding] = {
    "user": LayerBinding(
        memory=lambda app, uid: app.mm.user_memory(uid),
        graph=lambda app: app.user_graph,
        wiki=lambda app: app.user_wiki,
        hooks=lambda app: app.user_hooks,
        rag=lambda app: app.user_multi,
    ),
    "agent": LayerBinding(
        memory=lambda app, uid: app.mm.agent_memory(uid),
        graph=lambda app: app.agent_graph,
        wiki=lambda app: app.agent_wiki,
        hooks=lambda app: app.agent_hooks,
        rag=lambda app: app.agent_multi,
    ),
}


def register_layer(name: str, binding: LayerBinding) -> None:
    """Add a new memory layer. All tools and primitives accept it right away."""
    _LAYER_BINDINGS[name.lower()] = binding


def get_layer(name: str) -> LayerBinding:
    return _LAYER_BINDINGS[_validate_layer(name)]


def _get_memory(app: AppContext, layer: str, user_id: str) -> Any:
    return get_layer(layer).memory(app, user_id)


def _get_graph(app: AppContext, layer: str) -> EpistemicGraph:
    return get_layer(layer).graph(app)


def _get_wiki(app: AppContext, layer: str) -> WikiManager:
    return get_layer(layer).wiki(app)


def _get_rag(app: AppContext, layer: str) -> Any:
    return get_layer(layer).rag(app)


def _validate_layer(layer: str) -> str:
    """Validate and normalize layer parameter against the layer registry."""
    normalized = (layer or "").strip().lower()
    if normalized not in _LAYER_BINDINGS:
        raise ValueError(f"Invalid layer: {layer!r}. Must be one of {tuple(_LAYER_BINDINGS)}")
    from config import config

    if not config.get("layers", normalized, "enabled", default=True):
        raise ValueError(f"Layer {normalized!r} is disabled in config")
    return normalized


async def _fire_hook(hook_name: str, layer: str, context: dict[str, Any], mem: Any = None) -> dict[str, Any]:
    """Fire a hook safely — logs errors but never breaks the tool."""
    from hooks.registry import hook_registry

    try:
        return await hook_registry.fire(hook_name, layer, context, mem=mem)
    except Exception as e:
        logger.warning(f"Hook {hook_name} failed: {e}")
        return {"error": str(e)}


async def _check_rate_limit(app: AppContext, user_id: str) -> dict[str, Any] | None:
    """Check rate limit. Returns error dict if exceeded, None if ok."""
    with contextlib.suppress(Exception):
        result = await app.rate_limiter.check(user_id)
        if not result.get("allowed", True):
            return {
                "error": "rate_limit_exceeded",
                "remaining": result.get("remaining", 0),
                "reset_in": result.get("reset_in", 60),
            }
    return None


# Context cache: {key: (timestamp, data)}
_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CONTEXT_CACHE_TTL = 30  # seconds


def _get_cache_key(layer: str, user_id: str) -> str:
    return f"{layer}:{user_id}"


def _get_cached(key: str) -> dict[str, Any] | None:
    if key in _context_cache:
        ts, data = _context_cache[key]
        if time.time() - ts < _CONTEXT_CACHE_TTL:
            return data
    return None


def _set_cached(key: str, data: dict[str, Any]) -> None:
    _context_cache[key] = (time.time(), data)


def _invalidate_cache(layer: str, user_id: str) -> None:
    _context_cache.pop(_get_cache_key(layer, user_id), None)


# Recall cache
_recall_cache: dict[str, tuple[float, list[Any]]] = {}
_RECALL_CACHE_TTL = 10  # seconds
_RECALL_CACHE_MAX = 512
_recall_cache_lock = threading.Lock()


def _get_recall_cache(query: str, user_id: str, layer: str, limit: int) -> list[Any] | None:
    # SHA-256 for cache key, MD5 is flagged as weak by security scanners
    key = hashlib.sha256(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    with _recall_cache_lock:
        if key in _recall_cache:
            ts, results = _recall_cache[key]
            if time.time() - ts < _RECALL_CACHE_TTL:
                return results
    return None


def _set_recall_cache(query: str, user_id: str, layer: str, limit: int, results: list[Any]) -> None:
    # SHA-256 for cache key, MD5 is flagged as weak by security scanners
    key = hashlib.sha256(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    now = time.time()
    with _recall_cache_lock:
        # Bound memory: distinct queries would otherwise grow the dict forever.
        if len(_recall_cache) >= _RECALL_CACHE_MAX:
            expired = [k for k, (ts, _) in _recall_cache.items() if now - ts >= _RECALL_CACHE_TTL]
            for k in expired:
                del _recall_cache[k]
            while len(_recall_cache) >= _RECALL_CACHE_MAX:
                _recall_cache.pop(next(iter(_recall_cache)))
        _recall_cache[key] = (now, results)
