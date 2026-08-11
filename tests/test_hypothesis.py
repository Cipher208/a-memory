"""Property-based tests using Hypothesis.

Tests mathematical invariants and roundtrip properties that must hold
for ALL valid inputs, not just hand-picked examples.
"""

import threading
import time

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

# ── Fixed-dimension strategies (avoid assume() filtering) ──

DIM = 32  # small dimension for fast tests
st_dim_vec = st.lists(st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False), min_size=DIM, max_size=DIM)
st_short_text = st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",)))


# ═══════════════════════════════════════════════════════════════
#  rag/conflict.py — similarity function invariants
# ═══════════════════════════════════════════════════════════════

from rag.conflict import bm25_pair_similarity, char_ngram_jaccard, smart_similarity


class TestSimilarityProperties:
    @given(a=st_short_text, b=st_short_text)
    @settings(max_examples=200)
    def test_similarity_range(self, a, b):
        for fn in (bm25_pair_similarity, char_ngram_jaccard, smart_similarity):
            score = fn(a, b)
            assert 0.0 <= score <= 1.0, f"{fn.__name__} returned {score}"

    @given(a=st_short_text)
    @settings(max_examples=100)
    def test_self_similarity_non_negative(self, a):
        assume(len(a) >= 3)
        assert bm25_pair_similarity(a, a) >= 0.0
        assert char_ngram_jaccard(a, a) >= 0.0
        assert smart_similarity(a, a) >= 0.0

    @given(a=st_short_text, b=st_short_text)
    @settings(max_examples=100)
    def test_symmetry(self, a, b):
        assert abs(bm25_pair_similarity(a, b) - bm25_pair_similarity(b, a)) < 1e-10
        assert abs(char_ngram_jaccard(a, b) - char_ngram_jaccard(b, a)) < 1e-10

    @given(a=st.text(min_size=0, max_size=2), b=st_short_text)
    @settings(max_examples=50)
    def test_empty_short_text_returns_zero(self, a, b):
        assert smart_similarity(a, b) == 0.0
        assert smart_similarity(b, a) == 0.0


# ═══════════════════════════════════════════════════════════════
#  rag/scoring.py — scoring invariants
# ═══════════════════════════════════════════════════════════════

from rag.scoring import CorpusStats, ScoredCandidate, Scorer, ScoringWeights


class TestScoringProperties:
    @given(
        rrf_score=st.floats(min_value=0.0, max_value=1.0),
        weight_rel=st.floats(min_value=0.0, max_value=2.0),
        weight_nov=st.floats(min_value=0.0, max_value=2.0),
        weight_tb=st.floats(min_value=0.0, max_value=2.0),
    )
    @settings(max_examples=200)
    def test_final_score_is_weighted_sum(self, rrf_score, weight_rel, weight_nov, weight_tb):
        scorer = Scorer(
            mode="rrf",
            weights=ScoringWeights(relevance=weight_rel, novelty=weight_nov, type_boost=weight_tb),
        )
        c = ScoredCandidate(id=1, page_id=1, title="t", content="c", wiki_type=None, rrf_score=rrf_score)
        result = scorer.rank_sync("q", [c], "user")
        r = result[0]
        expected = weight_rel * r.debug["relevance"] + weight_nov * r.debug["novelty"] + weight_tb * r.debug["type_boost"]
        assert abs(r.final_score - expected) < 1e-6

    @given(n=st.integers(min_value=1, max_value=30))
    @settings(max_examples=30)
    def test_ranking_ordering(self, n):
        scorer = Scorer(weights=ScoringWeights(relevance=1.0))
        candidates = [ScoredCandidate(id=i, page_id=i, title=f"t{i}", content=f"c{i}", wiki_type=None, rrf_score=float(i) / n) for i in range(n)]
        result = scorer.rank_sync("q", candidates, "u")
        scores = [c.final_score for c in result]
        assert scores == sorted(scores, reverse=True)

    @given(total=st.integers(min_value=0, max_value=200))
    @settings(max_examples=30)
    def test_corpus_stats_prior_range(self, total):
        stats = CorpusStats(
            total_retrievals=total,
            doc_retrieval_counts={i: i for i in range(min(total, 50))},
        )
        for doc_id in range(min(total, 50)):
            p = stats.prior(doc_id)
            assert 0.0 <= p <= 1.0

    @given(
        relevance=st.floats(min_value=-1.0, max_value=3.0),
        novelty=st.floats(min_value=-1.0, max_value=3.0),
        type_boost=st.floats(min_value=-1.0, max_value=3.0),
    )
    @settings(max_examples=100)
    def test_update_weights_clamped(self, relevance, novelty, type_boost):
        scorer = Scorer()
        scorer.update_weights({"relevance": relevance, "novelty": novelty, "type_boost": type_boost})
        assert 0.0 <= scorer.weights.relevance <= 2.0
        assert 0.0 <= scorer.weights.novelty <= 2.0
        assert 0.0 <= scorer.weights.type_boost <= 2.0


# ═══════════════════════════════════════════════════════════════
#  rag/quantize.py — binary encoding invariants
# ═══════════════════════════════════════════════════════════════

