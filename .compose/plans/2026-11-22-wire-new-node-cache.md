# wire_new_node NER from cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose-subagent (recommended) or compose-execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `wire_new_node` перестаёт импортировать spaCy на горячем пути дистилляции и берёт сущности из кэша, прогретого воркером, с dict-only на промахе.

**Architecture:** `wire_new_node` заменяет `nlp = _get_ner()` + N вызовов `_entities(...)` на один вызов `_entities_read(read, texts, syn, None)`, который читает `ner_cache` по backend-тегу из метаданных и пишет промахи под `|dict-only`.spaCy остаётся только в ночном `warm_ner_cache.py`.

**Tech Stack:** Python 3.11 (repo `.venv`), pytest + pytest-asyncio, aiosqlite, spaCy 3.8.16 / en_core_web_sm 3.8.0.

## Global Constraints

- Работать в worktree: `/home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache` (branch `fix/wire-new-node-cache`), создание — через `git worktree add`.
- Тесты гонять venv'ом репозитория: `.venv/bin/python -m pytest`. Системный `python3` не имеет зависимостей.
- **Живые базы `~/.mcp-ariel-memory-*` неприкосновенны.** Тесты — только на временных БД (fixture `db`).
- `nlp=None` → бэкенд из метаданных; write-тег при отсутствии модели остаётся `dict-only`.
- spaCy/torch не должны появляться в `sys.modules` из-за `wire_new_node`.
- Коммит — только явные файлы, без `commit -a`; pre-commit требует `uv` в PATH: `PATH="/home/murat/.local/bin:$PATH" git commit ...`.
- Комментарии в коде — на английском.

---

### Task 1: `wire_new_node` читает сущности через кэш

**Covers:** [S1, S2, S3, S4, S6]

**Files:**
- Modify: `lifecycle/graph_miners.py` (`wire_new_node`, строки 1680–1741)
- Test: `tests/test_lifecycle/test_graph_miners.py`

**Interfaces:**
- Consumes: `_entities_read(read, texts, syn, nlp) -> tuple[list[set[str]], list[tuple[str, str, str]]]` (уже существует), `_ner_cache_tag`, `_layer_nodes`.
- Produces: `wire_new_node(cm, layer, node_id, content, tags=None, *, read) -> Callable[[Any], Awaitable[int]]` — сигнатура неизменна.

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_lifecycle/test_graph_miners.py`:

```python
@pytest.mark.asyncio
async def test_wire_new_node_does_not_import_spacy(db, monkeypatch):
    """Горячий путь дистилляции не имеет права тянуть spaCy в процесс."""
    import lifecycle.graph_miners as gm

    monkeypatch.setattr(gm, "_get_ner", lambda: (_ for _ in ()).throw(AssertionError("_get_ner must not be called")))
    conn = await connection_manager.get(DB_NAME)
    await gm._ensure_ner_cache(conn)
    first = await _node("python sqlite indexing", T)
    second = await _node("python sqlite indexing guide", T)
    await conn.commit()

    before = set(sys.modules)
    await gm.wire_new_node(conn, "user", second, "python sqlite indexing guide", ["db"], read=conn)
    assert not ({"spacy", "torch"} & (set(sys.modules) - before))
    assert "spacy" not in sys.modules
    assert "torch" not in sys.modules


@pytest.mark.asyncio
async def test_wire_new_node_uses_warm_cache(db):
    """Прогретая строка воркера даёт co_mentions без загрузки spaCy."""
    import hashlib
    import json as _json

    import lifecycle.graph_miners as gm

    conn = await connection_manager.get(DB_NAME)
    await gm._ensure_ner_cache(conn)
    first = await _node("Борис работает в Acme Corp", T)
    second = await _node("офис Acme Corp открыт давно", T)

    syn = gm.load_synonyms()
    warm_tag = gm._ner_cache_tag(syn, None)  # == backend tag: модель установлена
    for text in ("Борис работает в Acme Corp", "офис Acme Corp открыт давно"):
        h = hashlib.sha256(text.encode()).hexdigest()
        await conn.execute(
            "INSERT OR REPLACE INTO ner_cache (text_hash, tag, entities) VALUES (?, ?, ?)",
            (h, warm_tag, _json.dumps(["acme corp"])),
        )
    await conn.commit()

    await gm.wire_new_node(conn, "user", second, "офис Acme Corp открыт давно", [], read=conn)
    rows = await _edges("co_mentions")
    assert (min(first, second), max(first, second)) in {(r["source_id"], r["target_id"]) for r in rows}


@pytest.mark.asyncio
async def test_wire_new_node_cold_cache_writes_dict_only(db):
    """Холодный кэш: промах пишется под dict-only и не отравляет spaCy-тег."""
    import lifecycle.graph_miners as gm

    conn = await connection_manager.get(DB_NAME)
    await gm._ensure_ner_cache(conn)
    second = await _node("python sqlite indexing guide", T)
    await conn.commit()

    before_rows = await (await conn.execute("SELECT COUNT(*) FROM ner_cache")).fetchone()
    before_count = int(before_rows[0])

    await gm.wire_new_node(conn, "user", second, "python sqlite indexing guide", ["db"], read=conn)

    rows = await (await conn.execute("SELECT text_hash, tag, entities FROM ner_cache")).fetchall()
    new_rows = rows[before_count:]
    assert new_rows, "a miss must produce a cache row"
    assert all(r["tag"].endswith("|dict-only") for r in new_rows), [r["tag"] for r in new_rows]
    assert gm._ner_cache_tag(gm.load_synonyms(), None) != new_rows[0]["tag"]


