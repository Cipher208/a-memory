# NER out-of-process Implementation Plan
> [!NOTE]
> This document may not reflect the current implementation.
> See the final report for up-to-date state:
> [Final Report](reports/ner-out-of-process.md)


> **For agentic workers:** REQUIRED SUB-SKILL: Use compose-subagent (recommended) or compose-execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ночной майнер сущностей перестаёт импортировать spaCy в процессе-лидере; тег кэша вычисляется из метаданных пакета модели, а добор промахов делает внешний warm-воркер по таймеру.

**Architecture:** `_ner_cache_tag` получает бэкенд NER из `<site-packages>/<model>/meta.json` вместо `nlp.meta`, поэтому ночной проход читает кэш без импорта spaCy/torch. Промахи пишутся dict-only. `scripts/warm_ner_cache.py` (spaCy в своём процессе) добирает их по systemd-таймеру, по одному на контур.

**Tech Stack:** Python 3.11 (repo `.venv`), pytest + pytest-asyncio, aiosqlite, spaCy 3.8.16 / en_core_web_sm 3.8.0, systemd user timers.

## Global Constraints

- Работать в worktree `/home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process` (branch `feat/ner-out-of-process`).
- Тесты гонять venv'ом ворктри: `.venv/bin/python -m pytest`. Системный `python3` не имеет зависимостей.
- Свежий worktree-venv идёт **без spaCy-моделей**; их надо доставить (Task 0, Step 1), иначе тесты, зависящие от модели, краснеют или скипаются.
- `nlp is None` → backend обязан остаться `dict-only`; тег не должен меняться для уже закэшированных строк.
- spaCy/torch **не имеют права** попасть в `sys.modules` ночного прохода.
- Живые базы `~/.mcp-ariel-memory-*` не трогать: ни миграций, ни записи, ни прогонов на них. Тесты — только на временных БД (`db` fixture).
- Каждый коммит — только явно перечисленные файлы, никаких `commit -a`.
- Комментарии в коде — на английском.

---

### Task 0: Зелёный baseline — модели spaCy в worktree venv + прямая проверка вместо RSS-порога

**Covers:** [S5]

**Files:**
- Modify: `tests/test_autohooks/test_daemon_memory.py` (два теста: `test_spacy_gate_still_works_when_enabled`, `test_the_two_gates_are_independent`)
- Environment: `.venv` ворктри (модели `en_core_web_sm`, `ru_core_news_sm`)

**Interfaces:**
- Consumes: ничего.
- Produces: зелёный baseline, на котором Tasks 1–4 можно верифицировать.

**Почему это нужно.** На чистом worktree (только docs-файл) три теста падали:
`test_spacy_gate_still_works_when_enabled`, `test_the_two_gates_are_independent`,
`test_miner_entities_spacy_org_shared_mention`. Первые два — потому что в свежем worktree-venv
**нет spaCy-моделей**, а `sanitize()` глушит `OSError: [E050] Can't find model` через
`except Exception: pass`. Третий — потому что майнер тогда тоже не видел модель.
В CI тесты не падают: моделей там нет, и они уходят в `pytest.skip` — то есть CI их не проверяет.
После установки моделей `test_miner_entities_spacy_org_shared_mention` **уже зелёный**.
Остаются два теста, где поведение верное («John» маскируется, `priv._get_nlp() is not None`),
но внешняя эвристика `rss_kb > 400 * 1024` не выполняется: веса модели mmapped, и на одном
коротком предложении резидентно 204–226 MB. Это proxy-метрика вместо наблюдения.