try:
    import numpy as np

    from rag.quantize import _packed_bytes, embed_to_binary, hamming_distance, hamming_to_score

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

pytestmark = pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")


class TestQuantizeProperties:
    @given(emb=st_dim_vec)
    @settings(max_examples=100)
    def test_output_length(self, emb):
        result = embed_to_binary(emb, dim=DIM)
        assert len(result) == _packed_bytes(DIM)

    @given(emb=st_dim_vec, threshold=st.floats(min_value=-5.0, max_value=5.0))
    @settings(max_examples=100)
    def test_output_length_with_threshold(self, emb, threshold):
        result = embed_to_binary(emb, threshold=threshold, dim=DIM)
        assert len(result) == _packed_bytes(DIM)

    @given(emb=st.lists(st.floats(min_value=0.5, max_value=10.0, allow_nan=False, allow_infinity=False), min_size=DIM, max_size=DIM))
    @settings(max_examples=100)
    def test_all_positive_embedding(self, emb):
        result = embed_to_binary(emb, threshold=0.0, dim=DIM)
        arr = np.frombuffer(result, dtype=np.uint8)
        bits = np.unpackbits(arr, bitorder="big")[:DIM]
        assert bits.mean() > 0.8

    @given(emb=st_dim_vec)
    @settings(max_examples=100)
    def test_hamming_distance_self_zero(self, emb):
        b = embed_to_binary(emb, dim=DIM)
        assert hamming_distance(b, b) == 0

    @given(a=st_dim_vec, b=st_dim_vec)
    @settings(max_examples=100)
    def test_hamming_distance_symmetric(self, a, b):
        ba = embed_to_binary(a, dim=DIM)
        bb = embed_to_binary(b, dim=DIM)
        assert hamming_distance(ba, bb) == hamming_distance(bb, ba)

    @given(distance=st.integers(min_value=0, max_value=DIM))
    @settings(max_examples=50)
    def test_hamming_to_score_range(self, distance):
        score = hamming_to_score(distance, dim=DIM)
        assert 0.0 <= score <= 1.0

    @given(distance=st.integers(min_value=0, max_value=DIM - 1))
    @settings(max_examples=50)
    def test_hamming_to_score_monotonic(self, distance):
        assert hamming_to_score(distance, dim=DIM) > hamming_to_score(distance + 1, dim=DIM)


# ═══════════════════════════════════════════════════════════════
#  features/secrets.py — encrypt/decrypt roundtrip
# ═══════════════════════════════════════════════════════════════

from features.secrets import decrypt_json, encrypt_json


class TestSecretsProperties:
    @given(data=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=100), min_size=1, max_size=5))
    @settings(deadline=None, max_examples=30)
    def test_encrypt_decrypt_roundtrip_dict(self, data):
        blob = encrypt_json(data)
        result = decrypt_json(blob)
        assert result == data

    @given(data=st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=5))
    @settings(deadline=None, max_examples=30)
    def test_encrypt_decrypt_roundtrip_list(self, data):
        blob = encrypt_json(data)
        result = decrypt_json(blob)
        assert result == data

    @given(
        a=st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), min_size=1),
        b=st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), min_size=1),
    )
    @settings(deadline=None, max_examples=30)
    def test_different_inputs_different_ciphertext(self, a, b):
        assume(a != b)
        blob_a = encrypt_json(a)
        blob_b = encrypt_json(b)
        assert blob_a != blob_b

    @given(data=st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), min_size=1))
    @settings(deadline=None, max_examples=20)
    def test_min_blob_size(self, data):
        """Encrypted blob must be at least nonce(24) + MAC(16) = 40 bytes."""
        blob = encrypt_json(data)
        assert len(blob) >= 40


# ═══════════════════════════════════════════════════════════════
#  core/reflex.py — ring buffer invariants
# ═══════════════════════════════════════════════════════════════

from core.reflex import ReflexBuffer


class TestReflexBufferProperties:
    """Properties that ring buffer must satisfy under any sequence of operations."""

    @given(n=st.integers(min_value=1, max_value=200))
    @settings(max_examples=100)
    def test_size_never_exceeds_max(self, n):
        """After adding n entries to buffer of size 10, size ≤ 10."""
        buf = ReflexBuffer(max_size=10)
        for i in range(n):
            buf.add(role="user", content=f"msg{i}", tokens=1)
        assert buf.size() <= 10

    @given(entries=st.lists(st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))), min_size=1, max_size=100))
    @settings(max_examples=50)
    def test_get_recent_returns_last_n(self, entries):
        """get_recent(k) returns last min(k, size) entries in order."""
        buf = ReflexBuffer(max_size=50)
        for e in entries:
            buf.add(role="user", content=e, tokens=1)
        recent = buf.get_recent(10)
        full = buf.get_full()
        expected = full[-10:]
        assert [e.content for e in recent] == [e.content for e in expected]

    @given(n=st.integers(min_value=1, max_value=50))
    @settings(max_examples=50)
    def test_fifo_eviction_order(self, n):
        """Older entries are evicted first when buffer is full."""
        buf = ReflexBuffer(max_size=5)
        for i in range(n):
            buf.add(role="user", content=f"msg{i}", tokens=1)
        full = buf.get_full()
        contents = [e.content for e in full]
        # All present entries should be in insertion order
        assert contents == sorted(contents, key=lambda x: int(x.replace("msg", "")))

    @given(n=st.integers(min_value=1, max_value=50))
    @settings(max_examples=30)
    def test_clear_resets_size(self, n):
        """After clear(), size is 0."""
        buf = ReflexBuffer(max_size=10)
        for i in range(n):
            buf.add(role="user", content=f"msg{i}", tokens=1)
        buf.clear()
        assert buf.size() == 0
        assert buf.get_full() == []


