from __future__ import annotations

"""
Embeddings — async SQLite cache with multilingual model
"""

import asyncio
import hashlib
import os
import re
import struct
from typing import Any

from shared.circuit_breaker import breaker_registry
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

# E2: 3 consecutive model.encode failures → open 30s → hash-fallback service.
# Created through the registry factory (E2-audit fix): direct construction
# bypassed breaker_registry, making the diagnostics breaker check vacuous and
# the `reset_breakers` heal a no-op.
_embedding_breaker = breaker_registry.get("embedding_model", threshold=3, recovery_timeout=30.0)


def _configured_model() -> str:
    from config import config

    return str(config.get("embeddings", "model") or "intfloat/multilingual-e5-small")


DEFAULT_MODEL = _configured_model()
_model = None
_model_name = None


_fallback_warned = False


def _get_model(model_name: str | None = None) -> Any:
    global _model, _model_name, _fallback_warned
    # Deliberate hash mode: tests and resource-constrained installs set this
    # to keep embeddings deterministic and avoid loading the model.
    if os.environ.get("ARIEL_HASH_EMBEDDINGS"):
        return None
    target = model_name or DEFAULT_MODEL
    if _model is None or _model_name != target:
        try:
            from sentence_transformers import SentenceTransformer

            import logging

            logging.getLogger(__name__).info("Loading embedding model %s", target)
            _model = SentenceTransformer(target)
            _model_name = target
        except ImportError:
            _model = None
            if not _fallback_warned:
                _fallback_warned = True
                import logging

                logging.getLogger(__name__).warning(
                    "sentence-transformers not installed — using hash-fallback embeddings "
                    "(16 signal dims + zero padding; MIB search quality is degraded)."
                )
    return _model


class EmbeddingCache:
    def __init__(self, cm: AsyncConnectionManager | None = None, model_name: str | None = None) -> None:
        self._cm = cm or connection_manager
        self.model_name = model_name or DEFAULT_MODEL
        self._dimension = 384
        self._ready = False

    def _cache_model_tag(self, model: Any) -> str:
        """Cache rows are keyed by the BACKEND that produced them.

        Hash-fallback vectors must never be stored under the real model's
        name — otherwise installing sentence-transformers later would serve
        stale hash garbage as genuine model embeddings.
        """
        return self.model_name if model is not None else f"hash-fallback/{self.model_name}"

    async def ensure(self) -> None:
        """Lazy one-time schema setup so any consumer works without prior init."""
        if self._ready:
            return
        await self._init_db()
        self._ready = True

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                model_name TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """,
        )

    def _normalize_text(self, text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r"[^\w\s]", "", text)
        return re.sub(r"\s+", " ", text)

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(self._normalize_text(text).encode("utf-8")).hexdigest()

    async def _get_cached(self, text: str, cache_tag: str) -> list[float] | None:
        await self.ensure()
        text_hash = self._hash_text(text)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT embedding FROM embedding_cache WHERE text_hash=? AND model_name=?",
            (text_hash, cache_tag),
        )
        row = await cursor.fetchone()
        if row:
            blob: bytes = row[0]
            raw = list(struct.unpack(f"{len(blob) // 4}f", blob))
            import math

            return [v if math.isfinite(v) else 0.0 for v in raw]
        return None

    async def _cache(self, text: str, embedding: list[float], cache_tag: str) -> None:
        await self.ensure()
        text_hash = self._hash_text(text)
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
            (text_hash, blob, cache_tag),
        )
        await conn.commit()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        model = _get_model()
        cache_tag = self._cache_model_tag(model)
        results, to_compute = await self._get_results_from_cache(texts, cache_tag)

        if to_compute:
            computed = await self._compute_missing_embeddings(to_compute, cache_tag, model is not None)
            for idx, emb in computed.items():
                results[idx] = emb

        return [r if r is not None else [0.0] * self._dimension for r in results]

    async def _get_results_from_cache(self, texts: list[str], cache_tag: str) -> tuple[list[list[float] | None], list[tuple[int, str]]]:
        results: list[list[float] | None] = [None] * len(texts)
        to_compute: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            cached = await self._get_cached(text, cache_tag)
            if cached is not None:
                results[i] = cached
            else:
                to_compute.append((i, text))
        return results, to_compute

    async def _compute_missing_embeddings(self, to_compute: list[tuple[int, str]], cache_tag: str, has_model: bool) -> dict[int, list[float]]:
        computed: dict[int, list[float]] = {}

        # E2 circuit breaker: allow_request() is False while open → hash
        # fallback keeps recall serving; hash vectors cache under the
        # hash-fallback tag so they never masquerade as model embeddings.
        if has_model and _embedding_breaker.allow_request():
            model = _get_model()
            compute_texts = [t for _, t in to_compute]
            # encode() is CPU-bound sync work — keep it off the event loop
            try:
                embeddings = await asyncio.to_thread(model.encode, compute_texts)
            except Exception:
                _embedding_breaker.record_failure()
                raise
            _embedding_breaker.record_success()
            embeddings = embeddings.tolist()
            for (idx, text), emb in zip(to_compute, embeddings, strict=False):
                computed[idx] = emb
                await self._cache(text, emb, cache_tag)
        else:
            for idx, text in to_compute:
                emb = _hash_embedding(text)
                computed[idx] = emb
                await self._cache(text, emb, f"hash-fallback/{cache_tag}")
        return computed

    async def embed_single(self, text: str, *, prefix: str = "") -> list[float]:
        return (await self.embed([prefix + text]))[0]

    async def embed_with_prefix(self, texts: list[str], *, prefix: str = "") -> list[list[float]]:
        return await self.embed([prefix + t for t in texts])

    async def count(self) -> int:
        await self.ensure()
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM embedding_cache")).fetchone()
        return int(row[0]) if row else 0


async def embed_text(text: str, *, prefix: str = "") -> list[float]:
    """Prefix is prepended BEFORE hashing so each role gets its own cache row.

    e5 models are trained with instruction prefixes ("query: " for searches,
    "passage: " for indexed content) — matching them improves retrieval.
    """
    return await EmbeddingCache().embed_single(prefix + text)


async def embed_texts(texts: list[str], *, prefix: str = "") -> list[list[float]]:
    return await EmbeddingCache().embed([prefix + t for t in texts])


def similarity(a: list[float], b: list[float]) -> float:
    import math

    dot: float = sum(x * y for x, y in zip(a, b, strict=False))
    na: float = sum(x * x for x in a) ** 0.5
    nb: float = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0 or not math.isfinite(dot):
        return 0.0
    return dot / (na * nb)


def _hash_embedding(text: str, dim: int = 384) -> list[float]:
    import math

    h = hashlib.sha512(text.lower().encode()).digest()
    floats: list[float] = []
    for i in range(0, len(h) - 3, 4):
        if len(floats) >= dim:
            break
        val = struct.unpack("f", h[i : i + 4])[0]
        if math.isfinite(val):
            floats.append(val)
    while len(floats) < dim:
        floats.append(0.0)
    norm = sum(x * x for x in floats) ** 0.5
    if norm > 0:
        floats = [x / norm for x in floats]
    return floats[:dim]