- [ ] **Step 1: Install the models into the worktree venv**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
.venv/bin/python scripts/install_ner_models.py || {
  SRC=/home/murat/mcp-ariel-memory/.venv/lib/python3.11/site-packages
  DST=$(.venv/bin/python -c "import sysconfig;print(sysconfig.get_paths()['purelib'])")
  for M in en_core_web_sm ru_core_news_sm; do
    cp -r "$SRC/$M" "$DST/$M"
    for D in "$SRC/$M"-*.dist-info; do [ -d "$D" ] && cp -r "$D" "$DST/$(basename "$D")"; done
  done
}
.venv/bin/python -c "from importlib.metadata import version; print(version('en_core_web_sm'), version('ru_core_news_sm'))"
```

Expected: `3.8.0 3.8.0`. Модели `py3-none-any` — копирование между 3.11 и 3.12 безопасно.

- [ ] **Step 2: Write the failing test (probe reports the fact instead of RSS)**

В `test_spacy_gate_still_works_when_enabled` заменить `flush({"out": out})` на
`flush({"out": out, "model_loaded": priv._nlp is not None})` и заменить внешний RSS-ассерт на:

```python
    assert result["model_loaded"] is True, (
        "enabling the gate did not load a model — it may have regressed to a silent no-op"
    )
```

- [ ] **Step 3: Same for the independence test**

В `test_the_two_gates_are_independent` заменить `flush({})` на
`flush({"model_loaded": priv._nlp is not None})` и заменить RSS-ассерт на:

```python
    assert result["model_loaded"] is True, "the privacy gate did not open even when explicitly enabled"
```

- [ ] **Step 4: Run the three tests**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_autohooks/test_daemon_memory.py -k 'spacy_gate_still_works_when_enabled or two_gates_are_independent' tests/test_lifecycle/test_graph_miners.py::test_miner_entities_spacy_org_shared_mention -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
git add tests/test_autohooks/test_daemon_memory.py
git commit -m "test(autohooks): assert the model loaded instead of an RSS threshold"
```

---

### Task 1: `_ner_backend_tag()` — бэкенд NER из метаданных модели

**Covers:** [S3, S4]

**Files:**
- Modify: `lifecycle/graph_miners.py` (после `_ner_dict_fingerprint`, перед `_ner_cache_tag`, ~строка 579)
- Test: `tests/test_lifecycle/test_graph_miners.py`

**Interfaces:**
- Consumes: `_ner_dict_fingerprint(syn) -> str` (уже существует).
- Produces: `_NER_MODEL: str` (константа `"en_core_web_sm"`), `_ner_backend_tag() -> str | None` — `None` если spaCy не установлен или пакет модели не найден; иначе `"core_web_sm-3.8.0"`.

- [ ] **Step 1: Write the failing test**

Добавить в конец `tests/test_lifecycle/test_graph_miners.py`:

```python
def test_ner_backend_tag_none_without_spacy(monkeypatch):
    """spaCy не установлен ⇒ бэкенда нет, тег остаётся dict-only."""
    import lifecycle.graph_miners as gm

    monkeypatch.setattr(gm.importlib.util, "find_spec", lambda name: None)
    assert gm._ner_backend_tag() is None


def test_ner_backend_tag_reads_model_meta(monkeypatch, tmp_path):
    """Тег берётся из meta.json пакета модели, без импорта spaCy."""
    import lifecycle.graph_miners as gm

    pkg = tmp_path / "en_core_web_sm"
    pkg.mkdir()
    (pkg / "meta.json").write_text('{"name": "core_web_sm", "version": "3.8.0"}', encoding="utf-8")

    class _Spec:
        origin = str(pkg / "__init__.py")
        submodule_search_locations = [str(pkg)]

    monkeypatch.setattr(gm.importlib.util, "find_spec", lambda name: _Spec())
    assert gm._ner_backend_tag() == "core_web_sm-3.8.0"
    assert "spacy" not in gm.sys.modules
    assert "torch" not in gm.sys.modules


def test_ner_backend_tag_none_on_broken_meta(monkeypatch, tmp_path):
    """Битый meta.json не роняет проход — бэкенда нет."""
    import lifecycle.graph_miners as gm

    pkg = tmp_path / "en_core_web_sm"
    pkg.mkdir()
    (pkg / "meta.json").write_text("{not json", encoding="utf-8")

    class _Spec:
        origin = str(pkg / "__init__.py")
        submodule_search_locations = [str(pkg)]

    monkeypatch.setattr(gm.importlib.util, "find_spec", lambda name: _Spec())
    assert gm._ner_backend_tag() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'ner_backend_tag' -v`
Expected: FAIL — `AttributeError: module 'lifecycle.graph_miners' has no attribute '_ner_backend_tag'`.

