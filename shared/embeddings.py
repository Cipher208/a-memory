from __future__ import annotations

"""
Embeddings — async SQLite cache with multilingual model
"""

import asyncio
import hashlib
import logging
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


def _int8_enabled() -> bool:
    """A3.2: INT8 cache storage toggle (rag.int8_cache, default off)."""
    from config import config

    return bool(config.get("rag", "int8_cache", default=False))


def _encode_int8(vec: list[float]) -> bytes:
    """Symmetric per-vector INT8: magic uint16 + float32 scale + signed-byte dims.

    384 dims: 2 (magic) + 4 (scale) + 384 = 390 bytes vs 1536 float32 — 75% smaller.
    Magic (0x00A9) distinguishes INT8 blobs from legacy float32 (len%4==0).
    Round-trip error bounded by scale/127.
    """
    scale = max(abs(v) for v in vec) or 1.0
    qs = [max(-127, min(127, round(v / scale * 127))) for v in vec]
    return struct.pack("<Hf", 0x00A9, scale) + struct.pack(f"<{len(qs)}b", *qs)


def _decode_int8(blob: bytes) -> list[float] | None:
    """Decode an INT8 blob; returns None for corrupt/truncated blobs (cache miss)."""
    if len(blob) < 6:
        return None
    try:
        (_, scale) = struct.unpack("<Hf", blob[:6])
        dims = len(blob) - 6
        qs = struct.unpack(f"<{dims}b", blob[6:])
    except struct.error:
        return None
    if scale == 0 or scale != scale:  # zero or NaN guard
        return [0.0] * dims
    return [q / 127 * scale for q in qs]


# Chunk size for `... WHERE text_hash IN (?, ...)`. SQLite's default variable
# limit is 32766 on modern builds but 999 on older ones, so stay well below both.
_CACHE_LOOKUP_CHUNK = 500


def _decode_blob(blob: bytes) -> list[float] | None:
    """Decode a stored cache blob; None for corrupt/truncated rows (→ cache miss).

    Shared by the single-text and batched lookup paths so both apply exactly the
    same A3.2 rule: INT8 blobs start with a magic uint16 (0x00A9) + float32
    scale, legacy float32 blobs unpack as before (len % 4 == 0).
    """
    import math

    if len(blob) >= 6 and blob[0] == 0xA9 and blob[1] == 0x00:
        return _decode_int8(blob) or None
    if len(blob) % 4 == 0:
        raw = list(struct.unpack(f"{len(blob) // 4}f", blob))
        return [v if math.isfinite(v) else 0.0 for v in raw] or None
    if len(blob) < 6:
        return None  # corrupt/truncated row → cache miss, recompute
    return _decode_int8(blob)