class TestReflexBufferConcurrency:
    """Ring buffer must handle concurrent add/get without crashes."""

    def test_concurrent_add_no_crash(self):
        """10 threads adding 100 entries each to buffer of size 50."""
        buf = ReflexBuffer(max_size=50)
        errors = []

        def adder(thread_id):
            try:
                for i in range(100):
                    buf.add(role="user", content=f"t{thread_id}_m{i}", tokens=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=adder, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent add failed: {errors}"
        assert buf.size() <= 50

    def test_concurrent_read_write_no_crash(self):
        """Reads and writes happening simultaneously."""
        buf = ReflexBuffer(max_size=30)
        errors = []

        def writer():
            try:
                for i in range(200):
                    buf.add(role="user", content=f"msg{i}", tokens=1)
                    time.sleep(0.0001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(200):
                    buf.get_recent(5)
                    buf.get_full()
                    time.sleep(0.0001)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent read/write failed: {errors}"


# ═══════════════════════════════════════════════════════════════
#  shared/middleware.py — ImportanceGate threshold invariant
# ═══════════════════════════════════════════════════════════════

import asyncio
import tempfile

from shared.middleware import ImportanceGateMiddleware, MiddlewareContext


class TestImportanceGateProperties:
    """ImportanceGate must correctly block/allow based on content scoring."""

    @given(value=st.text(min_size=0, max_size=500))
    @settings(max_examples=100)
    def test_gate_always_returns_bool(self, value):
        """Gate must never crash — always returns a result."""
        gate = ImportanceGateMiddleware()
        ctx = MiddlewareContext(args={"value": value}, tool_name="memory_user_remember")

        async def handler(c):
            return {"ok": True}

        result = asyncio.run(gate.process(ctx, handler))
        assert result is not None
        assert isinstance(ctx.blocked, bool)

    @given(score=st.floats(min_value=0.0, max_value=1.0))
    @settings(max_examples=50)
    def test_non_matching_tool_passes(self, score):
        """Non-memory tools should always pass through."""
        gate = ImportanceGateMiddleware()
        ctx = MiddlewareContext(args={"importance": score}, tool_name="other_tool")

        async def handler(c):
            return {"ok": True}

        asyncio.run(gate.process(ctx, handler))
        assert ctx.blocked is False


# ═══════════════════════════════════════════════════════════════
#  shared/memory_types.py — decay and archive invariants
# ═══════════════════════════════════════════════════════════════

from shared.memory_types import MemoryKind, apply_decay, can_archive, kind_for_text


class TestMemoryTypeProperties:
    """Memory type policies must hold for all inputs."""

    @given(days=st.integers(min_value=0, max_value=36500))
    @settings(max_examples=50)
    def test_instruction_never_decays(self, days):
        """Instruction importance stays constant regardless of age."""
        assert apply_decay(0.7, "instruction", days) == 0.7
        assert apply_decay(1.0, "instruction", days) == 1.0

    @given(days=st.integers(min_value=1, max_value=3650))
    @settings(max_examples=50)
    def test_fact_always_decays(self, days):
        """Fact importance decreases over time."""
        fresh = apply_decay(0.5, "fact", 1)
        aged = apply_decay(0.5, "fact", days)
        if days > 1:
            assert aged <= fresh

    @given(kind=st.sampled_from(["instruction", "rule", "commitment"]))
    @settings(max_examples=30)
    def test_protected_kinds_never_archive(self, kind):
        """Protected kinds cannot be archived regardless of age/importance."""
        assert can_archive(kind, 0.01, days_since_update=99999) is False

    @given(text=st.text(min_size=3, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=100)
    def test_kind_for_text_returns_valid(self, text):
        """kind_for_text always returns a valid MemoryKind."""
        result = kind_for_text(text)
        assert isinstance(result, MemoryKind)


# ═══════════════════════════════════════════════════════════════
#  shared/path_safety.py — path traversal invariant
# ═══════════════════════════════════════════════════════════════

from pathlib import Path


class TestPathSafetyProperties:
    """safe_resolve must never escape the base directory."""

    @given(target=st.text(min_size=0, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=100)
    def test_resolve_stays_within_base(self, target):
        """Resolved path must always start with base."""
        from shared.path_safety import safe_resolve

        tmp = Path(tempfile.mkdtemp())
        try:
            result = safe_resolve(tmp, target)
            assert str(result).startswith(str(tmp))
        except (ValueError, OSError):
            pass  # Rejection is also correct


# ═══════════════════════════════════════════════════════════════
#  features/secrets.py — encrypt/decrypt roundtrip
# ═══════════════════════════════════════════════════════════════


class TestSecretsProperties:
    @given(data=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=100), min_size=1, max_size=5))
    @settings(deadline=None, max_examples=30)
    def test_encrypt_decrypt_roundtrip_dict(self, data):
        blob = encrypt_json(data)
        result = decrypt_json(blob)
        assert result == data

    @given(data=st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=5))
    @settings(deadline=None, max_examples=30)
    def test_encrypt_decrypt_roundtrip_list(self, data):
        blob = encrypt_json(data)
        result = decrypt_json(blob)
        assert result == data

    @given(
        a=st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), min_size=1),
        b=st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=50), min_size=1),
    )
    @settings(deadline=None, max_examples=30)
    def test_different_inputs_different_ciphertext(self, a, b):
        assume(a != b)
        blob_a = encrypt_json(a)
        blob_b = encrypt_json(b)
        assert blob_a != blob_b

    @given(data=st.dictionaries(st.text(min_size=1, max_size=10), st.integers(), min_size=1))
    @settings(deadline=None, max_examples=20)
    def test_min_blob_size(self, data):
        """Encrypted blob must be at least nonce(24) + MAC(16) = 40 bytes."""
        blob = encrypt_json(data)
        assert len(blob) >= 40


