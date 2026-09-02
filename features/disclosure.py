"""E11: disclosure triggers — recall-side rules («when X, surface Y»). No LLM.

Mirror of C1.10 watch_rules (save-side): operator CRUD over simple
keyword→content rules, evaluated at recall/inject time. Feature-private
table via lazy _ensure_table (scratchpad family — no alembic revision).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

_TABLE = "disclosure_rules"


def _ensure_table() -> None:
    base = connection_manager.base_dir
    if not base:
        return
    db_path = Path(str(base)) / DB_NAME
    if not db_path.exists():
        return  # no schema yet — evaluation callers treat this as "no rules"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                trigger_keywords TEXT NOT NULL,
                content TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )"""
        )


def add_rule(user_id: str, name: str, trigger_keywords: list[str], content: str) -> int:
    """Create a disclosure rule. Returns its id."""
    if not name.strip() or not content.strip():
        raise ValueError("name and content are required")
    keywords = [str(k).strip().lower() for k in (trigger_keywords or []) if str(k).strip()]
    if not keywords:
        raise ValueError("at least one trigger keyword is required")
    _ensure_table()
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            f"INSERT INTO {_TABLE} (user_id, name, trigger_keywords, content, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (user_id, name.strip(), json.dumps(keywords), content.strip(), time.time()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def list_rules(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    _ensure_table()
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, name, trigger_keywords, content, enabled, created_at FROM {_TABLE} WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 200))),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "trigger_keywords": json.loads(r["trigger_keywords"]),
                "content": r["content"],
                "enabled": bool(r["enabled"]),
                "created_at": r["created_at"],
            }
        )
    return out


def set_enabled(user_id: str, rule_id: int, enabled: bool) -> bool:
    """Toggle one rule. Returns False if not found."""
    _ensure_table()
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            f"UPDATE {_TABLE} SET enabled=? WHERE user_id=? AND id=?",
            (1 if enabled else 0, user_id, int(rule_id)),
        )
        conn.commit()
        return bool(cur.rowcount)


def delete_rule(user_id: str, rule_id: int) -> bool:
    _ensure_table()
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(f"DELETE FROM {_TABLE} WHERE user_id=? AND id=?", (user_id, int(rule_id)))
        conn.commit()
        return bool(cur.rowcount)


def evaluate_disclosures(user_id: str, text: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return enabled rules whose any keyword appears in text (case-insensitive).

    Fail-soft by contract: callers (recall/inject) wrap this — but the table
    guard and read path are cheap enough to stay honest here too.
    """
    query = str(text or "").lower()
    if not query:
        return []
    _ensure_table()
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT name, content, trigger_keywords FROM {_TABLE} WHERE user_id=? AND enabled=1 ORDER BY id DESC LIMIT 100",
                (user_id,),
            ).fetchall()
    except sqlite3.Error:
        return []
    hits: list[dict[str, Any]] = []
    for r in rows:
        keywords = json.loads(r["trigger_keywords"])
        if any(k in query for k in keywords):
            hits.append({"name": r["name"], "content": r["content"]})
            if len(hits) >= max(1, int(limit)):
                break
    return hits