@pytest.mark.asyncio
async def test_wire_new_node_co_mentions_from_dict_entities(db):
    """dict-only сущности (синонимы) тоже дают co_mentions, без spaCy."""
    import lifecycle.graph_miners as gm

    conn = await connection_manager.get(DB_NAME)
    await gm._ensure_ner_cache(conn)
    syn = {"алиса": ["alice"]}
    first = await _node("Алиса и Борис здесь", T)
    second = await _node("Алиса снова тут", T)
    await conn.commit()

    await gm.wire_new_node(conn, "user", second, "Алиса снова тут", [], read=conn)
    rows = await _edges("co_mentions")
    assert (min(first, second), max(first, second)) in {(r["source_id"], r["target_id"]) for r in rows}
```

⚠️ Тесты используют существующие хелперы `_node`, `_edges` и константу `T` из этого же файла.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
.venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'wire_new_node' -v
```
Expected: FAIL — `AssertionError: _get_ner must not be called` (первый тест), остальные могут падать поssh.

- [ ] **Step 3: Implement the change**

В `lifecycle/graph_miners.py`, в `wire_new_node`, заменить блок:

```python
    syn = load_synonyms()
    my_toks = _canon_tokens(content, syn)
    my_ents = _entities(content, syn)
    my_tags = set(tags or [])
    if not my_toks and not my_ents and not my_tags:
        return _noop(0)
    others = await _layer_nodes(read, layer)
    other_tags: dict[int, set[str]] = {}
    for r in await (await read.execute("SELECT node_id, tag FROM epi_tags WHERE node_id != ?", (node_id,))).fetchall():
        other_tags.setdefault(int(r["node_id"]), set()).add(str(r["tag"]))
    nlp = _get_ner()
```

на:

```python
    syn = load_synonyms()
    my_toks = _canon_tokens(content, syn)
    my_tags = set(tags or [])
    others = await _layer_nodes(read, layer)
    other_tags: dict[int, set[str]] = {}
    for r in await (await read.execute("SELECT node_id, tag FROM epi_tags WHERE node_id != ?", (node_id,))).fetchall():
        other_tags.setdefault(int(r["node_id"]), set()).add(str(r["tag"]))
    # Entities come from the cache the warmer fills, not from spaCy in this
    # process: importing spaCy here pulls 546 MB of torch that stays resident
    # for the life of a long-lived daemon. A miss is computed dictionary-only
    # and written under the dict-only tag; the nightly warmer fills it.
    ents_list, _pending = await _entities_read(read, [content] + [str(c) for _, c in others], syn, None)
    my_ents = ents_list[0]
    other_ents = dict(zip((oid for oid, _ in others), ents_list[1:], strict=True))
    if not my_toks and not my_ents and not my_tags:
        return _noop(0)
```

И в цикле заменить:

```python
        if my_ents and my_ents & _entities(ocontent, syn, nlp):
```

на:

```python
        if my_ents and my_ents & other_ents.get(oid, set()):
```

Импорты: `_entities_read` уже в этом модуле; `_get_ner` остаётся (нужен воркеру) — не удалять.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
.venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'wire_new_node' -v
```
Expected: PASS, 4 теста.

- [ ] **Step 5: Run the full file + gates**

Run:
```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
.venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -q 2>&1 | tail -3
.venv/bin/python -m ruff check lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
.venv/bin/python -m ruff format --check lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
.venv/bin/python -m mypy lifecycle/graph_miners.py
```
Expected: все тесты файла зелёные (56 + 4 = 60), ruff чисто, mypy Success.

- [ ] **Step 6: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
git add lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
PATH="/home/murat/.local/bin:$PATH" git commit -m "fix(graph): wire_new_node reads entities from the warmer cache, not spaCy"
```

---

### Task 2: Финальные гейты и отчёт

**Covers:** [S5, S7]

**Files:** без изменений кода.

- [ ] **Step 1: Полный прогон**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3
```
Expected: 0 failed (было 2884; +4 новых).

- [ ] **Step 2: Проверка живого инварианта**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/wire-new-node-cache
.venv/bin/python - <<'PY'
import os, sys, tempfile
os.environ['MCP_MEMORY_DATA_DIR'] = tempfile.mkdtemp(prefix='ariel-wnn-')
sys.path.insert(0, '.')
import lifecycle.graph_miners as gm
print("module import clean:", 'spacy' not in sys.modules and 'torch' not in sys.modules)
print("_ner_backend_tag:", gm._ner_backend_tag())
PY
```
Expected: `True`, тег `core_web_sm-3.8.0`.

- [ ] **Step 3: Отчёт**

Записать отчёт в `.compose/reports/wire-new-node-cache.md` (в worktree), закоммитить:
```bash
git add -f .compose/reports/wire-new-node-cache.md
PATH="/home/murat/.local/bin:$PATH" git commit -m "docs: wire_new_node cache final report"
```
