"""A3.2: INT8 scalar quantization for the embedding cache (75% smaller blobs)."""

import pytest

import shared.embeddings as emb


def test_int8_roundtrip_error_bounded():
    """Symmetric quantization: relative error <= 1/127 per dim."""
    vec = [0.5, -0.25, 0.0, 1.0, -1.0]
    blob = emb._encode_int8(vec)
    assert blob[0:2] == b"\xa9\x00"  # magic marker
    out = emb._decode_int8(blob)
    assert len(out) == len(vec)
    for a, b in zip(vec, out, strict=True):
        assert abs(a - b) <= 1.0 / 127 + 1e-6


def test_int8_size_advantage():
    import struct

    vec = [0.1] * 384
    assert len(emb._encode_int8(vec)) == 390  # 2 magic + 4 scale + 384 dims
    assert len(struct.pack(f"{384}f", *vec)) == 1536


def test_zero_vector_roundtrip():
    out = emb._decode_int8(emb._encode_int8([0.0] * 8))
    assert all(v == 0.0 for v in out)


def test_int8_disabled_by_default(tmp_path, monkeypatch):
    """Default config: cache stores float32 (legacy behavior)."""
    monkeypatch.setattr(connection_manager_mod(), "base_dir", tmp_path)

    class _FakeConfig:
        def get(self, section, key, default=None):
            return default

    import config as config_mod

    monkeypatch.setattr(config_mod, "config", _FakeConfig())
    assert emb._int8_enabled() is False


def connection_manager_mod():
    from shared import connection as conn_mod

    return conn_mod.connection_manager


async def test_cache_roundtrip_int8_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager_mod(), "base_dir", tmp_path)
    cache = emb.EmbeddingCache(cm=emb.connection_manager)
    await cache.ensure()

    # force INT8 path
    monkeypatch.setattr(emb, "_int8_enabled", lambda: True)
    vec = [0.5, -0.5, 0.25] * 128
    await cache._cache("text-a", vec, "model-x")
    out = await cache._get_cached("text-a", "model-x")
    assert out is not None and len(out) == 384
    for a, b in zip(vec, out, strict=True):
        assert abs(a - b) < 0.01

    # legacy float32 blob still readable (back-compat with existing rows)
    import struct

    legacy_text = "text-a-legacy"
    conn = await emb.connection_manager.get(emb.DB_NAME)
    float_blob = struct.pack(f"{len(vec)}f", *vec)
    await conn.execute(
        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
        (cache._hash_text(legacy_text), float_blob, "model-x"),
    )
    await conn.commit()
    out2 = await cache._get_cached(legacy_text, "model-x")
    assert out2 is not None and all(abs(a - b) < 1e-6 for a, b in zip(vec, out2, strict=True))
