"""F2b: bounded candidate set for the embedding miner.

The reference below is the CURRENT (base e52b43a) algorithm, copied verbatim:
`_reference_packed_words` + `_reference_candidate_order` are `_packed_words` and
`_semantic_candidate_order` as they stand before this package, and
`_reference_accepted` replays the miner's budget loop over that full pair list.

Why the reference lives here: the new implementation is allowed to drop pairs
that the greedy could never reach, and it must not change WHICH edges the greedy
accepts. A fixture where the bound is not binding (every node has fewer than
`_EMBED_TOPK * CANDIDATE_FACTOR` partners) is therefore an exact oracle: any
difference is a semantic change, not a rounding of the candidate list.

Round 2 added centring before quantisation, so the fixtures now model the e5
geometry (a 0.9 common component plus the ±0.5 meaning pattern) and the oracle
centres with the same float32 arithmetic the miner uses. Without that, centring
would collapse these synthetic corpora to zero bits and the oracle would compare
two empty sets.

The tests drive `miner_embedding` (the door), not the candidate helper alone.
"""

import math
import random
import struct
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

T = 1_700_000_000.0
_BITS = 384


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


def _vec(bits: int) -> list[float]:
    return [0.5 if (bits >> i) & 1 else -0.5 for i in range(_BITS)]


def _all_on() -> int:
    return (1 << _BITS) - 1


# The miner centres before quantising (F2b round 2). Every fixture therefore
# models the real e5 geometry: a large shared component plus the ±0.5 pattern
# that carries the meaning. Centring cancels the shared part exactly, so the
# oracle sees the intended bit patterns rather than a degenerate corpus.
_COMMON = 0.9 / math.sqrt(_BITS)


def _raw(bits: int) -> list[float]:
    """Vector whose centred form is the ±0.5 pattern `bits` (common part added)."""
    return [_COMMON + (0.5 if (bits >> i) & 1 else -0.5) for i in range(_BITS)]


def _centred_binary(vecs: list[list[float]]) -> list[bytes]:
    """Exactly what `miner_embedding` now does: centre, then binarise."""
    import numpy as np

    from rag.quantize import embed_to_binary

    arr = np.asarray(vecs, dtype=np.float32)
    return [embed_to_binary(v, dim=len(v)) for v in arr - arr.mean(axis=0)]


def _balanced(rnd: Any, ones: int = _BITS // 2) -> int:
    """Random bit pattern with exactly `ones` bits, so the set mean stays ~0."""
    dims = list(range(_BITS))
    rnd.shuffle(dims)
    bits = 0
    for d in dims[:ones]:
        bits |= 1 << d
    return bits


def _cluster_bits(rnd: Any, centers: list[int], flips: int) -> int:
    bits = centers[rnd.randrange(len(centers))]
    for _ in range(rnd.randrange(0, flips + 1)):
        bits ^= 1 << rnd.randrange(_BITS)
    return bits


# --- reference: the base algorithm, verbatim -------------------------------


def _reference_packed_words(binary: list[bytes]) -> Any | None:
    """Bit vectors as uint64 words, so the scan is 6 planes wide, not 48 bytes."""
    import numpy as np

    if not binary or not hasattr(np, "bitwise_count"):
        return None
    n = len(binary)
    width = len(binary[0])
    if width == 0 or any(len(b) != width for b in binary):
        return None
    packed = np.frombuffer(b"".join(binary), dtype=np.uint8).reshape(n, width)
    if width % 8:
        packed = np.pad(packed, ((0, 0), (0, 8 - width % 8)))
    return np.ascontiguousarray(packed).view(np.uint64)


def _reference_candidate_order(binary: list[bytes], threshold: float) -> tuple[Any, Any] | None:
    """Verbatim copy of `_semantic_candidate_order` at base e52b43a."""
    import numpy as np

    words = _reference_packed_words(binary)
    if words is None:
        return None
    n = len(binary)
    counts = np.bitwise_count(words).sum(axis=1).astype(np.int32)
    active = np.flatnonzero(counts > 0)
    cols = np.arange(n)

    row_parts: list[Any] = []
    col_parts: list[Any] = []
    val_parts: list[Any] = []
    for start in range(0, len(active), 512):
        rows = active[start : start + 512]
        block = words[rows]
        inter = np.zeros((len(rows), n), dtype=np.int32)
        for word in range(words.shape[1]):
            inter += np.bitwise_count(block[:, word][:, None] & words[None, :, word])
        union = counts[rows][:, None] + counts[None, :] - inter
        jac = np.divide(
            inter,
            np.maximum(union, 1),
            out=np.zeros(inter.shape, dtype=np.float64),
            where=union > 0,
        )
        bi, bj = np.nonzero((jac >= threshold) & (cols[None, :] > rows[:, None]))
        row_parts.append(rows[bi].astype(np.int32))
        col_parts.append(bj.astype(np.int32))
        val_parts.append(jac[bi, bj])

    if not row_parts:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)
    values = np.concatenate(val_parts)
    ii = np.concatenate(row_parts)
    jj = np.concatenate(col_parts)
    order = np.lexsort((-jj, -ii, -values))
    return ii[order], jj[order]


