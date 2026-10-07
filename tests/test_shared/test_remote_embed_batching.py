"""One bounded HTTP request per batch, instead of one request for the layer.

Why these exist: `_remote_embed` used to POST the whole text list in a single
request bounded by a fixed 30 s timeout. A ~6k-text layer measured 24.4 s, so
the timeout was a ceiling on layer size with ~20 % of headroom — and
`miner_embedding` swallowed the resulting timeout as a silent `{"edges": 0}`,
which is how `semantic_overlap` stayed frozen for six days without a log line.

These pin that the list is split, that nothing is lost or reordered by the
split, and that the ordinary small case still costs exactly one request.
"""

import json
import urllib.request
from collections.abc import AsyncIterator
from typing import Self

import pytest

import shared.embeddings as emb
from shared.connection import connection_manager

URL = "http://127.0.0.1:8710/v1/embeddings"


@pytest.fixture
async def cache(tmp_path, monkeypatch) -> AsyncIterator[emb.EmbeddingCache]:
    """Своя база на tmp_path — и уборка за собой.

    `connection_manager._conns` кэширует соединение по имени файла, поэтому
    фикстура, которая не чистит `_conns` на выходе, оставляет следующему тесту
    соединение к уже удалённому каталогу. Проверено: без этой уборки соседний
    `test_archived_memories` падал на `no such table: archived_memories` — он
    не изолирован и берёт соединение из того же кэша.
    """
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    c = emb.EmbeddingCache(cm=connection_manager)
    await c.ensure()
    yield c
    connection_manager._conns.clear()


def _install_fake_service(monkeypatch, calls: list[dict]) -> None:
    """Record every POST; answer with `index` reversed to exercise the sort."""

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode()

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_urlopen(req, timeout=None):
        texts = json.loads(req.data)["input"]
        calls.append({"texts": texts, "timeout": timeout, "url": req.full_url})
        items = [{"index": i, "embedding": [float(t.lstrip("t") or 0), 0.5]} for i, t in enumerate(texts)]
        return _Resp({"data": list(reversed(items))})

    monkeypatch.setattr(emb, "_remote_url", lambda: URL)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


async def test_remote_embed_splits_a_long_list_into_bounded_requests(cache, monkeypatch):
    """25 текстов при батче 10 — три запроса, последний короткий."""
    monkeypatch.setattr(emb, "_REMOTE_BATCH_SIZE", 10)
    calls: list[dict] = []
    _install_fake_service(monkeypatch, calls)

    out = await cache._remote_embed([f"t{i}" for i in range(25)])

    assert len(out) == 25
    assert [len(c["texts"]) for c in calls] == [10, 10, 5], f"запросы: {[len(c['texts']) for c in calls]}"
    assert all(c["timeout"] == emb._REMOTE_TIMEOUT_S for c in calls)
    assert all(c["url"] == URL for c in calls)


async def test_remote_embed_keeps_input_order_across_batches(cache, monkeypatch):
    """Склейка чанков обязана сохранить порядок входа, а не порядок ответов."""
    monkeypatch.setattr(emb, "_REMOTE_BATCH_SIZE", 5)
    calls: list[dict] = []
    _install_fake_service(monkeypatch, calls)

    out = await cache._remote_embed([f"t{i}" for i in range(12)])

    assert [v[0] for v in out] == [float(i) for i in range(12)]
    assert len(calls) == 3


async def test_remote_embed_uses_one_request_when_under_batch_size(cache, monkeypatch):
    """Обычный случай не подорожал: список меньше батча — ровно один POST."""
    calls: list[dict] = []
    _install_fake_service(monkeypatch, calls)

    out = await cache._remote_embed(["t0", "t1", "t2"])

    assert len(calls) == 1
    assert [v[0] for v in out] == [0.0, 1.0, 2.0]


async def test_remote_embed_empty_input_makes_no_request(cache, monkeypatch):
    """Пустой список — ноль запросов, а не запрос с пустым телом."""
    calls: list[dict] = []
    _install_fake_service(monkeypatch, calls)

    assert await cache._remote_embed([]) == []
    assert calls == []