def _chunked(items: list[str], size: int) -> list[list[str]]:
    """Split into chunks small enough for a single `IN (...)` parameter list."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fallback_tag(cache_tag: str) -> str:
    """Tag for a hash-fallback vector, prefixed exactly once.

    `_cache_model_tag` ALREADY returns `hash-fallback/<model>` when there is no
    model at all, so the old unconditional `f"hash-fallback/{cache_tag}"` doubled
    the prefix in that case: rows were written under
    `hash-fallback/hash-fallback/<model>` while lookups asked for
    `hash-fallback/<model>`. Every such row was therefore unreachable and the
    text was re-embedded (and re-committed) on every single run. Live evidence:
    29 such rows sat in cowagent's `embedding_cache` with the doubled tag.

    The prefix is still needed when the breaker opened while a model WAS
    available, because then `cache_tag` is the bare model name and the vector
    must not be stored under it.
    """
    if cache_tag.startswith("hash-fallback/"):
        return cache_tag
    return f"hash-fallback/{cache_tag}"


def _configured_model() -> str:
    from config import config

    return str(config.get("embeddings", "model") or "intfloat/multilingual-e5-small")


DEFAULT_MODEL = _configured_model()
_model = None
_model_name = None


def _remote_url() -> str:
    """Return the remote embeddings endpoint.

    Read from ``embeddings.url`` (e.g. the shared e5 indexer service).
    Empty/unset → local sentence-transformers or hash.
    """
    from config import config

    return str(config.get("embeddings", "url") or "").strip()


def _remote_active() -> bool:
    return bool(_remote_url()) and not os.environ.get("ARIEL_HASH_EMBEDDINGS")


_fallback_warned = False

# Degraded-embedding telemetry (2026-09-12): hash fallback used to be
# invisible — recall silently decayed to hash quality while every call
# "succeeded" (exactly the class of bug the old ARIEL_HASH_EMBEDDINGS
# default hid). Count every fallback for probes, log loud on the first and
# then every 100th.
_fallback_count = 0
_fallback_logged_at = 0


def hash_fallback_stats() -> dict[str, int]:
    """hash-fallback fires in this process (for the daily probe / diagnostics)."""
    return {"hash_fallback": _fallback_count}


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
        stale hash garbage as genuine model embeddings. Remote service
        vectors ARE genuine model vectors (same model, out-of-process).
        """
        if model is not None or _remote_active():
            return self.model_name
        return f"hash-fallback/{self.model_name}"

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
            return _decode_blob(row[0])
        return None

    async def _cache_many(self, items: list[tuple[str, list[float]]], cache_tag: str) -> None:
        """Write cache rows in bounded batches rather than one row at a time.

        The previous path committed per embedding, which cost a commit round trip
        for every vector — 3786 of them on hermes in a single nightly pass, at the
        same time as the model call it was meant to save.

        Batches are capped rather than written as one transaction on purpose: a
        single commit for the whole batch would lose every vector if the process
        is restarted mid-pass, whereas the old per-row path lost at most one. A
        hermes-sized pass costs 8 commits this way instead of 3786, and at most
        one batch is at risk.
        """
        if not items:
            return
        await self.ensure()
        int8 = _int8_enabled()
        conn = await self._cm.get(DB_NAME)
        for start in range(0, len(items), _CACHE_LOOKUP_CHUNK):
            batch = items[start : start + _CACHE_LOOKUP_CHUNK]
            rows = [
                (
                    self._hash_text(text),
                    _encode_int8(emb) if int8 else struct.pack(f"{len(emb)}f", *emb),
                    cache_tag,
                )
                for text, emb in batch
            ]
            await conn.executemany(
                "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
                rows,
            )
            await conn.commit()

    async def _cache(self, text: str, embedding: list[float], cache_tag: str) -> None:
        await self._cache_many([(text, embedding)], cache_tag)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        remote = _remote_active()
        model = None if remote else _get_model()
        cache_tag = self._cache_model_tag(model)
        results, to_compute = await self._get_results_from_cache(texts, cache_tag)

        if to_compute:
            computed = await self._compute_missing_embeddings(to_compute, cache_tag, remote or model is not None)
            for idx, emb in computed.items():
                results[idx] = emb

        return [r if r is not None else [0.0] * self._dimension for r in results]

    async def _get_results_from_cache(self, texts: list[str], cache_tag: str) -> tuple[list[list[float] | None], list[tuple[int, str]]]:
        """Look every text up in ONE query per chunk, not one query per text.

        This used to issue a separate SELECT — plus its own `ensure()` and
        `conn.get()` — for every text. On cowagent that was 1859 round trips for
        a layer whose rows were already 100% present, and it is why
        miner_embedding cost 16.5 s there while the model was never called: a
        cache hit has to be cheap, or it is not a cache.
        """
        results: list[list[float] | None] = [None] * len(texts)
        to_compute: list[tuple[int, str]] = []
        if not texts:
            return results, to_compute

        await self.ensure()
        hashes = [self._hash_text(t) for t in texts]
        conn = await self._cm.get(DB_NAME)
        by_hash: dict[str, list[float]] = {}
        for chunk in _chunked(list(dict.fromkeys(hashes)), _CACHE_LOOKUP_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = await (
                await conn.execute(
                    f"SELECT text_hash, embedding FROM embedding_cache WHERE model_name=? AND text_hash IN ({placeholders})",
                    (cache_tag, *chunk),
                )
            ).fetchall()
            for row in rows:
                decoded = _decode_blob(row[1])
                if decoded is not None:
                    by_hash[str(row[0])] = decoded

        for i, (text, text_hash) in enumerate(zip(texts, hashes, strict=True)):
            hit = by_hash.get(text_hash)
            if hit is None:
                to_compute.append((i, text))
            else:
                results[i] = hit
        return results, to_compute

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        """POST /v1/embeddings to the configured embeddings.url.

        Response is model-tagged with the configured model name, so cache
        rows are indistinguishable from local sentence-transformers output.
        Breaker semantics identical to the local model path.
        """
        import json
        import urllib.parse
        import urllib.request

        url = _remote_url()
        scheme = urllib.parse.urlparse(url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(f"embeddings.url must be http(s), got: {scheme or 'none'}")
        payload = json.dumps({"model": self.model_name, "input": texts}).encode()
        req = urllib.request.Request(  # noqa: S310 — scheme validated above
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _post() -> dict[str, Any]:
            # Scheme is validated above (http/https only).
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data: dict[str, Any] = json.loads(resp.read())
                return data

        data = await asyncio.to_thread(_post)
        # OpenAI-compatible shape: {"data": [{"embedding": [...], "index": n}, ...]}
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [list(map(float, d["embedding"])) for d in items]

    async def _compute_missing_embeddings(self, to_compute: list[tuple[int, str]], cache_tag: str, has_model: bool) -> dict[int, list[float]]:
        computed: dict[int, list[float]] = {}

        # E2 circuit breaker: allow_request() is False while open → hash
        # fallback keeps recall serving; hash vectors cache under the
        # hash-fallback tag so they never masquerade as model embeddings.
        if has_model and _embedding_breaker.allow_request():
            compute_texts = [t for _, t in to_compute]
            try:
                if _remote_active():
                    # Remote indexer service: encode happens out-of-process.
                    embeddings = await self._remote_embed(compute_texts)
                else:
                    # encode() is CPU-bound sync work — keep it off the event loop
                    model = _get_model()
                    raw = await asyncio.to_thread(model.encode, compute_texts)
                    # raw is a fresh local (no narrowing from the remote branch);
                    # ndarray.tolist() → list[list[float]]
                    embeddings = raw.tolist()
            except Exception:
                _embedding_breaker.record_failure()
                raise
            _embedding_breaker.record_success()
            pending: list[tuple[str, list[float]]] = []
            for (idx, text), emb in zip(to_compute, embeddings, strict=False):
                computed[idx] = emb
                pending.append((text, emb))
            await self._cache_many(pending, cache_tag)
        else:
            global _fallback_count, _fallback_logged_at
            _fallback_count += 1
            if _fallback_logged_at == 0 or _fallback_count - _fallback_logged_at >= 100:
                _fallback_logged_at = _fallback_count
                reason = "circuit breaker open" if has_model else "no model/remote configured"
                logging.getLogger(__name__).warning(
                    "embeddings: hash fallback x%d (%s) — recall quality degraded, check the e5 service :8710",
                    _fallback_count,
                    reason,
                )
            fallback: list[tuple[str, list[float]]] = []
            for idx, text in to_compute:
                emb = _hash_embedding(text)
                computed[idx] = emb
                fallback.append((text, emb))
            await self._cache_many(fallback, _fallback_tag(cache_tag))
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
