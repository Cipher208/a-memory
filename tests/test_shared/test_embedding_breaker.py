"""E2 wiring: model.encode failures trip the breaker; open circuit → hash fallback."""

import pytest


class _ExplodingModel:
    def encode(self, texts):
        raise RuntimeError("model exploded")


async def test_open_circuit_falls_back_to_hash(tmp_path, monkeypatch):
    import shared.embeddings as emb

    monkeypatch.delenv("ARIEL_HASH_EMBEDDINGS", raising=False)
    monkeypatch.setattr(emb, "_get_model", lambda: _ExplodingModel())
    monkeypatch.setattr(emb.connection_manager, "base_dir", tmp_path)
    cache = emb.EmbeddingCache(cm=emb.connection_manager)
    await cache.ensure()
    emb._embedding_breaker.reset()
    # threshold=3: three raises...
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cache.embed(["hello"])
    # ...then the breaker is open and embed() degrades to hash vectors
    vecs = await cache.embed(["hello"])
    assert len(vecs[0]) == 384  # hash-fallback dimension
    assert emb._embedding_breaker.get_metrics()["total_rejections"] >= 1


async def test_success_resets_failure_streak(tmp_path, monkeypatch):
    import shared.embeddings as emb

    class _FlakyThenGoodModel:
        def __init__(self):
            self.calls = 0

        def encode(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("flaky")
            return _FakeArray([[0.1] * 384 for _ in texts])

    class _FakeArray:
        """Mimics numpy: sentence-transformers returns .tolist()-able output."""

        def __init__(self, rows):
            self._rows = rows

        def tolist(self):
            return self._rows

    monkeypatch.delenv("ARIEL_HASH_EMBEDDINGS", raising=False)
    model = _FlakyThenGoodModel()
    monkeypatch.setattr(emb, "_get_model", lambda: model)
    monkeypatch.setattr(emb.connection_manager, "base_dir", tmp_path)
    cache = emb.EmbeddingCache(cm=emb.connection_manager)
    await cache.ensure()
    emb._embedding_breaker.reset()
    with pytest.raises(RuntimeError):
        await cache.embed(["a"])
    # recovery before threshold: breaker stays closed, model path resumes
    vecs = await cache.embed(["b"])
    assert vecs[0][0] == 0.1
    assert emb._embedding_breaker.state.value == "closed"


async def test_hash_fallback_counter_and_loud_warning(tmp_path, monkeypatch, caplog):
    """Degradation must be visible: counter + WARNING naming the reason."""
    import logging

    import shared.embeddings as emb

    monkeypatch.delenv("ARIEL_HASH_EMBEDDINGS", raising=False)
    # Force the local-model path: a prior test may have set embeddings.url, and
    # a LIVE :8710 would turn the expected failures into real successes.
    monkeypatch.setattr(emb, "_remote_active", lambda: False)
    monkeypatch.setattr(emb, "_get_model", lambda: _ExplodingModel())
    monkeypatch.setattr(emb.connection_manager, "base_dir", tmp_path)
    monkeypatch.setattr(emb, "_fallback_count", 0)
    monkeypatch.setattr(emb, "_fallback_logged_at", 0)
    cache = emb.EmbeddingCache(cm=emb.connection_manager)
    await cache.ensure()
    emb._embedding_breaker.reset()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cache.embed(["hello"])
    with caplog.at_level(logging.WARNING, logger="shared.embeddings"):
        vecs = await cache.embed(["hello"])  # breaker open → hash fallback
    assert len(vecs[0]) == 384
    assert emb.hash_fallback_stats()["hash_fallback"] == 1
    assert "hash fallback x1" in caplog.text
    assert "circuit breaker open" in caplog.text