- [ ] **Step 3: Add imports and implementation**

В шапке `lifecycle/graph_miners.py` рядом с существующими импортами (после `import hashlib`) добавить:

```python
import importlib.util
import sys
```

Перед `def _ner_cache_tag(...)` вставить:

```python
# The spaCy model the miner uses (`privacy._get_nlp(for_ner=True)`). The tag is
# derived from the installed package's meta.json so the nightly pass can compute
# it without importing spaCy — one import costs 546 MB RSS of torch that then
# stays resident for the life of the process.
_NER_MODEL = "en_core_web_sm"


def _ner_backend_tag() -> str | None:
    """The NER backend fingerprint, read from the model package metadata.

    Returns `None` when spaCy or the model package is absent — the caller then
    falls back to `dict-only`, exactly as a process without spaCy always has.
    ``importlib.util.find_spec`` locates the package without importing it, so
    torch never enters ``sys.modules`` on this path.
    """
    if importlib.util.find_spec("spacy") is None:
        return None
    spec = importlib.util.find_spec(_NER_MODEL)
    locations = getattr(spec, "submodule_search_locations", None) or []
    for location in locations:
        meta_path = pathlib.Path(location) / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("ner backend: cannot read %s", meta_path)
            continue
        name = meta.get("name")
        version = meta.get("version")
        if name and version:
            return f"{name}-{version}"
    return None
```

Добавить `import pathlib` к импортам, если его там нет.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'ner_backend_tag' -v`
Expected: PASS, 3 теста.

- [ ] **Step 5: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
git add lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
git commit -m "feat(graph): derive the NER cache backend from model metadata"
```

---

### Task 2: `_ner_cache_tag` использует бэкенд из метаданных

**Covers:** [S2, S3]

**Files:**
- Modify: `lifecycle/graph_miners.py:579-591` (`_ner_cache_tag`)
- Test: `tests/test_lifecycle/test_graph_miners.py`

**Interfaces:**
- Consumes: `_ner_backend_tag() -> str | None` из Task 1.
- Produces: `_ner_cache_tag(syn, nlp) -> str` — сигнатура неизменна; при `nlp is None` бэкенд берётся из `_ner_backend_tag()`.

- [ ] **Step 1: Write the failing test**

```python
def test_ner_cache_tag_none_uses_metadata_backend(monkeypatch):
    """nlp=None, но модель установлена: тег несёт бэкенд, а не dict-only."""
    import lifecycle.graph_miners as gm

    monkeypatch.setattr(gm, "_ner_backend_tag", lambda: "core_web_sm-3.8.0")
    tag = gm._ner_cache_tag({"алиса": ["alice"]}, None)
    assert tag.endswith("|core_web_sm-3.8.0")
    assert "dict-only" not in tag


def test_ner_cache_tag_none_falls_back_to_dict_only(monkeypatch):
    """Модели нет ⇒ прежнее поведение, старые строки остаются валидными."""
    import lifecycle.graph_miners as gm

    monkeypatch.setattr(gm, "_ner_backend_tag", lambda: None)
    assert gm._ner_cache_tag({"алиса": ["alice"]}, None).endswith("|dict-only")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'ner_cache_tag_none' -v`
Expected: FAIL — `test_ner_cache_tag_none_uses_metadata_backend` падает: тег оканчивается на `|dict-only`.

- [ ] **Step 3: Implement the branch**

Заменить тело `_ner_cache_tag` на:

```python
def _ner_cache_tag(syn: dict[str, list[str]], nlp: Any) -> str:
    """Cache tag tying a row to BOTH the dictionary and the NER backend.

    A row computed with `nlp=None` (dictionary only) must never be served once
    spaCy becomes available, and vice versa: the two produce different sets, and
    mixing them would silently change which co_mentions edges get written.

    With no loaded model the backend comes from the installed package metadata
    (`_ner_backend_tag`), so the nightly pass computes the same tag a spaCy-loaded
    process would, without paying 546 MB of resident torch to learn it.
    """
    if nlp is None:
        backend = _ner_backend_tag() or "dict-only"
    else:
        meta = getattr(nlp, "meta", None) or {}
        backend = f"{meta.get('name', 'ner')}-{meta.get('version', '?')}"
    return f"{_ner_dict_fingerprint(syn)}|{backend}"
```

