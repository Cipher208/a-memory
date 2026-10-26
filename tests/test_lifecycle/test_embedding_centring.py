"""F2b round 2: centring before quantisation.

`embed_to_binary` binarises by `sign(emb)`. e5 vectors share a large common
component — on the live hermes sample the mean vector has norm 0.91 while each
text vector has norm 1.0 — so `sign()` encodes the common component and every
pair scores Jaccard ~0.81, above `_EMBED_JACCARD = 0.7`. The threshold sits below
the noise floor: 100 % of pairs qualify, hence the 101 M candidates.

The miner now subtracts the per-run mean before quantising. `rag/quantize.py`
is untouched: the RAG binary path (`searcher`, `engine`, `ablation`) and
`embedding_clusters` must keep their current semantics.

Test 3 is the load-bearing one: it reproduces the geometry (mean norm 0.91,
text norm 1.0) and asserts that the uncentred sign vectors score ABOVE the
threshold while the centred ones score below it. Removing the centring, or
centring after quantising, makes it fail.
"""

import math
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

T = 1_700_000_000.0
_BITS = 384
# Measured on the live base: ||mean|| / ||text|| = 0.91 / 1.0.
_COMMON_NORM = 0.9
_COMMON = _COMMON_NORM / math.sqrt(_BITS)
# The rest of a unit vector after the common component: sqrt(1 - 0.81).
_UNIQUE = math.sqrt(1.0 - _COMMON_NORM**2)


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _node(content: str, ts: float = T) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
        (content, ts),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _seed_vector(text: str, vec: list[float]) -> None:
    from shared.embeddings import EmbeddingCache, _get_model

    cache = EmbeddingCache()
    await cache.ensure()
    tag = cache._cache_model_tag(_get_model())
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
        (cache._hash_text(text), struct.pack(f"{len(vec)}f", *vec), tag),
    )
    await conn.commit()


async def _edges(relation: str = "semantic_overlap") -> list[Any]:
    conn = await connection_manager.get(DB_NAME)
    return await (await conn.execute("SELECT * FROM epi_edges WHERE relation=? ORDER BY source_id, target_id", (relation,))).fetchall()


def _jaccard(a: bytes, b: bytes) -> float:
    ai, bi = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    inter = (ai & bi).bit_count()
    if inter == 0:
        return 0.0
    return inter / (ai | bi).bit_count()


def _max_pair_jaccard(binary: list[bytes]) -> float:
    best = 0.0
    for i in range(len(binary)):
        for j in range(i + 1, len(binary)):
            best = max(best, _jaccard(binary[i], binary[j]))
    return best


def _qualifying_pairs(binary: list[bytes], threshold: float) -> int:
    count = 0
    for i in range(len(binary)):
        for j in range(i + 1, len(binary)):
            if _jaccard(binary[i], binary[j]) >= threshold:
                count += 1
    return count


def _uncentred_binary(vecs: list[list[float]]) -> list[bytes]:
    from rag.quantize import embed_to_binary

    return [embed_to_binary(v, dim=len(v)) for v in vecs]


class _Capture:
    """Spy on the candidate structure: the exact bit vectors the miner built."""

    def __init__(self) -> None:
        self.binary: list[bytes] | None = None
        self.threshold: float | None = None
        self.pairs: tuple[Any, Any] | None = None


@pytest.fixture
def capture(monkeypatch) -> _Capture:
    import lifecycle.graph_miners as gm

    cap = _Capture()
    real = gm._semantic_candidate_order

    def spy(binary: list[bytes], threshold: float) -> tuple[Any, Any] | None:
        cap.binary = list(binary)
        cap.threshold = threshold
        cap.pairs = real(binary, threshold)
        return cap.pairs

    monkeypatch.setattr(gm, "_semantic_candidate_order", spy)
    return cap


def _fake_embed(monkeypatch, by_text: dict[str, list[float]]) -> None:
    """Serve controlled vectors through the miner's own `embed_texts` import."""
    import shared.embeddings as emb

    async def _fake(texts: list[str], *, prefix: str = "") -> list[list[float]]:
        return [by_text[t.strip()] for t in texts]

    monkeypatch.setattr(emb, "embed_texts", _fake)


def _common_plus(unique_dim: int) -> list[float]:
    """Unit-norm vector: the shared 0.9 component plus one unique basis dim."""
    vec = [_COMMON] * _BITS
    vec[unique_dim] += _UNIQUE
    return vec