def _reference_accepted(binary: list[bytes], threshold: float = 0.7, topk: int = 15) -> set[tuple[int, int]]:
    """The miner's budget loop over the full pair list (crosscheck off)."""
    pairs = _reference_candidate_order(binary, threshold)
    assert pairs is not None
    remaining = [topk] * len(binary)
    accepted: set[tuple[int, int]] = set()
    for i, j in zip(pairs[0].tolist(), pairs[1].tolist(), strict=True):
        if remaining[i] <= 0 or remaining[j] <= 0:
            continue
        remaining[i] -= 1
        remaining[j] -= 1
        accepted.add((i, j))
    return accepted


# --- tests -----------------------------------------------------------------


def test_candidates_are_bounded():
    """Each node keeps at most TOPK * FACTOR partners, so the union is O(n * M).

    Every pair qualifies on this fixture (600 identical vectors = 179 700
    pairs), so an unbounded candidate structure is observable: it exceeds n * M.
    n must be well above 2 * M, otherwise the per-node lists still cover every
    pair and the bound cannot bind.
    """
    import lifecycle.graph_miners as gm

    n = 600
    m = gm._EMBED_TOPK * gm._CANDIDATE_FACTOR
    binary = [b"\xff" * 48] * n
    ii, _jj = gm._semantic_candidate_order(binary, 0.7)

    assert ii.size > 0, "fixture must produce candidates"
    assert ii.size <= n * m, f"candidate structure is not bounded by n*M={n * m}: {ii.size} pairs"
    assert ii.size < n * (n - 1) // 2, "no pair was dropped: the per-node bound is inactive"


@pytest.mark.asyncio
async def test_greedy_result_matches_the_full_pair_reference(db):
    """Dense fixture: the accepted edge set equals the full-pair reference."""
    import lifecycle.graph_miners as gm

    rnd = random.Random(7)
    centers = [_balanced(rnd) for _ in range(4)]
    vecs: dict[str, list[float]] = {}
    for idx in range(30):
        text = f"плотный узел {idx}"
        vecs[text] = _raw(_cluster_bits(rnd, centers, 20))

    for text in vecs:
        await _node(text)
    for text, vec in vecs.items():
        await _seed_vector(text, vec)

    conn = await connection_manager.get(DB_NAME)
    nodes = await gm._layer_nodes(conn, "user")
    binary = _centred_binary([vecs[content] for _, content in nodes])
    expected_idx = _reference_accepted(binary)
    expected = {(nodes[i][0], nodes[j][0]) for i, j in expected_idx}
    assert expected_idx, "the fixture must produce pairs above the threshold"

    result = await gm.miner_embedding(db, "user")
    got = {(int(r["source_id"]), int(r["target_id"])) for r in await _edges()}

    assert result["edges"] == len(got)
    assert got == expected, f"divergence from reference: +{got - expected} -{expected - got}"