- [ ] **Step 4: Run the whole tag suite**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'ner' -v`
Expected: PASS — включая существующие `test_ner_cache_tag_separates_dict_only_from_ner_backend` и `test_entities_cached_invalidated_by_dictionary_change`.

- [ ] **Step 5: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
git add lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
git commit -m "feat(graph): night pass reads the NER tag without loading spaCy"
```

---

### Task 3: `miner_entities` больше не зовёт `_get_ner`

**Covers:** [S1, S3, S5]

**Files:**
- Modify: `lifecycle/graph_miners.py:498-540` (`miner_entities`)
- Test: `tests/test_lifecycle/test_graph_miners.py`

**Interfaces:**
- Consumes: `_ner_backend_tag`, `_ner_cache_tag`, `_entities_read`.
- Produces: `miner_entities(cm, layer, *, read)` — сигнатура неизменна; `nlp` внутри всегда `None`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_miner_entities_does_not_import_spacy(db, monkeypatch):
    """Ночной майнер не имеет права тянуть spaCy/torch в процесс-лидер."""
    import asyncio

    import lifecycle.graph_miners as gm

    monkeypatch.setattr(gm, "_get_ner", lambda: (_ for _ in ()).throw(AssertionError("_get_ner must not be called")))
    conn = await connection_manager.get(DB_NAME)
    for i in range(3):
        await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) "
            "VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
            (f"Алиса и Борис текст {i}", time.time()),
        )
    await conn.commit()

    apply = await gm.miner_entities(lambda layer: None, "user", read=conn)
    assert apply is not None
    assert "spacy" not in sys.modules
    assert "torch" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -k 'does_not_import_spacy' -v`
Expected: FAIL — `AssertionError: _get_ner must not be called`.

- [ ] **Step 3: Implement**

В `miner_entities` заменить строку `nlp = _get_ner()` на:

```python
    # No spaCy here on purpose: importing it costs 546 MB RSS that stays resident
    # for the life of this process. The tag still names the NER backend (read from
    # the model's meta.json), so a cache warmed by `warm_ner_cache.py` is hit; the
    # misses this pass sees are written dictionary-only and filled by the warmer.
    nlp = None
```

- [ ] **Step 4: Run the miner tests**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_lifecycle/test_graph_miners.py -v`
Expected: PASS, все тесты файла.

- [ ] **Step 5: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
git add lifecycle/graph_miners.py tests/test_lifecycle/test_graph_miners.py
git commit -m "fix(graph): the nightly entity miner no longer loads spaCy in-process"
```

---

### Task 4: Воркер и таймер на три контура

**Covers:** [S3, S6]

**Files:**
- Modify: `scripts/warm_ner_cache.py` (печать бэкенда — только диагностика)
- Test: `tests/test_scripts/test_warm_ner_cache_tag.py` (создать)
- Create: `~/.config/systemd/user/ariel-ner-warm@.service`
- Create: `~/.config/systemd/user/ariel-ner-warm@.timer`

**Interfaces:**
- Consumes: `gm._ner_cache_tag(syn, nlp)` из Task 2.
- Produces: templated юнит `ariel-ner-warm@<contour>.service/.timer`, `EnvironmentFile=~/.config/ariel-autohooks/<contour>.env` не требуется — контур передаётся через `%i` и `MCP_MEMORY_DATA_DIR`.

- [ ] **Step 1: Write the failing test**

Создать `tests/test_scripts/test_warm_ner_cache_tag.py`:

```python
"""Warm-воркер и ночной проход обязаны считать один и тот же тег.

Тег из метаданных модели (ночной путь) и тег с загруженной spaCy (воркер) должны
совпадать, иначе прогретые строки промахнутся и воркер будет молча бесполезен.
"""

import sys

import pytest