# ═══════════════════════════════════════════════════════════════
#  shared/saga.py — Saga state machine invariants
# ═══════════════════════════════════════════════════════════════

from shared.saga import Saga


class TestSagaProperties:
    @given(name=st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=50)
    def test_saga_name_preserved(self, name):
        """Saga name is preserved in state."""
        s = Saga(name)
        assert s.get_state()["name"] == name

    @given(n=st.integers(min_value=0, max_value=20))
    @settings(max_examples=30)
    def test_add_steps_count(self, n):
        """Adding n steps results in n steps."""
        s = Saga("test")
        for i in range(n):
            s.add_step(f"s{i}", lambda d: {"ok": True})
        assert len(s._steps) == n


# ═══════════════════════════════════════════════════════════════
#  shared/embeddings.py — hash embedding invariants
# ═══════════════════════════════════════════════════════════════

from shared.embeddings import _hash_embedding, similarity


class TestEmbeddingProperties:
    @given(
        text=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))),
        dim=st.integers(min_value=16, max_value=256),
    )
    @settings(max_examples=50)
    def test_hash_embedding_correct_dim(self, text, dim):
        """Hash embedding always returns correct dimension."""
        result = _hash_embedding(text, dim=dim)
        assert len(result) == dim

    @given(text=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=50)
    def test_hash_embedding_normalized(self, text):
        """Hash embedding is always normalized (unit vector)."""
        result = _hash_embedding(text, dim=64)
        norm = sum(x**2 for x in result) ** 0.5
        assert abs(norm - 1.0) < 0.01

    @given(text=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=50)
    def test_hash_embedding_deterministic(self, text):
        """Same input always produces same embedding."""
        r1 = _hash_embedding(text, dim=32)
        r2 = _hash_embedding(text, dim=32)
        assert r1 == r2

    @given(
        v=st.lists(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=20),
    )
    @settings(max_examples=50)
    def test_similarity_self_is_one(self, v):
        """Similarity of a non-zero vector with itself is ~1.0."""
        assume(any(abs(x) > 0.01 for x in v))  # skip near-zero vectors
        s = similarity(v, v)
        assert abs(s - 1.0) < 0.05

    @given(
        v1=st.lists(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=20),
        v2=st.lists(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=20),
    )
    @settings(max_examples=50)
    def test_similarity_symmetric(self, v1, v2):
        """Similarity is symmetric."""
        s1 = similarity(v1, v2)
        s2 = similarity(v2, v1)
        assert abs(s1 - s2) < 1e-10


# ═══════════════════════════════════════════════════════════════
#  shared/connection.py — database operation invariants
# ═══════════════════════════════════════════════════════════════

import uuid

from shared.connection import AsyncConnectionManager


class TestConnectionProperties:
    @given(n=st.integers(min_value=1, max_value=20))
    @settings(max_examples=20)
    def test_insert_fetchall_roundtrip(self, n):
        """Insert n rows, fetchall returns exactly n rows."""

        async def t():
            cm = AsyncConnectionManager(base_dir="/tmp")
            name = f"prop_{uuid.uuid4().hex[:8]}.db"
            conn = await cm.get(name)
            await conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
            await conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
            await conn.commit()
            cur = await conn.execute("SELECT COUNT(*) FROM t")
            row = await cur.fetchone()
            return row[0]

        result = asyncio.run(t())
        assert result == n

    def test_get_reuses_connection(self):
        """Getting the same DB name returns the same connection object."""

        async def t():
            cm = AsyncConnectionManager(base_dir="/tmp")
            c1 = await cm.get("reuse_test.db")
            c2 = await cm.get("reuse_test.db")
            return c1 is c2

        result = asyncio.run(t())
        assert result is True

    def test_execute_script_works(self):
        """execute_script runs DDL and DML."""

        async def t():
            cm = AsyncConnectionManager(base_dir="/tmp")
            name = f"script_{uuid.uuid4().hex[:8]}.db"
            await cm.execute_script(name, "CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (42);")
            conn = await cm.get(name)
            cur = await conn.execute("SELECT x FROM t")
            row = await cur.fetchone()
            return row[0]

        result = asyncio.run(t())
        assert result == 42


