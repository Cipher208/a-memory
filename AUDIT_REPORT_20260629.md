# Аудит mcp-ariel-memory
**Дата:** 2026-06-29  
**Тип:** Полный аудит кода (18/92 файлов)  
**Метод:** Делегированный обход (subagent, 20 мин, 6 API-вызовов)

---

## P0 — Критические

### 1. `_importance_gate` crash при `layer="agent"`
- **Файл:** `mcp_server/tools_layer.py:129`
- Суть: `memory_remember()` ВСЕГДА вызывает `hooks._importance_gate({"text": value})`,
  но `_get_hooks(app, "agent")` возвращает `AgentHooks`, у которого НЕТ метода `_importance_gate`.
  Метод существует ТОЛЬКО в `UserHooks` (user_hooks.py:67).
  Любой вызов `memory_remember(layer="agent")` падает с `AttributeError`.
- **Фикс:** Добавить проверку `if hasattr(hooks, '_importance_gate')` перед вызовом.
  Или добавить заглушку `_importance_gate` в `AgentHooks`.

---

## P1 — Высокие

### 2. Backup cron — дубликат `memory.db` ×10
- **Файл:** `features/backup_cron.py:93`
- Суть: `db_files = ["memory.db", "memory.db", ... ×10]` — один файл указан 10 раз.
  Copy-paste ошибка. Каждый бэкап копирует один и тот же файл 10 раз, перезаписывая сам себя.
- **Фикс:** `db_files = ["memory.db"]`

### 3-5. Sync/await mismatch в `tools_ops.py`
- **Файл:** `mcp_server/tools_ops.py:70` — `await backup_cron.backup_now()`
  → `backup_now()` синхронный (возвращает `str`, не корутину). TypeError.
- **Файл:** `mcp_server/tools_ops.py:73` — `await backup_cron.list_backups()`
  → `list_backups()` синхронный (возвращает `list`). TypeError.
- **Файл:** `mcp_server/tools_ops.py:180` — `backup_task = backup_cron.cleanup_old()`
  → результат синхронного метода передаётся в `asyncio.gather`. TypeError.
- **Фикс:** Убрать `await` для синхронных методов. Для `cleanup_old` использовать `asyncio.to_thread()`.

### 6-7. `_get_conn()` на несуществующих методах
- **Файл:** `mcp_server/tools_ops.py:253` — `EpistemicGraph._get_conn()` — метод не существует.
  Использует `self._cm.get("memory.db")`.
- **Файл:** `mcp_server/tools_ops.py:240` — `AuditTrail._get_conn()` — метод не существует.
- **Фикс:** Использовать `self._cm.get(...)` или `await eg._cm.get("memory.db")`.

---

## P2 — Средние

### 8. Hooks выключены по умолчанию
- **Файл:** `config.py:35`
- Суть: `is_hook_enabled()` возвращает `False` по умолчанию. Все 24 хука не работают
  без ручного включения в config.yaml.
- **Фикс:** Сделать `default=True` для известных хуков.

### 9. Нет теста на agent-путь `memory_remember`
- **Файл:** `tests/test_tools_layer.py:35`
- Суть: `test_memory_remember_agent` не тестирует через реальный `memory_remember()` —
  напрямую вызывает `_get_memory`, минуя hooks и rate_limiter.
  Баг P0 не пойман тестами.
- **Фикс:** Интеграционный тест с вызовом `memory_remember(layer="agent")` через FakeApp.

### 10. Нет тестов на `AgentHooks._importance_gate`
- **Файл:** `tests/test_hooks/test_hooks.py`
- Суть: Всего 3 теста (31 строка). Нет теста на вызов `_importance_gate` для AgentHooks.
- **Фикс:** `with pytest.raises(AttributeError): ah._importance_gate(...)`

### 11. `_run_async` thread safety
- **Файл:** `hooks/agent_hooks.py:28`
- Суть: `_run_async` использует `ThreadPoolExecutor` для async-кода из синхронного контекста.
  Это может привести к параллельному доступу к одному SQLite-соединению → `database is locked`.
- **Фикс:** Отдельные соединения для `_run_async` или `asyncio.Lock` в EpistemicGraph.

---

## P3 — Низкие

### 12. Config — нет fallback при отсутствии config.yaml
- **Файл:** `config.py:22`
- Суть: `open(config_path)` падает с `FileNotFoundError` при свежей установке.
- **Фикс:** `try/except FileNotFoundError: self._data = {}`

### 13. Нет тестов для `auth.py`
- **Файл:** `tests/test_features/`
- Суть: Не тестируются `APIKeyAuth`, `BearerAuth`, `encrypt/decrypt`, `BackupCron`.
- **Фикс:** Добавить тесты на `create/verify/revoke/list_keys`.

### 14. Нет тестов для `backup_cron.py`
- **Файл:** `tests/test_features/`
- Суть: Не тестируются `backup_now/list/restore/status`.
- **Фикс:** Добавить тесты.

### 15. Нет тестов для `secrets.py`
- **Файл:** `tests/test_features/`
- Суть: Не тестируются `encrypt_json/decrypt_json`.
- **Фикс:** Добавить тесты.

---

## Статистика

| Метрика | Значение |
|---|---|
| Файлов всего | 92 |
| Прочитано | 18 |
| Багов найдено | 15 |
| P0 | 1 |
| P1 | 6 |
| P2 | 5 |
| P3 | 3 |
| Время аудита | ~20 мин |
| Метод | Subagent (delegate_task) |

---

*Сгенерировано Люси-Прайм из отчёта делегированного аудита. P0 баг требует правки в одной строке: `if hasattr(hooks, '_importance_gate'): gate = hooks._importance_gate(...)` в `tools_layer.py:129`.*