def _pattern(rnd: Any, ones: int = _BITS // 2) -> int:
    """Random balanced bit pattern — the meaning part of an unrelated text."""
    dims = list(range(_BITS))
    rnd.shuffle(dims)
    bits = 0
    for d in dims[:ones]:
        bits |= 1 << d
    return bits


def _raw(bits: int) -> list[float]:
    return [_COMMON + (0.5 if (bits >> i) & 1 else -0.5) for i in range(_BITS)]


# --- 1 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_texts_score_one(db, capture):
    """Two identical texts must be exactly 1.0 — asserted on the miner's own bits."""
    import lifecycle.graph_miners as gm

    dup = "одинаковый текст про синхронизацию очередей"
    for text in (dup, dup, "совсем другой сюжет про духовку", "третий про капусту", "четвёртый про поезд"):
        await _node(text)

    conn = await connection_manager.get(DB_NAME)
    nodes = await gm._layer_nodes(conn, "user")

    await gm.miner_embedding(db, "user")

    assert capture.binary is not None, "the miner did not reach the candidate stage"
    dup_idx = [i for i, (_nid, content) in enumerate(nodes) if content == dup]
    assert len(dup_idx) == 2, "the fixture must contain the duplicate twice"
    i, j = dup_idx
    assert _jaccard(capture.binary[i], capture.binary[j]) == 1.0, "identical texts must score exactly 1.0"

    a, b = nodes[i][0], nodes[j][0]
    rows = await _edges()
    pairs = {(int(r["source_id"]), int(r["target_id"])) for r in rows}
    assert (min(a, b), max(a, b)) in pairs, f"the duplicate pair must become an edge: {pairs}"


# --- 2 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrelated_texts_stay_below_the_threshold(db, capture, monkeypatch):
    """Unrelated texts must not produce a single semantic_overlap edge.

    The vectors are seeded rather than taken from the hash fallback: the
    fallback lives in 16 dimensions, and centring a 16-dim set of 8 vectors
    makes them anti-correlated (they sum to zero), which inflates Jaccard — an
    artefact of the fallback, not of the miner.
    """
    import random

    import lifecycle.graph_miners as gm

    rnd = random.Random(5)
    texts = [
        "рецепт капустного пирога с дрожжевым тестом",
        "деплой сервиса через systemd юнит",
        "кот спит на подоконнике весь день",
        "курс доллара вырос на бирже утром",
        "упражнения для спины после сидячей работы",
        "настройка tailscale на впс",
        "погода в городе обещает дождь к вечеру",
        "книга про историю рима",
        "замена масла в двигателе",
        "таблица умножения для школьника",
        "выбор подарка на день рождения",
        "пересадка фикуса в больший горшок",
    ]
    vecs = {text: _raw(_pattern(rnd)) for text in texts}
    _fake_embed(monkeypatch, vecs)
    for text in texts:
        await _node(text)

    await gm.miner_embedding(db, "user")

    assert capture.binary is not None
    assert _max_pair_jaccard(capture.binary) < gm._EMBED_JACCARD, "unrelated texts scored above the threshold"
    assert await _edges() == []


# --- 3 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_centring_is_what_makes_the_threshold_meaningful(db, capture, monkeypatch):
    """The defect, reproduced in-test: sign() of the raw vectors scores 1.0.

    Geometry taken from the live measurement: every vector is the same 0.9
    component plus a unique 0.435 direction (norm 1.0). Uncentred, every
    coordinate is positive, so every vector is the all-ones bit pattern and
    every pair scores Jaccard 1.0 — above `_EMBED_JACCARD`, which is why the
    candidate list exploded. Centred, each vector keeps only its own bit and
    the pairs score 0.
    """
    import lifecycle.graph_miners as gm

    texts = [f"независимый текст номер {i}" for i in range(8)]
    vecs = {text: _common_plus(i) for i, text in enumerate(texts)}
    _fake_embed(monkeypatch, vecs)
    for text in texts:
        await _node(text)

    raw = [vecs[text] for text in texts]
    assert _max_pair_jaccard(_uncentred_binary(raw)) >= gm._EMBED_JACCARD, "the defect did not reproduce"

    await gm.miner_embedding(db, "user")

    assert capture.binary is not None
    assert _max_pair_jaccard(capture.binary) < gm._EMBED_JACCARD, "centring did not make the threshold meaningful"
    assert await _edges() == [], "the defect leaked into the graph"


# --- 4 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_candidate_bound_does_not_bind_on_a_realistic_fixture(db, capture):
    """A few duplicates among unrelated texts must not drop a single pair."""
    import lifecycle.graph_miners as gm

    for i in range(10):
        await _node(f"дубликат про очередь {i % 5}")
    for i in range(10):
        await _node(f"посторонняя тема {i}")

    await gm.miner_embedding(db, "user")

    assert capture.binary is not None and capture.pairs is not None
    ii, _jj = capture.pairs
    qualifying = _qualifying_pairs(capture.binary, gm._EMBED_JACCARD)
    assert qualifying > 0, "the fixture must produce qualifying pairs"
    assert int(ii.size) == qualifying, f"the safety net dropped pairs on realistic data: {int(ii.size)} != {qualifying}"


# --- 5 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bound_still_protects_a_pathological_corpus(db, capture):
    """A huge duplicate clique must not materialise every pair.

    After centring, the only way to get a mega-clique is many *identical*
    texts: near-duplicates that differ from the corpus mean collapse to noise
    once the mean is removed. The unrelated texts keep the mean away from the
    duplicates so their centred vectors stay non-degenerate.
    """
    import lifecycle.graph_miners as gm

    n_dup = 600
    for _ in range(n_dup):
        await _node("один и тот же текст, размноженный по всему корпусу")
    for i in range(6):
        await _node(f"совсем другая тема {i}")

    await gm.miner_embedding(db, "user")

    assert capture.binary is not None and capture.pairs is not None
    ii, _jj = capture.pairs
    bound = gm._EMBED_TOPK * gm._CANDIDATE_FACTOR
    n = len(capture.binary)
    total = int(ii.size)
    available = n * (n - 1) // 2

    assert total <= n * bound, f"candidate structure is not bounded: {total} pairs, n*M={n * bound}"
    assert total < available, f"no pair was dropped ({total} of {available}): the bound is inactive"