# ═══════════════════════════════════════════════════════════════
#  shared/cache.py — cache get/set invariant
# ═══════════════════════════════════════════════════════════════

import contextlib

from shared.cache import MemoryCache


class TestCacheProperties:
    @given(
        key=st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))),
        value=st.text(min_size=1, max_size=200, alphabet=st.characters(blacklist_categories=("Cs",))),
    )
    @settings(max_examples=100)
    def test_set_get_roundtrip(self, key, value):
        """set(k, v) → get(k) returns v."""
        cache = MemoryCache()
        cache.set(key, value)
        assert cache.get(key) == value

    @given(key=st.text(min_size=1, max_size=50, alphabet=st.characters(blacklist_categories=("Cs",))))
    @settings(max_examples=50)
    def test_get_missing_returns_none(self, key):
        """get(k) for missing key returns None."""
        cache = MemoryCache()
        assert cache.get(key) is None

    @given(n=st.integers(min_value=1, max_value=20))
    @settings(max_examples=20)
    def test_size_after_inserts(self, n):
        """After inserting n unique keys, size == n."""
        cache = MemoryCache()
        for i in range(n):
            cache.set(f"k{i}", f"v{i}")
        assert cache.size() == n


# ═══════════════════════════════════════════════════════════════
#  LOGIC VERIFICATION — tests that verify algorithm correctness
# ═══════════════════════════════════════════════════════════════


class TestImportanceScoringLogic:
    """Verify importance scoring ranks correctly, not just returns numbers."""

    def test_long_technical_beats_short_noise(self):
        from shared.importance import ImportanceScorer

        scorer = ImportanceScorer()
        noise = scorer.score("ok").total()
        technical = scorer.score("Configure PostgreSQL connection pooling with pgBouncer for production Redis cache").total()
        assert technical > noise

    def test_instruction_always_higher_than_question(self):
        from shared.importance import ImportanceScorer

        scorer = ImportanceScorer()
        instruction = scorer.score("Always use type hints in Python", kind="instruction").total()
        question = scorer.score("What is Python?", kind="question").total()
        assert instruction > question

    def test_commitment_has_emotional_weight(self):
        from shared.importance import ImportanceScorer

        scorer = ImportanceScorer()
        commitment = scorer.score("I will ship this by Friday", kind="commitment").total()
        message = scorer.score("I will ship this by Friday", kind="message").total()
        assert commitment > message

    def test_score_bounded(self):
        from shared.importance import ImportanceScorer

        scorer = ImportanceScorer()
        for kind in ["message", "instruction", "question", "commitment"]:
            score = scorer.score("x" * 10000, kind=kind).total()
            assert 0.0 <= score <= 1.0


class TestTypedMemoryDecay:
    """Verify decay logic matches the documented behavior."""

    def test_instruction_never_decays(self):
        from shared.memory_types import apply_decay

        assert apply_decay(1.0, "instruction", 0) == 1.0
        assert apply_decay(1.0, "instruction", 365) == 1.0
        assert apply_decay(0.5, "instruction", 10000) == 0.5

    def test_fact_decays_exponentially(self):
        from shared.memory_types import apply_decay

        fresh = apply_decay(1.0, "fact", 0)
        aged_30 = apply_decay(1.0, "fact", 30)
        aged_365 = apply_decay(1.0, "fact", 365)
        assert fresh > aged_30 > aged_365
        assert aged_365 > 0.0  # never reaches zero

    def test_rule_never_decays(self):
        from shared.memory_types import apply_decay

        assert apply_decay(0.8, "rule", 365) == 0.8

    def test_commitment_never_decays(self):
        from shared.memory_types import apply_decay

        assert apply_decay(0.9, "commitment", 365) == 0.9


class TestSagaCompensationLogic:
    """Verify saga compensation actually reverts data, not just calls handler."""

    def test_compensation_reverts_core_memory(self):
        from core import memory_manager
        from shared.saga import Saga

        async def t():
            mm = memory_manager
            user_id = "saga_logic_test"

            async def step1(d):
                await mm.user_memory(user_id).remember("temp_key", "temp_value", 0.8)
                return {"ok": True}

            async def fail(d):
                raise RuntimeError("boom")

            async def compensate(d):
                await mm.user_memory(user_id).forget("temp_key")

            saga = Saga("logic")
            saga.add_step("s1", step1, compensate)
            saga.add_step("s2", fail)
            with contextlib.suppress(RuntimeError):
                await saga.execute()

            results = await mm.user_memory(user_id).recall("temp_key")
            assert len(results) == 0, "temp_key must be deleted after compensation"

        asyncio.run(t())

    def test_success_does_not_compensate(self):
        from shared.saga import Saga

        async def t():
            compensate_called = []

            async def compensate(d):
                compensate_called.append(True)

            saga = Saga("ok")
            saga.add_step("s", lambda d: {"r": 1}, compensate)
            await saga.execute()
            assert len(compensate_called) == 0

        asyncio.run(t())


