"""Tests for supervised threshold training."""

import numpy as np
import pytest

from rag.engine import RAGEngine
from rag.quantize import (
    binary_from_threshold_array,
    load_thresholds,
    save_thresholds,
    supervised_threshold,
    train_supervised_thresholds,
)
from shared.connection import AsyncConnectionManager


@pytest.fixture
async def rag(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    r = RAGEngine(cm=cm, layer="user", binary_dim=8)
    await r.init_db()
    return r


def _make_pairs(n=10, dim=8, seed=42):
    """Create positive pairs: similar embeddings with small noise."""
    rng = np.random.RandomState(seed)
    base = rng.randn(n, dim).astype(np.float32)
    noise = rng.randn(n, dim).astype(np.float32) * 0.1
    return list(zip(base.tolist(), (base + noise).tolist()))


def _make_neg_pairs(n=5, dim=8, seed=99):
    """Create negative pairs: dissimilar embeddings."""
    rng = np.random.RandomState(seed)
    a = rng.randn(n, dim).astype(np.float32)
    b = rng.randn(n, dim).astype(np.float32) * 5.0
    return list(zip(a.tolist(), b.tolist()))


class TestSupervisedThresholdExisting:
    def test_returns_correct_shape(self):
        pairs = _make_pairs(n=5, dim=4)
        t = supervised_threshold(pairs, dim=4)
        assert t.shape == (4,)

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            supervised_threshold([])


class TestTrainSupervisedThresholds:
    def test_pos_only(self):
        pairs = _make_pairs(n=10, dim=8)
        t = train_supervised_thresholds(pos_pairs=pairs, n_candidates=20, dim=8)
        assert t.shape == (8,)
        assert np.all(np.isfinite(t))

    def test_pos_and_neg(self):
        pos = _make_pairs(n=10, dim=8)
        neg = _make_neg_pairs(n=5, dim=8)
        t = train_supervised_thresholds(pos_pairs=pos, neg_pairs=neg, n_candidates=20, dim=8)
        assert t.shape == (8,)
        assert np.all(np.isfinite(t))

    def test_with_emb_fn(self):
        """emb_fn converts text pairs to embeddings."""
        embeddings = {str(i): np.random.randn(4).astype(np.float32).tolist() for i in range(6)}
        pos = [("0", "1"), ("2", "3")]
        neg = [("0", "5")]
        t = train_supervised_thresholds(
            pos_pairs=pos,
            neg_pairs=neg,
            emb_fn=lambda x: embeddings[x],
            n_candidates=20,
            dim=4,
        )
        assert t.shape == (4,)

    def test_bad_dim_raises(self):
        pairs = _make_pairs(n=3, dim=4)
        with pytest.raises(ValueError, match="must be"):
            train_supervised_thresholds(pos_pairs=pairs, dim=8)


class TestSaveLoadThresholds:
    def test_round_trip(self, tmp_path):
        t = np.array([0.1, -0.2, 0.5, 0.0], dtype=np.float32)
        path = str(tmp_path / "thresholds.npy")
        save_thresholds(t, path)
        loaded = load_thresholds(path)
        np.testing.assert_array_equal(t, loaded)

    def test_load_missing_returns_none(self, tmp_path):
        result = load_thresholds(str(tmp_path / "nonexistent.npy"))
        assert result is None

    def test_saved_thresholds_work_with_binarize(self, tmp_path):
        t = np.array([0.0] * 8, dtype=np.float32)
        path = str(tmp_path / "t.npy")
        save_thresholds(t, path)
        loaded = load_thresholds(path)
        emb = [0.5, -0.5, 0.1, -0.1, 0.9, -0.9, 0.0, 0.01]
        binary = binary_from_threshold_array(emb, loaded)
        assert len(binary) == 1  # 8 bits = 1 byte


class TestRAGEngineThresholds:
    @pytest.mark.asyncio
    async def test_thresholds_attribute(self, rag):
        """thresholds attribute is usable."""
        assert rag.thresholds is None
        t = np.zeros(8, dtype=np.float32)
        rag.thresholds = t
        assert rag.thresholds is not None

    @pytest.mark.asyncio
    async def test_binary_for_uses_direct_thresholds(self, rag):
        """_binary_for uses self.thresholds over file-based path."""
        rag.thresholds = np.array([0.0] * 8, dtype=np.float32)
        binary = rag._binary_for([0.5] * 8)
        assert binary is not None
        assert len(binary) == 1  # 8 bits = 1 byte

    @pytest.mark.asyncio
    async def test_thresholds_from_training_apply(self, rag):
        """Trained thresholds produce working binarization."""
        pos = _make_pairs(n=5, dim=8)
        t = train_supervised_thresholds(pos_pairs=pos, n_candidates=10, dim=8)
        rag.thresholds = t
        binary = rag._binary_for([0.1] * 8)
        assert binary is not None
