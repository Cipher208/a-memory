"""S6: decided rows outside the tombstone window die; gated-in pattern proposals auto-apply."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from features import staging
from shared.connection import connection_manager

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


@pytest.fixture()
def ensure_schema(fresh_dir: Path) -> Path:
    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mutation_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, kind TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'default', layer TEXT NOT NULL DEFAULT 'user',
            payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            proposed_at REAL NOT NULL, expires_at REAL NOT NULL,
            decided_at REAL, decided_by TEXT, result_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            action TEXT NOT NULL, layer TEXT, target_id TEXT, details TEXT, timestamp REAL NOT NULL
        );
    """)
    conn.close()
    return fresh_dir


def _insert_decided(base: Path, status: str, decided_age_days: float) -> int:
    conn = sqlite3.connect(base / "memory.db")
    cur = conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at, decided_at)"
        " VALUES ('session_close', 'core_write', 'u1', 'user', ?, ?, ?, ?, ?)",
        (
            json.dumps({"key": f"k{id(conn)}", "value": "v", "importance": 0.6}),
            status,
            time.time() - 86400 * 2,
            time.time(),
            time.time() - decided_age_days * 86400,
        ),
    )
    conn.commit()
    pid = int(cur.lastrowid or 0)
    conn.close()
    return pid


@pytest.mark.asyncio
async def test_purge_removes_only_old_decided(ensure_schema: Path) -> None:
    old_rej = _insert_decided(ensure_schema, "rejected", 30)
    fresh_exp = _insert_decided(ensure_schema, "expired", 3)
    old_pending = _insert_decided(ensure_schema, "rejected", 90)  # beyond window too
    conn = sqlite3.connect(ensure_schema / "memory.db")
    conn.execute(
        "INSERT INTO mutation_proposals (source, kind, user_id, layer, payload, status, proposed_at, expires_at)"
        " VALUES ('session_close', 'core_write', 'u1', 'user', ?, 'pending', ?, ?)",
        (json.dumps({"key": "keep", "value": "v", "importance": 0.6}), time.time(), time.time() + 3600),
    )
    conn.commit()
    conn.close()

    n = await staging.purge_decided_past_window()
    assert n == 2  # only the 30d and 90d decided rows; the 3d expired row is still inside the window

    conn = sqlite3.connect(ensure_schema / "memory.db")
    ids = {r[0] for r in conn.execute("SELECT id FROM mutation_proposals")}
    conn.close()
    assert old_rej not in ids and old_pending not in ids
    assert fresh_exp in ids  # inside the 14-day tombstone window — kept for dedup
    assert len(ids) == 2  # fresh_exp + the untouched pending row


class _FakeRemember:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float]] = []

    async def remember(self, key: str, value: str, importance: float) -> int:
        self.saved.append((key, value, importance))
        return 1


class _FakeMM:
    def __init__(self) -> None:
        self.mem = _FakeRemember()

    def user_memory(self, user_id: str):
        return self.mem

    def agent_memory(self, user_id: str):
        return self.mem


class _FakeApp:
    def __init__(self) -> None:
        self.mm = _FakeMM()


@pytest.mark.asyncio
async def test_auto_apply_applies_gated_in_ignores_rest(ensure_schema: Path) -> None:
    app = _FakeApp()
    # preference 0.6 — passes with the K2 0.5 threshold
    pid_pass = await staging.propose(
        "session_close",
        "core_write",
        "u1",
        "user",
        {"key": "preference:люблю", "value": "люблю компактные отчёты по итогам дня", "importance": 0.6, "memory_kind": "preference"},
    )
    # observation 0.65 — still under the 0.7 fact-class gate
    pid_fail = await staging.propose(
        "session_close",
        "core_write",
        "u1",
        "user",
        {"key": "observation:кэш", "value": "оказалось что кэш живёт после рестарта сервиса", "importance": 0.65, "memory_kind": "observation"},
    )
    # wiki_write from session_close is NOT an auto lane (only core_write applies)
    pid_wiki = await staging.propose("session_close", "wiki_write", "u1", "user", {"title": "t", "content": "c"})

    n = await staging.auto_apply_pending(app)
    assert n == 1

    conn = sqlite3.connect(ensure_schema / "memory.db")
    st = {row[0]: row[1] for row in conn.execute("SELECT id, status FROM mutation_proposals WHERE id IN (?,?)", (pid_pass, pid_fail))}
    wiki_status = conn.execute("SELECT status FROM mutation_proposals WHERE id=?", (pid_wiki,)).fetchone()[0]
    conn.close()
    assert st[pid_pass] == "applied"
    assert st[pid_fail] == "pending"
    assert wiki_status == "pending"
    assert app.mm.mem.saved and app.mm.mem.saved[0][0] == "preference:люблю"