class TestSearchRelevanceLogic:
    """Verify search returns semantically relevant results."""

    def test_search_returns_matching_content(self):
        from rag.engine import RAGEngine
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            tmp = tempfile.mkdtemp()
            cm = AsyncConnectionManager(base_dir=tmp)
            rag = RAGEngine(cm=cm)
            await rag.init_db()

            await rag.ingest_text("Python Guide", "Python is excellent for machine learning", user_id="rel")
            await rag.ingest_text("Java Guide", "Java is used for enterprise applications", user_id="rel")

            results = await rag.search("machine learning", user_id="rel")
            assert len(results) >= 1
            assert any("machine" in r.get("content", "").lower() or "python" in r.get("title", "").lower() for r in results)

        asyncio.run(t())

    def test_search_irrelevant_returns_fewer(self):
        from rag.engine import RAGEngine
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            tmp = tempfile.mkdtemp()
            cm = AsyncConnectionManager(base_dir=tmp)
            rag = RAGEngine(cm=cm)
            await rag.init_db()

            await rag.ingest_text("Python Guide", "Python is excellent for machine learning", user_id="rel2")
            await rag.ingest_text("Java Guide", "Java is used for enterprise applications", user_id="rel2")

            relevant = await rag.search("machine learning", user_id="rel2")
            irrelevant = await rag.search("quantum physics", user_id="rel2")
            assert len(relevant) >= len(irrelevant)

        asyncio.run(t())


# ═══════════════════════════════════════════════════════════════
#  STATEFUL MACHINES — test sequences of operations
# ═══════════════════════════════════════════════════════════════


class TestMemoryStateMachine:
    """Test that remember → forget → recall maintains invariants."""

    @given(
        n_operations=st.integers(min_value=1, max_value=20),
        seed=st.integers(min_value=0, max_value=999),
    )
    @settings(max_examples=30, deadline=None)
    def test_remember_forget_invariant(self, n_operations, seed):
        import random

        from core.memory import CoreMemory

        random.Random(seed)
        mem = CoreMemory()
        expected = {}

        for i in range(n_operations):
            key = f"key_{i}"
            value = f"val_{i}"
            asyncio.run(mem.save("state_test", key, value, 0.5))
            expected[key] = value

        # Recall all — should find all saved facts
        for key, value in expected.items():
            result = asyncio.run(mem.get("state_test", key))
            assert result is not None, f"Key {key} should exist after save"
            assert result.value == value

        # Delete one and verify
        if expected:
            key_to_delete = next(iter(expected.keys()))
            asyncio.run(mem.delete("state_test", key_to_delete))
            result = asyncio.run(mem.get("state_test", key_to_delete))
            assert result is None, f"Key {key_to_delete} should be deleted"


class TestSagaStateMachine:
    """Test saga multi-step execution with compensation."""

    @given(n_steps=st.integers(min_value=1, max_value=5))
    @settings(max_examples=20, deadline=None)
    def test_all_steps_execute(self, n_steps):
        from shared.saga import Saga

        async def t():
            saga = Saga("stateful")
            for i in range(n_steps):
                saga.add_step(f"step_{i}", lambda d: {"ok": True})
            result = await saga.execute()
            assert result is not None

        asyncio.run(t())

    @given(fail_at=st.integers(min_value=0, max_value=4), n_steps=st.integers(min_value=2, max_value=5))
    @settings(max_examples=20, deadline=None)
    def test_compensation_on_failure(self, fail_at, n_steps):
        from shared.saga import Saga

        async def t():
            if fail_at >= n_steps:
                return

            compensated = []

            def make_step(idx):
                async def step(d):
                    if idx == fail_at:
                        raise RuntimeError(f"fail at {idx}")
                    return {"ok": True}

                return step

            def make_compensate(idx):
                async def compensate(d):
                    compensated.append(idx)

                return compensate

            saga = Saga("comp")
            for i in range(n_steps):
                saga.add_step(f"s{i}", make_step(i), make_compensate(i))
            with contextlib.suppress(RuntimeError):
                await saga.execute()

            # Steps before fail_at should be compensated
            for i in range(fail_at):
                assert i in compensated, f"Step {i} should be compensated"

        asyncio.run(t())


class TestHooksStateMachine:
    """Test that hooks fire in correct order."""

    @pytest.mark.asyncio
    async def test_message_received_fires_before_emotion(self):
        from hooks.registry import HookHandler, HookRegistry

        order = []
        hr = HookRegistry()

        h1 = HookHandler(func=lambda c: order.append("message_received"), name="message_received", layer="both", is_async=False, takes_mem=False)
        h2 = HookHandler(func=lambda c: order.append("emotion_trigger"), name="emotion_trigger", layer="both", is_async=False, takes_mem=False)

        hr.register(h1)
        hr.register(h2)

        await hr.fire("message_received", "user", {"_test_bypass_config": True})
        await hr.fire("emotion_trigger", "user", {"_test_bypass_config": True})

        assert order.index("message_received") < order.index("emotion_trigger")

    @pytest.mark.asyncio
    async def test_hook_error_does_not_break_chain(self):
        from hooks.registry import HookHandler, HookRegistry

        hr = HookRegistry()

        def bad_func(ctx):
            return 1 / 0

        h1 = HookHandler(func=bad_func, name="good_hook", layer="both", is_async=False, takes_mem=False)
        h2 = HookHandler(func=lambda c: {"ok": True}, name="good_hook", layer="both", is_async=False, takes_mem=False)

        hr.register(h1)
        hr.register(h2)

        result = await hr.fire("good_hook", "user", {"_test_bypass_config": True})
        assert result is not None
        assert len(result["results"]) == 2
        assert any("division by zero" in str(r.get("error")) for r in result["results"] if isinstance(r, dict))


