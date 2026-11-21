# NER out-of-process — design
> [!NOTE]
> This document may not reflect the current implementation.
> See the final report for up-to-date state:
> [Final Report](reports/ner-out-of-process.md)


**Дата:** 21.11.2026
**Статус:** дизайн согласован с Лили, ожидает плана.

## [S1] Проблема

Ночной проход графа (`graph_enrich` → `MINERS["entities"]` → `miner_entities` → `_get_ner()`) импортирует spaCy **в процессе-лидере**. `import spacy` тянет torch и nvidia/CUDA: сам импорт — 546 MB RSS, с моделью — 752 MB (задокументировано в `mcp_server/utils/privacy.py:57`). `dlopen` не выгружает библиотеки, поэтому после первого ночного прохода они остаются в памяти навсегда.

Замер 21.11.2026:

| процесс | контур | RSS | spacy-маппингов |
|---|---|---|---|
| pid 2112170 | hermes (MCP-сервер) | 1338 MB | 283 |
| pid 3925296 | cowagent (MCP-сервер) | 527 MB | 283 |
| pid 3704699 | hermes (autohooks, до рестарта) | 525 MB | 283 |
| pid 3020604 | mimocode (мой MCP-сервер) | 117 MB | 0 |

Итого ≈1.9 GB RSS — постоянная плата за ночную задачу, которая длится минуты. OOM нет, но свободной RAM 554 MB и 7 GB swap.

Попытка «отключить майнер» отвергнута: это потеря качества, которую мы строили. `scripts/warm_ner_cache.py` уже существует и греет кэш **вне** ночного бюджета, но нигде не запланирован, а ночной проход всё равно грузит spaCy — чтобы посчитать тег кэша.

## [S2] Почему нельзя просто убрать nlp

`_ner_cache_tag(syn, nlp)` (`lifecycle/graph_miners.py:579`) делает тег из отпечатка синонимов **и** бэкенда NER:

```
<dict-fp>|dict-only            ← nlp=None
<dict-fp>|core_web_sm-3.8.0    ← загруженная spaCy
```

Тег без загруженной spaCy другой → все строки `ner_cache` промахиваются, и каждая ночь переваривала бы узлы заново. Это ровно то, о чём предупреждает docstring тега: «A row computed with `nlp=None` must never be served once spaCy becomes available». Наивный фикс сломал бы кэш молча.

## [S3] Решение

Тег вычисляется из **метаданных установленного пакета модели**, без импорта spaCy и torch.

Проверено вживую:

```
en_core_web_sm/meta.json           → name=core_web_sm, version=3.8.0
spacy.load("en_core_web_sm").meta  → name=core_web_sm, version=3.8.0
importlib.util.find_spec("spacy")  → True, при этом spacy и torch НЕ в sys.modules
```

Значит ночной проход может посчитать байт-в-байт тот же тег, что и процесс с загруженной spaCy, не поднимая 750 MB.

### Компоненты

1. **`_ner_backend_tag()`** (новый, в `lifecycle/graph_miners.py`) — возвращает `str | None`:
   - `None`, если spaCy не установлен (`find_spec("spacy")` → None) или пакет модели не найден;
   - иначе `f"{meta['name']}-{meta['version']}"`, где `meta` — JSON из `<site-packages>/<model>/meta.json`.
   - Модель определяется константой `_NER_MODEL = "en_core_web_sm"` (та же, что грузит `_get_nlp`).

2. **`_ner_cache_tag(syn, nlp)`** — сигнатура и поведение не меняются. Добавляется ветка: если `nlp is None`, backend берётся из `_ner_backend_tag()`; если и там None — `dict-only` (как сейчас).

3. **`miner_entities`** — `nlp = _get_ner()` заменяется на `nlp = None` + вычисление тега. Майнер больше не импортирует spaCy ни при каких условиях.

4. **`_entities_read`** — без изменений: читает кэш по тегу, для misses вызывает `_entities(text, syn, nlp)` с `nlp=None`, то есть даёт **dict-only** набор (синонимы + канонизация работают всегда, NER-слой отсутствует до добора воркером).

5. **`scripts/warm_ner_cache.py`** — без изменений по логике; он грузит spaCy в своём процессе, дозаполняет misses и умирает.

6. **`ariel-ner-warm.timer`** (новый user-юнит) — раз в сутки, до ночного прохода, по одному на контур (hermes, cowagent, mimocode). Каждый запуск — `scripts/warm_ner_cache.py` с нужным `MCP_MEMORY_DATA_DIR`.

### Поток данных

```
ночь:  graph_enrich → miner_entities
         ├─ _ner_backend_tag()  ← meta.json (0 импортов)
         ├─ чтение ner_cache по тегу → хиты
         └─ misses → _entities(..., nlp=None) → dict-only
                              ↓
день:  ariel-ner-warm.timer → warm_ner_cache.py
         └─ spaCy в отдельном процессе → misses добираются → процесс умирает
```

## [S4] Обработка ошибок и деградация

- spaCy не установлен → `_ner_backend_tag()` → None → тег `dict-only`. Ровно сегодняшнее поведение, без исключений.
- Пакет модели не установлен → None → `dict-only`.
- `meta.json` битый/отсутствует → `logger.warning` с путём, fallback `dict-only`. Проход не падает.
- Warm-воркер упал → ночной проход всё равно работает на dict-only; misses остаются незакрытыми до следующего запуска таймера. Это видно в отчёте `miner:entities` (число узлов) и в логе ворера.
- Таймер выключен → деградация до dict-only со временем, но **без поломки**: граф продолжает строиться.

## [S5] Тесты (TDD, порядок)

1. `test_ner_backend_tag_matches_loaded_spacy` — вычислить тег из `meta.json`, сравнить с тегом процесса, где spaCy загружена;-equal. Помечен как требующий установленной модели (skip, если нет).
2. `test_ner_backend_tag_is_none_without_spacy` — `find_spec` → None ⇒ тег `dict-only`, без исключений.
3. `test_miner_entities_never_imports_spacy` — прогнать `miner_entities` и убедиться, что `spacy`/`torch` не в `sys.modules`.
4. `test_entities_read_misses_are_dict_only` — при пустом кэше misses дают dict-only набор и пишутся с тегом бэкенда.
5. `test_warm_cache_tag_matches_nightly_tag` — warm-скрипт и ночной проход дают одинаковый тег (иначе добор не закроет misses).

## [S6] Раскатка

1. Реализация + тесты зелёные (гейты репо: ruff, mypy, pytest).
2. Раскатать `ariel-ner-warm` **только на hermes**, прогнать вручную, замерить RSS MCP-сервера до/после следующего ночного прохода.
3. Если RSS упал и граф не деградировал — включить таймер на cowagent и mimocode.
4. Проверка качества: число `co_mentions`-рёбер до/после не должно упасть относительно кэша.

## [S7] Откат

Один `git revert` + `systemctl --user disable --now ariel-ner-warm.timer` (×3). Базы не трогаются, кэш `ner_cache` остаётся валидным: тег не менялся, менялся только способ его вычисления.
