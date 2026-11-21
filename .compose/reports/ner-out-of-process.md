---
feature: ner-out-of-process
status: delivered
specs:
  - .compose/specs/2026-11-21-ner-out-of-process-design.md
plans:
  - .compose/plans/2026-11-21-ner-out-of-process.md
branch: feat/ner-out-of-process
commits: 2bda5e9..4be9a33
---

# NER out-of-process — Final Report

## What Was Built

Ночной майнер сущностей больше не импортирует spaCy в процессе-лидере. Тег кэша `ner_cache` вычисляется из метаданных установленного пакета модели (`meta.json`), а не из загруженного `nlp.meta` — поэтому ночной проход находит строки, прогретые воркером, не поднимая 546 MB torch, которые остаются резидентными навсегда.

Добор промахов делает `warm_ner_cache.py` — короткоживущий процесс по systemd-таймеру на каждом контуре (hermes, cowagent, mimocode). Он один грузит spaCy, дозаполняет кэш и умирает.

## Architecture

- `_ner_backend_tag()` — читает `<site-packages>/en_core_web_sm/meta.json` через `importlib.util.find_spec` (не импортируя spaCy). Возвращает `core_web_sm-3.8.0` или `None`.
- `_ner_cache_tag(syn, nlp)` — при `nlp is None` берёт бэкенд из метаданных; иначе из `nlp.meta`. Это **read tag**.
- `_entities_read` разводит два тега: `tag` (read, из метаданных) и `write_tag` (что реально считалось: `dict-only` при `nlp=None`). Запись dict-only под spaCy-тегом отравляла бы кэш — warm-воркер читал бы пустое множество как валидное.
- `miner_entities` — `nlp = None` навсегда. Тег читается из метаданных, misses пишутся dict-only.
- `ariel-ner-warm@.service/.timer` — templated user-юниты, `OnCalendar 03:30`, `Persistent=true`, `RandomizedDelaySec=600`, по одному на контур.

### Design Decisions

- **Тег из метаданных, а не из БД/файла** — нет нового состояния, которое может разойтись. `meta.json` и `nlp.meta` дают байт-в-байт одинаковую строку (проверено).
- **Read tag ≠ write tag** — компромисс: ночной проход обязан находить прогретые строки (read по бэкенду), но не имеет права писать dict-only под этим же тегом. Разведение стоило того, что dict-only режим (`entity_mining_ner: false`) больше не перечитывает свои строки — но парс словаря без spaCy стоит 0.025 s на 2000 текстов, так что цена нулевая.
- **Таймер, а не демон** — воркер живёт секунды; держать spaCy в памяти незачем.
- **Три контура сразу** — решение владельца: таймеры включены на hermes, cowagent, mimocode.

## Usage

```bash
# статус таймеров
systemctl --user list-timers 'ariel-ner-warm*'

# ручной прогон на контуре (осторожно: до 900 s на hermes)
systemctl --user start ariel-ner-warm@hermes.service

# диагностика: что прогрето
tail -5 ~/.mcp-ariel-memory-hermes/logs/memory.log
```

## Verification

- Полный прогон: **2884 passed** (было 2874 до T0; +10 новых тестов).
- `mypy lifecycle/graph_miners.py` → Success.
- `ruff check` / `ruff format --check` → чисто.
- Слепая верификация каждого пакета: T0 `FIX_CONFIRMED`, T1 `FIX_CONFIRMED` (после T1-fix), T2 `FIX_CONFIRMED` (после T2-fix + T2-fix2), T3 `FIX_CONFIRMED`.
- Живые инварианты, проверенные вживую:
  - `read tag == loaded spaCy tag` — байт-в-байт;
  - тёплые строки воркера дают `co_mentions` без импорта spaCy в майнере;
  - отравление кэша закрыто: write `|dict-only`, read `|core_web_sm-3.8.0`;
  - `_ner_backend_tag()` не добавляет spacy/torch в `sys.modules`.
- Живые базы не тронуты: все прогоны в `/tmp`, mtime `~/.mcp-ariel-memory*` не менялись от тестов.

## Journey Log

> Для будущих.

- [lesson] CI был зелёным, а 3 теста падали локально: в CI нет spaCy-моделей → `pytest.skip`, а не проверка. Красный baseline пришлось чинить до фичи (T0).
- [lesson] `rss_kb > 400 MB` — proxy-метрика вместо наблюдения. Заменена на `model_loaded is True` из самого прого́на.
- [dead end] T2 в первой версии отравлял кэш: один тег на чтение и запись. Слепой верификатор поймал, я воспроизвела сама — `set()` под spaCy-тегом. Развела read/write.
- [lesson] Слепой верификатор дважды находил то, что автор считал очевидным: мутацию B (снятие spaCy-guard) и отравление кэша. Не пропускать.
- [lesson] `scripts/warm_ner_cache.py` печатал `ner_backend=dict-only` при `nlp=None`, хотя строки писал под spaCy-тегом — диагностика врала ровно там, где оператор ищет причину. NOTE добавлен.

## Source Materials

| File | Role |
|------|------|
| `.compose/specs/2026-11-21-ner-out-of-process-design.md` | Design (S1–S7) |
| `.compose/plans/2026-11-21-ner-out-of-process.md` | Plan (Task 0–5) |
| `~/.config/systemd/user/ariel-ner-warm@.{service,timer}` | Live units |
| `scripts/install-ner-warm-timers.sh` | Idempotent installer |