# A set system where the greedy's accepted set really depends on the `j`
# tie-break: every node shares a 250-bit core and adds `u` unique bits, so
# pairs with equal `u` tie on Jaccard, while different `u` values order above
# them and leave the tied partners with unequal budgets. Shuffled so the tie
# class is not index-symmetric (found by search over seeds; dropping `j desc`
# moves 9 edges).
#
# Centring removes any shared component, so each pattern is paired with its
# bitwise complement: the set mean is then exactly zero and centring is a
# no-op, leaving the tie structure intact. Without the complements the core
# would be cancelled by the mean and every vector would collapse to its unique
# bits, where all pairs tie at 0.
_TIE_CORE = 250
_TIE_U = [2, 12, 8, 12, 6, 10, 2, 10, 8, 4, 2, 12, 2, 10, 2, 2, 12, 6]


def _tie_bits() -> list[int]:
    nxt = _TIE_CORE
    out: list[int] = []
    for u in _TIE_U:
        bits = (1 << _TIE_CORE) - 1
        for _ in range(u):
            bits |= 1 << nxt
            nxt += 1
        out.append(bits)
    return out


@pytest.mark.asyncio
async def test_order_is_preserved_under_ties(db):
    """Equal Jaccard values: the accepted set follows (i desc, j desc)."""
    import lifecycle.graph_miners as gm

    patterns = _tie_bits()
    patterns += [bits ^ _all_on() for bits in patterns]
    vecs = {f"ничейный узел {idx}": _raw(bits) for idx, bits in enumerate(patterns)}
    for text in vecs:
        await _node(text)
    for text, vec in vecs.items():
        await _seed_vector(text, vec)

    conn = await connection_manager.get(DB_NAME)
    nodes = await gm._layer_nodes(conn, "user")
    binary = _centred_binary([vecs[content] for _, content in nodes])
    expected_idx = _reference_accepted(binary)
    assert expected_idx, "the fixture must produce pairs above the threshold"
    expected = {(nodes[i][0], nodes[j][0]) for i, j in expected_idx}

    await gm.miner_embedding(db, "user")
    got = {(int(r["source_id"]), int(r["target_id"])) for r in await _edges()}

    assert got == expected, f"tie order diverged: +{got - expected} -{expected - got}"


@pytest.mark.asyncio
async def test_a_node_with_no_candidates_is_untouched(db):
    """Zero-bit vectors share nothing and must stay out of every edge."""
    import lifecycle.graph_miners as gm

    zero_texts = [f"пустой вектор {idx}" for idx in range(3)]
    real_texts = [f"живой узел {idx}" for idx in range(3)]
    for text in zero_texts:
        await _node(text)
        await _seed_vector(text, [0.0] * _BITS)
    for text in real_texts:
        await _node(text)
        await _seed_vector(text, _raw(_all_on()))

    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute("SELECT node_id FROM epi_nodes WHERE content LIKE 'пустой вектор%'")).fetchall()
    zero_ids = {int(r["node_id"]) for r in rows}

    result = await gm.miner_embedding(db, "user")
    assert result["anomalies"] == len(zero_ids)

    for r in await _edges():
        assert int(r["source_id"]) not in zero_ids
        assert int(r["target_id"]) not in zero_ids

    ii, _jj = gm._semantic_candidate_order([b"\x00" * 48] * 4, 0.7)
    assert ii.size == 0, "a zero vector must not become a candidate"


@pytest.mark.asyncio
async def test_budget_is_still_per_node_topk(db):
    """Bounding the candidates must not raise the per-node top-k budget."""
    import lifecycle.graph_miners as gm

    rnd = random.Random(3)
    centers = [_balanced(rnd) for _ in range(2)]
    vecs: dict[str, list[float]] = {}
    for idx in range(40):
        text = f"бюджетный узел {idx}"
        vecs[text] = _raw(_cluster_bits(rnd, centers, 15))
    for text in vecs:
        await _node(text)
    for text, vec in vecs.items():
        await _seed_vector(text, vec)

    await gm.miner_embedding(db, "user")

    degree: Counter[int] = Counter()
    for r in await _edges():
        degree[int(r["source_id"])] += 1
        degree[int(r["target_id"])] += 1
    assert degree, "fixture must produce edges"
    assert max(degree.values()) <= gm._EMBED_TOPK, f"top-k budget broken: {max(degree.values())}"
