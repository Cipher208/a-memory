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
# Per-dimension amplitude of the meaning part: 384 * _AMP**2 = 1 - 0.81, so a
# balanced pattern is a unit vector whose mean over the batch is exactly the
# common component. This is the real e5 geometry: ||mean|| / ||row|| = 0.90.
# (An amplitude of 0.5 per dimension would make the rows ~10 long and the ratio
# 0.09 — a fixture that is NOT in the e5 cone and must not be centred.)
_AMP = math.sqrt((1.0 - _COMMON_NORM**2) / _BITS)


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
    return [_COMMON + (_AMP if (bits >> i) & 1 else -_AMP) for i in range(_BITS)]


def _common_ratio(rows: list[list[float]]) -> float:
    """The guard's own quantity: ||mean|| / median(||row||), over non-zero rows."""
    import numpy as np

    arr = np.asarray(rows, dtype=np.float32)
    arr = arr[np.any(arr != 0.0, axis=1)]
    typical = float(np.median(np.linalg.norm(arr, axis=1)))
    return float(np.linalg.norm(arr.mean(axis=0))) / typical if typical > 0 else 0.0


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


# --- 6: hash fallback ------------------------------------------------------


FALLBACK_TEXTS = [
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


@pytest.mark.asyncio
async def test_a_hash_fallback_batch_is_not_centred(db, capture):
    """No model → hash-fallback vectors → centring must be skipped, on EVERY run.

    The fallback vector carries signal in ~16 dimensions and zeros in the rest,
    which is not the e5 cone. Subtracting the mean lifts that identical padding
    off zero, so `sign()` encodes the padding in every vector and unrelated
    texts correlate: measured 12 unrelated texts gave 0 edges on base and 22 on
    the centring-only version.

    The second run is the load-bearing one. A provenance guard
    (`hash_fallback_stats()` rising) only fires when vectors are COMPUTED, and
    the fallback rows are cached under `hash-fallback/<model>` — so run 2 reads
    them from cache, the counter stays 0 → 0, and the defect came back. That is
    why the guard is geometry: it measures the vectors it is about to centre.
    """
    import lifecycle.graph_miners as gm

    for text in FALLBACK_TEXTS:
        await _node(text)

    first = await gm.miner_embedding(db, "user")
    assert first["edges"] == 0, f"run 1 centred the fallback: {first}"
    assert await _edges() == [], "hash-fallback vectors were centred: unrelated texts correlate"
    assert _max_pair_jaccard(capture.binary or []) < gm._EMBED_JACCARD

    second = await gm.miner_embedding(db, "user")
    assert second["edges"] == 0, f"run 2 (cache hit) centred the fallback: {second}"
    assert await _edges() == [], "run 2 read the fallback from cache and centred it"
    assert _max_pair_jaccard(capture.binary or []) < gm._EMBED_JACCARD


# --- 6b: the guard measures the geometry -----------------------------------


def test_the_ratio_separates_e5_from_the_fallback():
    """The guard's quantity, measured on both geometries at the batch size used here.

    Measured 2026-10-26 (400 draws each, live hermes cache + `_hash_embedding`):

        n     e5 median   e5 min   fallback median   fallback max
        5       0.929      0.768        0.447            0.665
        12      0.917      0.904        0.279            0.439
        50      0.912      0.891        0.138            0.214
        1000    0.910      0.907        0.027            0.042

    The whole live batch (14 774 rows) scores 0.910. The constant sits in the
    gap: 0.7. 0.5 was rejected — the n=5 fallback tail reaches 0.665 (p99
    0.601), so at 0.5 about a fifth of small fallback batches would still be
    centred.
    """
    import random

    from lifecycle.graph_miners import _CENTRE_COMMON_RATIO
    from shared.embeddings import _hash_embedding

    rnd = random.Random(23)
    e5_like = [_raw(_pattern(rnd)) for _ in range(12)]
    fallback = [_hash_embedding(text) for text in FALLBACK_TEXTS]

    e5_ratio = _common_ratio(e5_like)
    fb_ratio = _common_ratio(fallback)

    assert 0.85 <= e5_ratio <= 0.95, f"e5-like batch ratio {e5_ratio:.3f} is not the measured ~0.91"
    assert 0.15 <= fb_ratio <= 0.45, f"fallback batch ratio {fb_ratio:.3f} is not the measured ~0.28"
    assert e5_ratio > _CENTRE_COMMON_RATIO, f"e5 would not be centred: {e5_ratio:.3f} < {_CENTRE_COMMON_RATIO}"
    assert fb_ratio < _CENTRE_COMMON_RATIO, f"fallback would be centred: {fb_ratio:.3f} >= {_CENTRE_COMMON_RATIO}"


# --- 7: zero vectors -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_zero_vector_stays_junk(db, capture, monkeypatch):
    """A zero embedding is "no signal": it must stay bit-degenerate.

    Centring a zero row turns it into `0 - mean`, ~190 bits, which is not only
    unflagged but identical for every zero row — a Jaccard 1.0 clique. The mean
    is therefore taken over the non-zero vectors only and zero rows stay zero,
    so the junk flag still fires while the real vectors are centred normally.
    """
    import random

    import lifecycle.graph_miners as gm

    rnd = random.Random(17)
    # Two nodes with the SAME meaning vector (they must still find each other
    # after centring) plus two unrelated ones, which keep the corpus mean away
    # from that vector.
    real = ["живой текст раз", "живой текст два", "посторонний текст", "ещё один посторонний"]
    shared = _pattern(rnd)
    vecs = {
        real[0]: _raw(shared),
        real[1]: _raw(shared),
        real[2]: _raw(_pattern(rnd)),
        real[3]: _raw(_pattern(rnd)),
    }
    zero_text = "текст без сигнала"
    vecs[zero_text] = [0.0] * _BITS
    _fake_embed(monkeypatch, vecs)

    ids = {}
    for text in (*real, zero_text):
        ids[text] = await _node(text)

    result = await gm.miner_embedding(db, "user")

    assert result["anomalies"] == 1, f"ровно один junk-узел: {result}"
    conn = await connection_manager.get(DB_NAME)
    tagged = await (await conn.execute("SELECT node_id FROM epi_tags WHERE tag='anomaly:junk_vector'")).fetchall()
    assert {int(r["node_id"]) for r in tagged} == {ids[zero_text]}, "флаг обязан стоять на нулевом векторе"

    rows = await _edges()
    zero_id = ids[zero_text]
    assert rows, "живые векторы обязаны по-прежнему давать рёбра"
    assert all(zero_id not in (int(r["source_id"]), int(r["target_id"])) for r in rows), "нулевой узел в рёбрах"

    assert capture.binary is not None
    zero_pos = next(i for i, (nid, _) in enumerate(await _layer_nodes()) if nid == zero_id)
    assert int.from_bytes(capture.binary[zero_pos], "big") == 0, "нулевой вектор обязан остаться нулевым"


async def _layer_nodes() -> list[Any]:
    import lifecycle.graph_miners as gm

    conn = await connection_manager.get(DB_NAME)
    return await gm._layer_nodes(conn, "user")