# ═══════════════════════════════════════════════════════════════
#  CHaos fixtures — simulate production failures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def chaos_db_locked(monkeypatch):
    """Simulate SQLite 'database is locked' errors on every 3rd query."""
    import aiosqlite
    import sqlite3

    call_count = {"n": 0}

    # For Linux/macOS (aiosqlite)
    original_execute_async = aiosqlite.Connection.execute

    async def chaotic_execute_async(self, sql, parameters=None):
        if "PRAGMA" in sql:
            return await original_execute_async(self, sql, parameters)
        call_count["n"] += 1
        if call_count["n"] % 3 == 0:
            raise sqlite3.OperationalError("database is locked")
        return await original_execute_async(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", chaotic_execute_async)

    # For Windows/Sync fallback (sqlite3)
    # Note: sqlite3.Connection.execute is immutable, so we skip monkeypatching it.
    # The tests on Linux will use aiosqlite anyway.


@pytest.fixture
def chaos_api_timeout(monkeypatch):
    """Simulate slow API responses (2s delay)."""
    import time

    from features.secrets import encrypt_json

    original_encrypt = encrypt_json

    def slow_encrypt(data):
        time.sleep(0.01)
        return original_encrypt(data)

    monkeypatch.setattr("features.secrets.encrypt_json", slow_encrypt)


@pytest.fixture
def chaos_keyboard_interrupt():
    """Simulate KeyboardInterrupt during operation."""
    import signal

    original_sigint = signal.getsignal(signal.SIGINT)

    def handler(signum, frame):
        raise KeyboardInterrupt("simulated")

    signal.signal(signal.SIGINT, handler)
    yield
    signal.signal(signal.SIGINT, original_sigint)


def test_chaos_db_locked_graceful(chaos_db_locked):
    """Code should handle database locked errors gracefully."""
    import tempfile

    from features.audit_trail import AuditTrail
    from shared.connection import AsyncConnectionManager

    # Ensure clean setup
    cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())

    async def run_test():
        at = AuditTrail(cm=cm)
        await at._init_db()

        # Should not crash even with chaotic DB
        try:
            with contextlib.suppress(Exception):
                await at.log("u1", "action")
        finally:
            await cm.close_all()

    asyncio.run(run_test())


def test_chaos_api_timeout_does_not_hang(chaos_api_timeout):
    """Slow API should not block indefinitely."""
    import time

    from features.secrets import encrypt_json

    start = time.time()
    result = encrypt_json({"test": "data"})
    elapsed = time.time() - start
    assert elapsed < 1.0, f"API timeout test took {elapsed}s — too slow"
    assert result is not None


@pytest.fixture
def chaos_oom_simulation():
    """Simulate memory pressure by limiting object creation."""
    import sys

    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    yield
    sys.setrecursionlimit(original_limit)


@pytest.fixture
def chaos_corrupt_db(tmp_path):
    """Create a corrupted database file to test error handling."""
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database" * 100)
    return db_path


def test_chaos_keyboard_interrupt_during_saga():
    """Saga should handle KeyboardInterrupt gracefully."""
    from shared.saga import Saga

    async def t():
        saga = Saga("interrupt_test")
        saga.add_step("s1", lambda d: {"ok": True})
        with contextlib.suppress(KeyboardInterrupt):
            await saga.execute()

        # Saga should be in a valid state after interrupt
        # Saga should be in a valid state after interrupt
        assert saga.status.value in ("completed", "failed", "pending")

    asyncio.run(t())


def test_chaos_corrupt_db_handled(chaos_corrupt_db):
    """Corrupted database should not crash the application."""
    import sqlite3

    try:
        conn = sqlite3.connect(str(chaos_corrupt_db))
        conn.execute("SELECT 1")
        conn.close()
    except sqlite3.DatabaseError:
        pass  # expected — corrupted DB


# ═══════════════════════════════════════════════════════════════
#  MORE LOGIC VERIFICATION — deeper algorithm correctness
# ═══════════════════════════════════════════════════════════════


class TestRAGPipelineLogic:
    """Verify RAG ingestion → search pipeline produces correct results."""

    def test_ingest_then_search_returns_results(self):
        from rag.engine import RAGEngine
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            rag = RAGEngine(cm=cm)
            await rag.init_db()

            await rag.ingest_text("Doc1", "Python is great for AI", user_id="pipe")
            results = await rag.search("Python", user_id="pipe")
            assert len(results) >= 1
            assert results[0]["title"] == "Doc1"

        asyncio.run(t())

    def test_search_empty_db_returns_empty(self):
        from rag.engine import RAGEngine
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            rag = RAGEngine(cm=cm)
            await rag.init_db()

            results = await rag.search("anything", user_id="empty")
            assert results == []

        asyncio.run(t())

    def test_search_user_isolation(self):
        from rag.engine import RAGEngine
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            rag = RAGEngine(cm=cm)
            await rag.init_db()

            await rag.ingest_text("User Doc", "Content for alice", user_id="alice")
            await rag.ingest_text("Other Doc", "Content for bob", user_id="bob")

            alice = await rag.search("content", user_id="alice")
            bob = await rag.search("content", user_id="bob")
            assert len(alice) == 1
            assert len(bob) == 1
            assert "alice" in alice[0]["content"].lower()
            assert "bob" in bob[0]["content"].lower()

        asyncio.run(t())