@pytest.mark.skipif(
    "spacy" not in sys.modules and __import__("importlib").util.find_spec("spacy") is None,
    reason="spaCy is not installed",
)
def test_nightly_tag_equals_loaded_spacy_tag():
    import spacy

    import lifecycle.graph_miners as gm

    nlp = spacy.load("en_core_web_sm")
    assert gm._ner_cache_tag({"алиса": ["alice"]}, None) == gm._ner_cache_tag({"алиса": ["alice"]}, nlp)
```

- [ ] **Step 2: Run test to verify it fails (Task 2 ещё не даёт равенства)**

Run: `cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process && .venv/bin/python -m pytest tests/test_scripts/test_warm_ner_cache_tag.py -v`
Expected: PASS после Task 2; если FAIL — вернуться к Task 2 и исправить `_ner_backend_tag`.

- [ ] **Step 3: Fix the warmer's diagnostic line**

В `scripts/warm_ner_cache.py` заменить строку с `backend`:

```python
    backend = _ner_cache_tag(syn, nlp).rsplit("|", 1)[-1]
```

Это печатает реальный тег бэкенда, который воркер записал, — чтобы расхождение с ночным было видно в логе, а не молчало.

- [ ] **Step 4: Create the systemd units**

`~/.config/systemd/user/ariel-ner-warm@.service`:

```ini
[Unit]
Description=Ariel NER cache warmer (%i)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/home/murat/mcp-ariel-memory
Environment=MCP_MEMORY_DATA_DIR=/home/murat/.mcp-ariel-memory-%i
ExecStart=/home/murat/mcp-ariel-memory/.venv/bin/python /home/murat/mcp-ariel-memory/scripts/warm_ner_cache.py
TimeoutStartSec=900
```

`~/.config/systemd/user/ariel-ner-warm@.timer`:

```ini
[Unit]
Description=Nightly Ariel NER warm (%i)

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
```

- [ ] **Step 5: Enable and verify on hermes only**

```bash
systemctl --user daemon-reload
systemctl --user enable --now ariel-ner-warm@hermes.timer
systemctl --user list-timers 'ariel-ner-warm*' --no-pager
```

Expected: таймер запланирован на 03:30. Воркер вручную **не** запускать на живой базе без прямого разрешения владельца — только таймер.

- [ ] **Step 6: Commit**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
git add scripts/warm_ner_cache.py tests/test_scripts/test_warm_ner_cache_tag.py
git commit -m "feat(scripts): warm_ner_cache prints the backend tag it wrote"
```

Юниты живут в `~/.config/systemd/user/` и коммитятся в стек отдельно (вне этого репо).

---

### Task 5: Гейты репо и приёмка раскатки

**Covers:** [S6, S7]

**Files:** без изменений кода.

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: зелёные гейты + решение о раскатке на cowagent/mimocode.

- [ ] **Step 1: Lint and types**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
.venv/bin/python -m ruff check lifecycle/graph_miners.py scripts/warm_ner_cache.py tests/test_lifecycle/test_graph_miners.py tests/test_scripts/test_warm_ner_cache_tag.py
.venv/bin/python -m mypy lifecycle/graph_miners.py
```

Expected: ruff clean; mypy без новых ошибок (сравнить с `master`).

- [ ] **Step 2: Full test suite**

```bash
cd /home/murat/mcp-ariel-memory/.worktrees/ner-out-of-process
.venv/bin/python -m pytest -q
```

Expected: 0 failed. Существующие падения, если есть, сверить с `master` — не списывать на свои изменения.

- [ ] **Step 3: Rollout gate**

После ночного прохода на hermes: сравнить `co_mentions`-рёбра и RSS MCP-сервера с замером до (1338 MB, spacy_maps=283). Критерий: рёбра не упали, spacy_maps=0.

- [ ] **Step 4: Extend to the other contours (owner decision)**

```bash
systemctl --user enable --now ariel-ner-warm@cowagent.timer
systemctl --user enable --now ariel-ner-warm@mimocode.timer
```

Только после явного «да» владельца: это третий контур, и он — память этого агента.

- [ ] **Step 5: Rollback note**

```bash
git revert <merge-sha>
systemctl --user disable --now 'ariel-ner-warm@*'
```

Базы не трогаются: тег не менялся, менялся только способ его вычисления.
