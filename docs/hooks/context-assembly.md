# Context Assembly — контракт harness-адаптеров

> **Кому:** CowAgent / Hermes / MiMoCode-хук (все три адаптера ariel).
> **Что решает:** агент не думает об инжекте — система сама собирает контекст
> перед ответом (Автохуки.md §3, хук `context_assembly`).

## 1. Одна точка входа

```bash
ariel-py -m autohooks context --config ~/.config/ariel-autohooks/<agent>.yaml \
  --text "<текст сообщения пользователя>" --format md
```

Возврат (markdown, готовый к вставке):

```
- [semantic] <факт/эпизод, релевантный сообщению>
- [day] <что происходило за сутки>
<recent>
- <последние L1-реплики (непрерывность диалога)>
</recent>
```

`--format json` возвращает `{"relevant": [...], "recent": [...], "budget": N,
"used_tokens": M}` — для адаптеров, которые сами форматируют.

- **Стабильный критический сет** (rehydrate/important, E9 `<cache:break>`) —
  это `autohooks inject` (session-start), его адаптеры УЖЕ вызывают. `context`
  — **per-turn** дополнение, не замена.
- Бюджет: `--budget` (default 2000 токенов); protocol внутри бюджет-capped и
  dedup'нут по контенту.

## 2. Правила вставки (cache-friendly, паттерн TencentDB)

1. **L1/recent → prepend** перед user-сообщением (короткоживущий блок,
   меняется каждый ход).
2. **Релевантные блоки → обёртка `<relevant-memories>…</relevant-memories>`,
   вставляемая ПЕРЕД текущим сообщением.** Перед вставкой **вырезать
   предыдущее `<relevant-memories>` из истории** — старые реколлы не
   накапливаются (иначе история разбухает и провайдерский кэш промахивается).
3. **`<cache:break>`** внутри вывода уважать как границу стабильного префикса
   (E9) — если адаптер конкатенирует с system, ставить break только после
   стабильной части.
4. Не дублировать: если `inject` уже вставил факт в этот ход, `context`
   повтор не принесёт (dedup по контенту внутри протокола; между inject и
   context dedup — ответственность адаптера, обычно достаточно «context
   вставляется в ход, inject — на session-start»).

## 3. Offload: тяжёлые tool-логи не живут в контексте

**Вызывающая сторона** (адаптер или autohooks-daemon на tool_result):

```python
from features.offload import offload_tool_log

result = await offload_tool_log(wiki, user_id, tool_name, tool_output, summary="Self-Monitoring Report 08:05")
if result:  # лог > 4000 символов
    # в память/контекст идёт ТОЛЬКО указатель:
    pointer = f"{tool_name} лог оффлоаден ({result['chars']} симв) → {result['ref_path']}"
```

- Полный текст: `work_notes/refs-<date>-<tool>.md` (wiki-страница, FTS-поиск
  работает, Obsidian/VS Code читают).
- Граф: узел появится штатно через `wiki_graph_builder` (wiki_page-узел с
  content=file_path) — **Mermaid-канвас с node_id**, никакого bypass записи
  (F-T9 AST-инвариант).
- Drill-down: `wiki_read(path=<ref_path>)` — полный лог на месте.
- Порог 4000 символов (`features/offload.py::REFS_THRESHOLD_CHARS`).

## 4. Wiring по адаптерам (патчи — на следующий деплой, не срочно)

**CowAgent** (`agent/memory/ariel_hooks.py`): в turn-хуке перед ответом
```python
ctx_text = _run_cli(["-m", "autohooks", "context", "--config", ARIEL_CONFIG, "--text", user_message])
# → вставить ctx_text в <relevant-memories> перед сообщением (правило 2)
```
Для тяжёлых tool-outputs: после execute, если `len(output) > 4000` —
`offload_tool_log` и в память передать `pointer`, не `output`.

**Hermes** (`plugins/ariel/__init__.py`): в `prefetch` (перед ответом) —
`context --text <msg>` → merge по правилу 2; в `sync_turn` для tool-результатов
— offload-проверка. Breaker уже есть (5 fails → 60s).

**MiMoCode-хук** (`ariel-inject.ts`): per-turn hook (post-message) с
`run(["-m", "autohooks", "context", ...])` и вставкой через
`experimental.session.compacting`-подобный context.push — ИЛИ оставить
session-start inject как есть и вызывать context только на больших сессиях.
`HOOK_ENV` в хуке уже скопирован (hash scoped per-spawn).

## 5. Границы (честно)

- `context` НЕ делает AST-агрегации/семантического скора — это
  recall_protocol-оси (маркеры → сессия → семантика → expand → день). Умная
  релевантность = тема эволюции протокола, не контракта.
- Offload НЕ индексирует полный лог в rag_chunks (только wiki-FTS по странице)
  — для tool-логов достаточно; если понадобится векторный поиск по логам —
  включить ingest страницы в RAG (уже происходит через wiki ingest? нет —
  wiki-страницы идут в wiki_index + wiki FTS, rag_chunks отдельный корпус).
- Никакого LLM в сборке (детерминизм горячего пути сохранён).
