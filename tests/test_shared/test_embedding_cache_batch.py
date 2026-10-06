"""Embedding cache: batched lookups must be cheap AND return the same vectors.

Why these exist: `miner_embedding` cost 16.5 s on cowagent whose layer was 100%
cached, because the read path issued one SELECT (plus its own `ensure()` and
`conn.get()`) per text — 1859 round trips for a hit that should be free. These
tests pin both halves of the fix: the batched path returns exactly what the
per-text path returned, and it does not issue one query per text.
"""

import shared.embeddings as emb
from shared.connection import connection_manager


async def _cache(tmp_path, monkeypatch) -> emb.EmbeddingCache:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    cache = emb.EmbeddingCache(cm=connection_manager)
    await cache.ensure()
    return cache


async def test_batched_lookup_matches_per_text_lookup(tmp_path, monkeypatch):
    """Батч обязан возвращать ровно то же, что поштучный путь."""
    cache = await _cache(tmp_path, monkeypatch)
    texts = [f"text-{i}" for i in range(7)]
    await cache._cache_many([(t, [float(i), 0.5, 0.25]) for i, t in enumerate(texts)], "m")

    results, to_compute = await cache._get_results_from_cache(texts, "m")

    assert to_compute == []
    for i, text in enumerate(texts):
        assert results[i] == await cache._get_cached(text, "m"), f"расхождение на {text}"


async def test_batched_lookup_reports_only_missing(tmp_path, monkeypatch):
    """Промахи возвращаются индексами+текстами, попадания — готовыми векторами."""
    cache = await _cache(tmp_path, monkeypatch)
    await cache._cache_many([("known", [1.0, 0.0])], "m")

    results, to_compute = await cache._get_results_from_cache(["known", "missing"], "m")

    assert results[0] == [1.0, 0.0]
    assert results[1] is None
    assert to_compute == [(1, "missing")]


async def test_batched_lookup_handles_duplicate_texts(tmp_path, monkeypatch):
    """Один и тот же текст дважды — оба попадания, без ложного промаха."""
    cache = await _cache(tmp_path, monkeypatch)
    await cache._cache_many([("dup", [3.0, 4.0])], "m")

    results, to_compute = await cache._get_results_from_cache(["dup", "dup"], "m")

    assert to_compute == []
    assert results[0] == results[1] == [3.0, 4.0]


async def test_batched_lookup_reads_int8_rows(tmp_path, monkeypatch):
    """INT8-блоб декодируется и в батче так же, как поштучно (A3.2)."""
    cache = await _cache(tmp_path, monkeypatch)
    monkeypatch.setattr(emb, "_int8_enabled", lambda: True)
    vec = [0.5, -0.5, 0.25, 0.125]

    await cache._cache_many([("int8-text", vec)], "m")
    results, to_compute = await cache._get_results_from_cache(["int8-text"], "m")

    assert to_compute == []
    assert len(results[0]) == len(vec)
    for a, b in zip(vec, results[0], strict=True):
        assert abs(a - b) < 0.01


async def test_batched_lookup_does_not_query_per_text(tmp_path, monkeypatch):
    """Главное свойство: N попаданий — это НЕ N запросов к базе."""
    cache = await _cache(tmp_path, monkeypatch)
    texts = [f"t{i}" for i in range(50)]
    await cache._cache_many([(t, [1.0, 2.0]) for t in texts], "m")

    conn = await connection_manager.get(emb.DB_NAME)
    real_execute = conn.execute
    selects: list[str] = []

    async def counting_execute(sql, params=()):
        if "embedding_cache" in sql and "select" in sql.lower():
            selects.append(sql)
        return await real_execute(sql, params)

    monkeypatch.setattr(conn, "execute", counting_execute)
    results, to_compute = await cache._get_results_from_cache(texts, "m")

    assert to_compute == []
    assert all(r is not None for r in results)
    assert len(selects) == 1, f"ожидался 1 запрос на пачку из {len(texts)}, а их {len(selects)}"


