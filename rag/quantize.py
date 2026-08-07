"""Maximally-Informative Binarization (MIB) helpers.

Naive-threshold binarization (sign of embedding - midpoint).
Supervised variant (per-dimension threshold) activates via
`supervised_threshold()` after collecting positive pairs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

try:
    import numpy as np

    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# 384 dims → 48 bytes (for intfloat/multilingual-e5-small)
DEFAULT_DIM = 384


def _check_numpy():
    if not _HAS_NUMPY:
        raise ImportError("numpy is required for binary embeddings. Install with: pip install mcp-ariel-memory[binary]")


def _packed_bytes(dim: int) -> int:
    """Number of packed bytes for given dimension."""
    return (dim + 7) // 8


def embed_to_binary(
    emb: Sequence[float],
    threshold: float = 0.0,
    dim: int = DEFAULT_DIM,
) -> bytes:
    """Naive MIB: 1 if sign > threshold, else 0.

    Args:
        emb: dense float32 embedding of length `dim`.
        threshold: per-call midpoint (for supervised variant — array).
        dim: dimensionality (control invariant).

    Returns:
        packed bits, MSB-first. Length = _packed_bytes(dim).
    """
    _check_numpy()
    arr = np.asarray(emb, dtype=np.float32)
    if arr.shape[0] != dim:
        raise ValueError(f"expected dim={dim}, got {arr.shape[0]}")
    bits = (arr > threshold).astype(np.uint8)
    packed = np.packbits(bits, bitorder="big")
    return packed.tobytes()


def supervised_threshold(
    pos_pairs: Iterable[tuple[Sequence[float], Sequence[float]]],
    dim: int = DEFAULT_DIM,
    n_candidates: int = 50,
):
    """Per-dimension threshold maximizing agreement on positive pairs.

    Args:
        pos_pairs: iterable of (emb_a, emb_b) for same semantic relation.
        dim: dimensionality.
        n_candidates: number of threshold candidates per dimension.

    Returns:
        np.ndarray of length dim — thresholds t_i.
    """
    _check_numpy()
    pos_pairs = list(pos_pairs)
    if not pos_pairs:
        raise ValueError("pos_pairs is empty")

    a = np.asarray([p[0] for p in pos_pairs], dtype=np.float32)  # [N, D]
    b = np.asarray([p[1] for p in pos_pairs], dtype=np.float32)
    thresholds = np.zeros(dim, dtype=np.float32)
    for i in range(dim):
        col_a, col_b = a[:, i], b[:, i]
        candidates = np.linspace(
            min(col_a.min(), col_b.min()),
            max(col_a.max(), col_b.max()),
            n_candidates,
        )
        best_t, best_score = candidates[0], -1.0
        for t in candidates:
            agreement = ((col_a > t) == (col_b > t)).mean()
            if agreement > best_score:
                best_score = agreement
                best_t = t
        thresholds[i] = best_t
    return thresholds


def train_supervised_thresholds(
    pos_pairs: list[tuple[Sequence[float], Sequence[float]]],
    neg_pairs: list[tuple[Sequence[float], Sequence[float]]] | None = None,
    emb_fn: Callable | None = None,
    n_candidates: int = 50,
    dim: int = DEFAULT_DIM,
) -> np.ndarray:
    """Train per-dimension thresholds from positive and negative pairs.

    Optimizes a weighted score: 0.7 * agree_pos + 0.3 * agree_neg,
    where agree_pos measures binarization agreement on positive pairs
    and agree_neg measures disagreement on negative pairs.

    Args:
        pos_pairs: (emb_a, emb_b) that share semantic relation.
        neg_pairs: (emb_a, emb_b) that should NOT agree.
        emb_fn: if set, pos_pairs/neg_pairs are (text_a, text_b) and this
                converts text to embedding. Otherwise pairs are raw embeddings.
        n_candidates: threshold grid resolution per dimension.
        dim: embedding dimensionality.

    Returns:
        np.ndarray of length dim — per-dimension thresholds.
    """
    _check_numpy()

    if emb_fn is not None:
        pos_a = np.array([emb_fn(a) for a, _ in pos_pairs])
        pos_b = np.array([emb_fn(b) for _, b in pos_pairs])
    else:
        pos_a = np.asarray([p[0] for p in pos_pairs], dtype=np.float32)
        pos_b = np.asarray([p[1] for p in pos_pairs], dtype=np.float32)

    if pos_a.ndim != 2 or pos_a.shape[1] != dim:
        raise ValueError(f"pos_pairs embeddings must be [N, {dim}], got {pos_a.shape}")

    neg_a = np.array([], dtype=np.float32)
    neg_b = np.array([], dtype=np.float32)
    if neg_pairs:
        if emb_fn is not None:
            neg_a = np.array([emb_fn(a) for a, _ in neg_pairs])
            neg_b = np.array([emb_fn(b) for _, b in neg_pairs])
        else:
            neg_a = np.asarray([p[0] for p in neg_pairs], dtype=np.float32)
            neg_b = np.asarray([p[1] for p in neg_pairs], dtype=np.float32)

    thresholds = np.zeros(dim, dtype=np.float32)
    for i in range(dim):
        candidates = np.linspace(
            min(pos_a[:, i].min(), pos_b[:, i].min()),
            max(pos_a[:, i].max(), pos_b[:, i].max()),
            n_candidates,
        )
        best_t, best_score = candidates[0], -1.0
        for t in candidates:
            agree_pos = ((pos_a[:, i] > t) == (pos_b[:, i] > t)).mean()
            agree_neg = 1.0 - ((neg_a[:, i] > t) == (neg_b[:, i] > t)).mean() if len(neg_a) > 0 else 0.5
            score = 0.7 * agree_pos + 0.3 * agree_neg
            if score > best_score:
                best_score = score
                best_t = t
        thresholds[i] = best_t
    return thresholds


def save_thresholds(thresholds: np.ndarray, path: str):
    """Save thresholds to .npy file."""
    _check_numpy()
    np.save(path, thresholds)


def load_thresholds(path: str) -> np.ndarray | None:
    """Load thresholds from .npy file. Returns None if file doesn't exist."""
    _check_numpy()
    try:
        return np.load(path)
    except (FileNotFoundError, Exception):
        return None


