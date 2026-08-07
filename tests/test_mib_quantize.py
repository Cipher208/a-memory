"""Unit tests for rag/quantize. Pure numpy, no DB."""

import pytest

from rag.quantize import (
    binary_batch,
    embed_to_binary,
    hamming_distance,
    hamming_to_score,
    supervised_threshold,
)


def test_embed_to_binary_basic():
    emb = [0.1, -0.2, 0.3, -0.4]  # dim=4 for test
    packed = embed_to_binary(emb, threshold=0.0, dim=4)
    assert len(packed) == 1
    # bits: 1, 0, 1, 0 → MSB-first → 0b1010 = 0x0A
    assert packed[0] == 0b10100000


def test_embed_to_binary_negative_threshold():
    emb = [0.1, -0.2, 0.3, -0.4]
    embed_to_binary(emb, threshold=0.0, dim=4)
    packed_b = embed_to_binary(emb, threshold=0.5, dim=4)
    # with threshold=0.5 all values <0.5 → all zeros
    assert packed_b == b"\x00"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (b"\xff" * 6, b"\xff" * 6, 0),
        (b"\xff" * 6, b"\x00" * 6, 48),
    ],
)
def test_hamming_distance(a, b, expected):
    assert hamming_distance(a, b) == expected


def test_hamming_to_score():
    assert hamming_to_score(0) == 1.0
    assert hamming_to_score(384, dim=384) == pytest.approx(0.0)


def test_binary_batch_consistent_with_single():
    embs = [
        [0.1, -0.2, 0.3, -0.4, 0.5, -0.6, 0.7, -0.8],
        [-0.1, 0.2, -0.3, 0.4, -0.5, 0.6, -0.7, 0.8],
    ]
    batched = binary_batch(embs, dim=8)
    single = [embed_to_binary(e, dim=8) for e in embs]
    assert batched == single


def test_supervised_threshold_separates_pos_neg():
    """Supervised threshold should find optimal separation point."""
    import numpy as np

    # Create pairs with clear separation
    pos_pairs = [([0.8, 0.2] * 192, [0.9, 0.3] * 192)]  # dim=384
    thr = supervised_threshold(pos_pairs, dim=384, n_candidates=10)
    assert thr.shape == (384,)
    # Threshold should exist and be finite
    assert np.isfinite(thr).all()
    # Threshold should be between min and max of the values
    assert thr[0] >= 0.8
    assert thr[0] <= 0.9
    assert thr[1] >= 0.2
    assert thr[1] <= 0.3


def test_binary_pipeline_roundtrip():
    """Generate → binarize → compute distance — idempotent for identical."""
    import numpy as np

    rng = np.random.default_rng(42)
    a = rng.normal(0, 1, size=384).tolist()
    b = a[:]  # copy
    bin_a = embed_to_binary(a, dim=384)
    bin_b = embed_to_binary(b, dim=384)
    assert hamming_distance(bin_a, bin_b) == 0


def test_embed_to_binary_dimension_mismatch():
    emb = [0.1, -0.2, 0.3]  # dim=3
    with pytest.raises(ValueError, match="expected dim=4"):
        embed_to_binary(emb, dim=4)


def test_hamming_distance_length_mismatch():
    a = b"\xff" * 6
    b = b"\xff" * 5
    with pytest.raises(ValueError, match="length mismatch"):
        hamming_distance(a, b)


def test_binary_batch_invalid_shape():
    embs = [[0.1, 0.2]]  # dim=2, not 8
    with pytest.raises(ValueError):
        binary_batch(embs, dim=8)