async def test_cache_many_commits_in_batches_not_one_transaction(tmp_path, monkeypatch):
    """Запись идёт порциями, а не одной транзакцией на всю пачку.

    Иначе рестарт посреди ночного прогона теряет ВСЕ посчитанные векторы,
    тогда как прежний поштучный путь терял максимум один. Проверяем, что при
    пачке больше порции коммитов больше одного — то есть долговечность
    частичная, как и была.
    """
    cache = await _cache(tmp_path, monkeypatch)
    monkeypatch.setattr(emb, "_CACHE_LOOKUP_CHUNK", 4)
    total = 10

    conn = await connection_manager.get(emb.DB_NAME)
    real_commit = conn.commit
    commits = 0

    async def counting_commit():
        nonlocal commits
        commits += 1
        await real_commit()

    monkeypatch.setattr(conn, "commit", counting_commit)
    await cache._cache_many([(f"b{i}", [1.0]) for i in range(total)], "m")

    assert await cache.count() == total
    assert commits == 3, f"10 строк порциями по 4 — это 3 коммита, а не {commits}"


async def test_cache_many_writes_every_row(tmp_path, monkeypatch):
    """Батч-запись сохраняет все строки — иначе кэш тихо теряет векторы."""
    cache = await _cache(tmp_path, monkeypatch)
    texts = [f"w{i}" for i in range(5)]

    await cache._cache_many([(t, [float(i)]) for i, t in enumerate(texts)], "m")

    assert await cache.count() == 5
    for i, text in enumerate(texts):
        got = await cache._get_cached(text, "m")
        assert got is not None and got[0] == float(i)


def test_fallback_tag_is_prefixed_exactly_once():
    """Без модели тег уже помечен — второй префикс делал строки недостижимыми."""
    assert emb._fallback_tag("hash-fallback/model-x") == "hash-fallback/model-x"
    # а когда модель БЫЛА и сработал breaker, префикс нужен
    assert emb._fallback_tag("model-x") == "hash-fallback/model-x"


async def test_embed_reuses_hash_fallback_rows(tmp_path, monkeypatch):
    """Хэш-фолбэк обязан переиспользовать свои строки, а не считать заново.

    Регрессия: двойной префикс тега писал строки под
    `hash-fallback/hash-fallback/<model>`, тогда как поиск спрашивал
    `hash-fallback/<model>` — каждая строка была недостижима, текст
    пересчитывался и перезаписывался на каждом прогоне.

    Сравниваем НЕ float-равенство векторов: колонка хранит float32, а
    `_hash_embedding` даёт компоненты вплоть до 1e-72, которые во float32
    схлопываются в ноль. Значимо то, что доходит до графа — MIB-биты
    (`embed_to_binary` сам приводит к float32) и, значит, Jaccard.
    """
    from lifecycle.graph_miners import _bit_jaccard, _bits_int
    from rag.quantize import embed_to_binary

    cache = await _cache(tmp_path, monkeypatch)
    monkeypatch.setenv("ARIEL_HASH_EMBEDDINGS", "1")  # модели нет → hash-fallback

    first = await cache.embed(["alpha", "beta"])
    assert await cache.count() == 2

    conn = await connection_manager.get(emb.DB_NAME)
    tags = [r[0] for r in (await (await conn.execute("SELECT DISTINCT model_name FROM embedding_cache")).fetchall())]
    assert tags == ["hash-fallback/intfloat/multilingual-e5-small"], tags

    # второй прогон не должен ничего дописывать
    second = await cache.embed(["alpha", "beta"])
    assert await cache.count() == 2, "второй прогон не должен добавлять строки"

    # и должен вернуть векторы, эквивалентные исходным для графа
    for i, text in enumerate(["alpha", "beta"]):
        assert second[i] == await cache._get_cached(text, "hash-fallback/intfloat/multilingual-e5-small"), (
            f"{text} должен читаться из кэша, а не считаться заново"
        )
        bits_a = _bits_int(embed_to_binary(first[i], dim=len(first[i])))
        bits_b = _bits_int(embed_to_binary(second[i], dim=len(second[i])))
        assert bits_a == bits_b, f"MIB-биты {text} обязаны совпасть"
        assert _bit_jaccard(bits_a, bits_b) == 1.0
