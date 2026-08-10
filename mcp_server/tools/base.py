from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, TYPE_CHECKING

from mcp.server.fastmcp import Context
from mcp_server.registry import _get_ctx
from mcp_server.utils.privacy import strip_secrets

if TYPE_CHECKING:
    from mcp_server.context import AppContext

logger = logging.getLogger(__name__)

class _DedupCache:
    """SHA-256 dedup with TTL and periodic cleanup."""

    def __init__(self, ttl=300, max_size=10000):
        self._cache = {}
        self._ttl = ttl
        self._max_size = max_size
        self._last_cleanup = time.time()

    def _cleanup(self):
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

    def is_duplicate(self, session_id, tool, input_text):
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

def _get_memory(app: AppContext, layer: str, user_id: str):
    if layer == "agent":
        return app.mm.agent_memory(user_id)
    return app.mm.user_memory(user_id)

def _get_graph(app: AppContext, layer: str):
    if layer == "agent":
        return app.agent_graph
    return app.user_graph

def _get_wiki(app: AppContext, layer: str):
    if layer == "agent":
        return app.agent_wiki
    return app.user_wiki

def _get_hooks(app: AppContext, layer: str):
    if layer == "agent":
        return app.agent_hooks
    return app.user_hooks

_VALID_LAYERS = ("user", "agent")

def _validate_layer(layer: str) -> str:
    """Validate and normalize layer parameter."""
    if layer not in _VALID_LAYERS:
        raise ValueError(f"Invalid layer: {layer!r}. Must be one of {_VALID_LAYERS}")
    return layer

async def _fire_hook(hook_name: str, layer: str, context: dict, mem=None) -> dict:
    """Fire a hook safely — logs errors but never breaks the tool."""
    from hooks.registry import hook_registry

    try:
        return await hook_registry.fire(hook_name, layer, context, mem=mem)
    except Exception as e:
        logger.warning("Hook %s failed: %s", hook_name, e)
        return {"error": str(e)}

async def _check_rate_limit(app: AppContext, user_id: str) -> dict | None:
    """Check rate limit. Returns error dict if exceeded, None if ok."""
    try:
        result = await app.rate_limiter.check(user_id)
        if not result.get("allowed", True):
            return {
                "error": "rate_limit_exceeded",
                "remaining": result.get("remaining", 0),
                "reset_in": result.get("reset_in", 60),
            }
    except Exception:
        pass
    return None

# Context cache: {key: (timestamp, data)}
_context_cache: dict[str, tuple[float, dict]] = {}
_CONTEXT_CACHE_TTL = 30  # seconds

def _get_cache_key(layer: str, user_id: str) -> str:
    return f"{layer}:{user_id}"

def _get_cached(key: str) -> dict | None:
    if key in _context_cache:
        ts, data = _context_cache[key]
        if time.time() - ts < _CONTEXT_CACHE_TTL:
            return data
    return None

def _set_cached(key: str, data: dict) -> None:
    _context_cache[key] = (time.time(), data)

def _invalidate_cache(layer: str, user_id: str):
    _context_cache.pop(_get_cache_key(layer, user_id), None)

# Recall cache
_recall_cache: dict[str, tuple[float, list]] = {}
_RECALL_CACHE_TTL = 10  # seconds

def _get_recall_cache(query: str, user_id: str, layer: str, limit: int) -> list | None:
    key = hashlib.md5(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    if key in _recall_cache:
        ts, results = _recall_cache[key]
        if time.time() - ts < _RECALL_CACHE_TTL:
            return results
    return None

def _set_recall_cache(query: str, user_id: str, layer: str, limit: int, results: list) -> None:
    key = hashlib.md5(f"{layer}:{user_id}:{query}:{limit}".encode()).hexdigest()
    _recall_cache[key] = (time.time(), results)
