"""
Embeddings — async SQLite cache with multilingual model
"""

import hashlib
import re
import struct

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

DEFAULT_MODEL = "intfloat/multilingual-e5-small"
_model = None
_model_name = None


def _get_model(model_name: str | None = None):
    global _model, _model_name
    target = model_name or DEFAULT_MODEL
    if _model is None or _model_name != target:
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(target)
            _model_name = target
        except ImportError:
            _model = None
    return _model


class EmbeddingCache:
    def __init__(self, cm: "AsyncConnectionManager" | None = None, model_name: str | None = None):
        self._cm = cm or connection_manager
        self.model_name = model_name or DEFAULT_MODEL
        self._dimension = 384

    async def _init_db(self):
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
        text = re.sub(r"\s+", " ", text)
        return text

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(self._normalize_text(text).encode("utf-8")).hexdigest()

    async def _get_cached(self, text: str) -> list[float] | None:
        text_hash = self._hash_text(text)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "SELECT embedding FROM embedding_cache WHERE text_hash=? AND model_name=?",
            (text_hash, self.model_name),
        )
        row = await cursor.fetchone()
        if row:
            blob = row[0]
            raw = list(struct.unpack("%df" % (len(blob) // 4), blob))
            import math

            return [v if math.isfinite(v) else 0.0 for v in raw]
        return None

    async def _cache(self, text: str, embedding: list[float]):
        text_hash = self._hash_text(text)
        blob = struct.pack("%df" % len(embedding), *embedding)
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
            (text_hash, blob, self.model_name),
        )
        await conn.commit()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = _get_model()
        results: list[list[float] | None] = [None] * len(texts)
        to_compute = []
        for i, text in enumerate(texts):
            cached = await self._get_cached(text)
            if cached is not None:
                results[i] = cached
            else:
                to_compute.append((i, text))
        if to_compute and model:
            compute_texts = [t for _, t in to_compute]
            embeddings = model.encode(compute_texts).tolist()
            for (idx, text), emb in zip(to_compute, embeddings, strict=False):
                results[idx] = emb
                await self._cache(text, emb)
        elif to_compute:
            for idx, text in to_compute:
                emb = _hash_embedding(text)
                results[idx] = emb
                await self._cache(text, emb)
        return [r if r is not None else [0.0] * self._dimension for r in results]

    async def embed_single(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def count(self) -> int:
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT COUNT(*) FROM embedding_cache")).fetchone()
        return row[0] if row else 0


async def embed_text(text: str) -> list[float]:
    return await EmbeddingCache().embed_single(text)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    return await EmbeddingCache().embed(texts)


def similarity(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
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
