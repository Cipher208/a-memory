AsyncConnectionManager - "C:\Users\Sart-\mcp-ariel-memory\shared\connection.py"

`shared/connection.py` — 120 строк. Не пул, а **ConnectionManager**:

Что Как
Один коннект на файл `get("core_memory.db")` → создаёт или возвращает
aiosqlite Уже в `pyproject.toml` как зависимость
WAL + busy_timeout + synchronous=NORMAL Каждый коннект
Хелперы `execute_script()`, `table_exists()`, `vacuum()`
Глобальный экземпляр `connection_manager` для быстрого импорта
Stale-connection check Проверяет `SELECT 1` перед возвратом
Пример миграции В комментариях — как перевести `CoreMemory` с `sqlite3` на aiosqlite





Осталось тебе (ручная работа)
**23 файла** с `sqlite3.connect()`. Конкретно — в каждом паттерн:

# ДО

def save(self, user_id, key, value):
    conn = sqlite3.connect(self.db_path)   # ← заменить
    conn.execute(...)
    conn.commit()

# ПОСЛЕ

async def save(self, user_id, key, value):
    conn = await self._cm.get("core_memory.db")   # ← aiosqlite
    await conn.execute(...)
    await conn.commit()



**Порядок:**
`core/memory.py` (CoreMemory — L4)
`core/session.py` (SessionStore — L2)
`core/episodic.py` (EpisodicMemory — L3)
`lifecycle/consolidation.py`
`lifecycle/forgetting.py`
`rag/engine.py`, `rag/conflict.py`
`wiki/file_wiki.py`, `wiki/user_wiki.py`, `wiki/agent_wiki.py`
`graph/epistemic.py`, `graph/temporal.py`
`features/audit_trail.py`, `features/rate_limiting.py`, `features/compression.py`, `features/import_export.py`, `features/backup.py`
`shared/read_only.py`, `shared/embeddings.py`, `shared/archived_memories.py`, `shared/dream_buffer.py`, `shared/migrations.py`





Первый сделан для примера

`shared/memory.py` **— 142 строк → 124 строки**

"C:\Users\Sart-\mcp-ariel-memory\shared\memory.py"

Было Стало
`import sqlite3` `from shared.connection import ...`
`def _get_conn()` — открывал новое соединение `await self._cm.get("core_memory.db")` — переиспользует
`try/finally/conn.close()` в каждом методе **Ушло** — коннект живёт в менеджере
`self.db_path` `self._cm`
`_init_db()` синхронный `async _init_db()`





**Паттерн конверсии для любого файла:**

# 1. Импорт

from shared.connection import AsyncConnectionManager, connection_manager

# 2. Конструктор — принимает cm вместо db_path

def __init__(self, cm: AsyncConnectionManager | None = None):
    self._cm = cm or connection_manager

# 3. _get_conn → self._cm.get(db_name)

conn = await self._cm.get("core_memory.db")

# 4. conn.execute → await conn.execute()

cursor = await conn.execute("SQL", params)
row = await cursor.fetchone()          # вместо .fetchone()
rows = await cursor.fetchall()         # вместо .fetchall()

# 5. conn.commit() → await conn.commit()

# 6. try/finally/conn.close() → выкинуть (коннектом управляет менеджер)

Твоя очередь. **Осталось 22 файла** — порядок любой, но я бы рекомендовала:
`core/session.py` — простой, повторяет паттерн один-в-один
`core/episodic.py` — ещё проще
`lifecycle/consolidation.py` — тут важно: он напрямую открывает `episodic.db` через `sqlite3.connect` — нужно перевести на `self._cm.get()`
`rag/engine.py` — самый сложный (BLOB embedding, RRF), его в конце
Всё остальное — механически по паттерну