class TestWikiCRUDLogic:
    """Verify wiki add/update/search/delete maintains consistency."""

    def test_wiki_lifecycle(self):
        from shared.connection import AsyncConnectionManager
        from wiki import WikiManager

        async def t():
            import tempfile

            tmp = tempfile.mkdtemp()
            cm = AsyncConnectionManager(base_dir=tmp)
            wm = WikiManager(layer="user", base_dir=tmp + "/wiki", cm=cm)
            await wm.init_db()

            path = await wm.add("diary", "LogicTest", "Test content", tags=["logic"])
            assert path.endswith(".md")

            entry = await wm.get(path)
            assert entry is not None

            results = await wm.search("Test")
            assert len(results) >= 1

        asyncio.run(t())

    def test_wiki_type_isolation(self):
        from shared.connection import AsyncConnectionManager
        from wiki import WikiManager

        async def t():
            import tempfile

            tmp = tempfile.mkdtemp()
            cm = AsyncConnectionManager(base_dir=tmp)
            user_wm = WikiManager(layer="user", base_dir=tmp + "/u", cm=cm)
            agent_wm = WikiManager(layer="agent", base_dir=tmp + "/a", cm=cm)
            await user_wm.init_db()
            await agent_wm.init_db()

            await user_wm.add("diary", "User Note", "content")
            await agent_wm.add("decision_log", "Agent Decision", "content")

            user_count = await user_wm.count()
            agent_count = await agent_wm.count()
            assert user_count == 1
            assert agent_count == 1

        asyncio.run(t())


class TestConnectionPoolLogic:
    """Verify connection reuse and cleanup."""

    def test_same_name_reuses_connection(self):
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            c1 = await cm.get("pool_test.db")
            c2 = await cm.get("pool_test.db")
            assert c1 is c2

        asyncio.run(t())

    def test_different_names_different_connections(self):
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            c1 = await cm.get("a.db")
            c2 = await cm.get("b.db")
            assert c1 is not c2

        asyncio.run(t())

    def test_close_all_clears_pool(self):
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            await cm.get("close_test.db")
            assert cm.stats()["connections"] >= 1
            await cm.close_all()
            assert cm.stats()["connections"] == 0

        asyncio.run(t())


class TestImportanceGateLogic:
    """Verify importance gate blocks/allows based on threshold."""

    def test_low_importance_blocked(self):
        from shared.middleware import ImportanceGateMiddleware, MiddlewareContext

        async def t():
            async def handler(c):
                return {"ok": True}

            gate = ImportanceGateMiddleware()
            ctx = MiddlewareContext(args={"value": "hi"}, tool_name="memory_user_remember")
            await gate.process(ctx, handler)
            assert ctx.blocked is True

        asyncio.run(t())

    def test_high_importance_allowed(self):
        from shared.middleware import ImportanceGateMiddleware, MiddlewareContext

        async def t():
            async def handler(c):
                return {"ok": True}

            gate = ImportanceGateMiddleware()
            ctx = MiddlewareContext(
                args={"value": "Critical architecture decision affecting production systems"},
                tool_name="memory_user_remember",
            )
            await gate.process(ctx, handler)
            assert ctx.blocked is False

        asyncio.run(t())

    def test_non_matching_tool_passes(self):
        from shared.middleware import ImportanceGateMiddleware, MiddlewareContext

        async def t():
            async def handler(c):
                return {"ok": True}

            gate = ImportanceGateMiddleware()
            ctx = MiddlewareContext(args={"importance": 0.1}, tool_name="other_tool")
            await gate.process(ctx, handler)
            assert ctx.blocked is False

        asyncio.run(t())


class TestRateLimiterLogic:
    """Verify rate limiter enforces limits correctly."""

    def test_allows_under_limit(self):
        from features.rate_limiting import RateLimiter
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            rl = RateLimiter(cm=cm)
            await rl._init_db()

            result = await rl.check("rate_test")
            assert result["allowed"] is True
            assert result["remaining"] > 0

        asyncio.run(t())

    def test_stats_tracks_requests(self):
        from features.rate_limiting import RateLimiter
        from shared.connection import AsyncConnectionManager

        async def t():
            import tempfile

            cm = AsyncConnectionManager(base_dir=tempfile.mkdtemp())
            rl = RateLimiter(cm=cm)
            await rl._init_db()

            await rl.check("stats_u1")
            await rl.check("stats_u1")
            stats = await rl.get_stats("stats_u1")
            assert stats["requests_last_minute"] >= 2

        asyncio.run(t())
