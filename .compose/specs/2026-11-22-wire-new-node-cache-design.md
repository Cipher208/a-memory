# wire_new_node — NER из кэша (дизайн)

**Дата:** 22.11.2026
**Автор:** Xal'Vara
**Статус:** дизайн согласован с Лили, ожидает плана.

## [S1] Проблема

T3 (PR #201) убрал spaCy из ночного майнера `miner_entities`, но пропустил вторую точку: `wire_new_node` (`lifecycle/graph_miners.py:1713`) тоже зовёт `_get_ner()`. Это **горячий путь** — `lifecycle/distiller.py:549` вызывает его на каждом дистиллированном сообщении (`hooks/agent_hooks.py:113`, `hooks/external.py:592`).

Следствие: даже на merged-коде spaCy (546 MB torch, `dlopen` не выгружает) может загрузиться **днём**, в процессе-лидере контура. Проверено живьём 22.11: демоны `ariel-autohooks-{hermes,mimocode}` несли 220 spacy-маппингов каждый, потому что жили с кода до T3.

## [S2] Замер качества (почему наивный dict-only не годится)

200 реальных узлов базы mimocode, живой прогон:

| вариант | co_mention пар | сохранено от spaCy |
|---|---|---|
| с spaCy (текущее поведение) | 1240 | 100% |
| dict-only (убрать nlp совсем) | 1176 | 94.8% (−64 пары) |
| **тёплый кэш воркера** | **1240** | **100%** |
| холодный кэш (без воркера) | 61 | 4.9% |

Теги совпадают байт-в-байт: `87d36d89f55d49f4|core_web_sm-3.8.0` (read == write при загруженной модели). Чтение 200/200.

Вывод: фиксить надо через кэш воркера, а не «просто убрать spaCy» — иначе теряется 5% рёбер.

## [S3] Решение

`wire_new_node` читает сущности через `_entities_cached(read, texts, syn, nlp=None)`:

- read-tag из метаданных (`_ner_backend_tag`) → находит строки, прогретые `warm_ner_cache.py`;
- промахи → dict-only (синонимы + канонизация работают всегда), пишутся под `|dict-only`;
- ночной `ariel-ner-warm.timer` (03:30) добирает их spaCy-моделью.

Один вызов `_entities_read` на все тексты (целевой узел + кандидаты) вместо N вызовов `_entities`.

## [S4] Границы

В scope:
- `lifecycle/graph_miners.py` — `wire_new_node`;
- `tests/test_lifecycle/test_graph_miners.py` — 4 новых теста.

Вне scope:
- `miner_entities` (уже покрыт T3);
- `lifecycle/distiller.py`, `scripts/warm_ner_cache.py`, таймеры;
- схема `ner_cache`, теги.

## [S5] Тесты (TDD)

1. `test_wire_new_node_does_not_import_spacy` — `_get_ner` не зовётся; spacy/torch не в `sys.modules`.
2. `test_wire_new_node_uses_warm_cache` — прогретая строка под backend-тегом даёт `co_mentions` без spaCy.
3. `test_wire_new_node_cold_cache_writes_dict_only` — промах пишет `|dict-only`, не отравляет spaCy-тег.
4. `test_wire_new_node_co_mentions_from_dict_entities` — dict-only сущности (синонимы) тоже дают рёбра.

## [S6] Ошибки и деградация

- Воркер не запускался / кэш пуст → dict-only рёбра, новые узлы добираются ночью; граф не ломается.
- Кэш под `|dict-only` не читается ночным проходом (он ищет backend-тег) → промахи не отравляют spaCy-строки.
- Схема `ner_cache` отсутствует → `_entities_read` работает как холодный кэш (проверено: `test_entities_read` не DDL-ит).

## [S7] Откат

`git revert` + рестарт демонов контуров. Схема не менялась, кэш остаётся валидным.