def binary_from_threshold_array(
    emb: Sequence[float],
    thresholds: Sequence[float],
) -> bytes:
    """Binarize using precomputed per-dim thresholds."""
    _check_numpy()
    arr = np.asarray(emb, dtype=np.float32)
    thr = np.asarray(thresholds, dtype=np.float32)
    if arr.shape[0] != thr.shape[0]:
        raise ValueError(f"emb dim={arr.shape[0]} != thresholds len={thr.shape[0]}")
    bits = (arr > thr).astype(np.uint8)
    return np.packbits(bits, bitorder="big").tobytes()


def hamming_distance(a: bytes, b: bytes) -> int:
    """Number of differing bits. Optimized via numpy bitwise XOR."""
    _check_numpy()
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    arr_a = np.frombuffer(a, dtype=np.uint8)
    arr_b = np.frombuffer(b, dtype=np.uint8)
    return int(np.unpackbits(arr_a ^ arr_b, bitorder="big").sum())


def hamming_to_score(distance: int, dim: int = DEFAULT_DIM) -> float:
    """Convert Hamming distance to similarity in [0, 1]."""
    return 1.0 - (distance / dim)


def binary_batch(
    embeddings: Sequence[Sequence[float]],
    thresholds: Sequence[float] | float | None = None,
    dim: int = DEFAULT_DIM,
) -> list[bytes]:
    """Vectorized binarization of multiple embeddings."""
    _check_numpy()
    arr = np.asarray(embeddings, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != dim:
        raise ValueError(f"expected [N, {dim}]")
    if thresholds is None:
        bits = (arr > 0.0).astype(np.uint8)
    elif isinstance(thresholds, (int, float)):
        bits = (arr > thresholds).astype(np.uint8)
    else:
        thr = np.asarray(thresholds, dtype=np.float32)
        if thr.shape != (dim,):
            raise ValueError(f"thresholds shape {thr.shape} != ({dim},)")
        bits = (arr > thr).astype(np.uint8)
    return [row.tobytes() for row in np.packbits(bits, axis=1, bitorder="big")]
