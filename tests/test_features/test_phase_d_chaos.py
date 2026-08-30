"""Phase D chaos/edge — time boundaries, malformed inputs, races, caps, limits.

No new deps: time stays real (boundary timestamps computed relative to now),
fs chaos rides tmp_path, races ride asyncio.gather. Malformed YAML must
degrade to an empty ruleset/schema-set with a warning, never a crash.
"""

import asyncio
import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def chaos_db(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    db = tmp_path / "memory.db"
    now = time.time()
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE core_memory ("
            " entry_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', key TEXT NOT NULL, value TEXT NOT NULL,"
            " importance REAL DEFAULT 0.5, memory_kind TEXT, expires_at REAL,"
            " source TEXT DEFAULT 'manual', metadata TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE episodes ("
            " episode_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', summary TEXT NOT NULL,"
            " emotional_weight REAL DEFAULT 0.5, tags TEXT, memory_kind TEXT, created_at REAL NOT NULL)"
        )
        for i in range(30):
            conn.execute(
                "INSERT INTO core_memory (user_id, key, value, importance, created_at, updated_at) VALUES ('u1', ?, ?, 0.5, ?, ?)",
                (f"k{i}", f"val {i}", now - i, now - i),
            )
        conn.commit()
    yield db
    connection_manager.base_dir = original
    await connection_manager.close_all()


class _FakeL3:
    def __init__(self, by_tag=None):
        self._by_tag = by_tag or {}

    async def search_by_tag(self, user_id, tag, limit=10):
        return self._by_tag.get(tag, [])


class _FakeL4:
    async def get_all(self, user_id, limit):
        return []


class _FakeMem:
    def __init__(self, by_tag=None):
        self.l1 = type("R", (), {"get_recent": staticmethod(lambda n: [])})()
        self.l3 = _FakeL3(by_tag)
        self.l4 = _FakeL4()


def _ep(summary, age_s):
    return type("E", (), {"summary": summary, "created_at": time.time() - age_s})()


# ── time boundaries ──


@pytest.mark.asyncio
async def test_diff_gap_boundary_24h(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        from features.continuity import session_recap

        mem = _FakeMem(by_tag={"diff_gap": [_ep("gap fresh", 86_399), _ep("gap stale", 86_401)]})
        blocks = await session_recap(mem, "u1")
        pending = next(b for b in blocks if b["axis"] == "recap_pending")
        assert "diff_gaps: 1" in pending["content"]
    finally:
        connection_manager.base_dir = original


@pytest.mark.asyncio
async def test_recap_zero_and_negative_budget(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        from features.continuity import session_recap

        mem = _FakeMem(by_tag={"auto_save": [_ep("digest", 60)], "diff_gap": [_ep("gap", 60)]})
        assert await session_recap(mem, "u1", budget=0) == []
        assert await session_recap(mem, "u1", budget=-5) == []
    finally:
        connection_manager.base_dir = original


# ── caps and limits ──


@pytest.mark.asyncio
async def test_query_limit_clamped(chaos_db):
    from features.query_dsl import query_memory

    res = await query_memory("u1", limit=1000)
    assert res["count"] == 30 and res["filters"]["limit"] == 200
    res = await query_memory("u1", limit=0)
    assert res["count"] == 1  # clamped up to 1


@pytest.mark.asyncio
async def test_unicode_and_injection_filters_are_safe(chaos_db):
    from features.query_dsl import query_memory

    for needle in ("🍕", "кириллица", "'; DROP TABLE core_memory; --", "%_\\"):
        res = await query_memory("u1", key_like=needle)
        assert res["count"] == 0
    with sqlite3.connect(str(chaos_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM core_memory").fetchone()[0] == 30


@pytest.mark.asyncio
async def test_scratchpad_lru_cap_20(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        from features.scratchpad import read_entries, write_entry

        for i in range(25):
            await write_entry("u1", "user", f"key{i}", f"content {i}")
        entries = read_entries("u1", "user")
        assert len(entries) == 20
        keys = {e["key"] for e in entries}
        assert "key0" not in keys and "key24" in keys  # oldest evicted
    finally:
        connection_manager.base_dir = original


# ── malformed inputs must degrade, never crash ──


def test_rules_yaml_garbage_degrades_to_empty(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        import features.rules as rules_mod

        (tmp_path / "rules.yaml").write_text("{{{ not yaml :: [", encoding="utf-8")
        assert rules_mod.load_rules(force=True) == []
        out = rules_mod.apply_rules("anything release")
        assert out == {"importance_boost": 0.0, "tags": [], "matched": []}

        (tmp_path / "rules.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        assert rules_mod.load_rules(force=True) == []

        (tmp_path / "rules.yaml").write_text('rules:\n  - name: no-when\n  - name: ok\n    when_content_contains: ["x"]\n', encoding="utf-8")
        assert len(rules_mod.load_rules(force=True)) == 1
    finally:
        connection_manager.base_dir = original


def test_malformed_schema_yaml_skipped(tmp_path, monkeypatch):
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    try:
        schemas = tmp_path / "schemas"
        schemas.mkdir()
        (schemas / "bad.yaml").write_text("{{{", encoding="utf-8")
        (schemas / "nonschema.yaml").write_text("- a\n- b\n", encoding="utf-8")
        (schemas / "good.yaml").write_text("habit:\n  name: {required: true}\n", encoding="utf-8")
        from features.typed_memory import available_schemas

        avail = available_schemas()
        assert "habit" in avail and "decision" in avail  # builtin unaffected
    finally:
        connection_manager.base_dir = original


def test_compress_huge_log_bounded():
    from features.compress_output import compress_log

    log = "\n".join(f"step {i} ok" if i % 10 else f"FAILED step {i}" for i in range(50_000))
    out = compress_log(log, max_lines=20)
    assert len(out.splitlines()) == 21
    assert "truncated" in out


# ── races: concurrent writes + queries on one real DB ──


@pytest.mark.asyncio
async def test_concurrent_remember_and_query(chaos_db):
    from core.memory import CoreMemory
    from features.query_dsl import query_memory

    core = CoreMemory(layer="user")
    await core._init_db()

    async def _save(i: int) -> int:
        return await core.save("u1", f"race{i}", f"value {i}", 0.6)

    async def _query() -> int:
        res = await query_memory("u1", limit=200)
        return res["count"]

    saves, counts = await asyncio.gather(
        asyncio.gather(*(_save(i) for i in range(10))),
        asyncio.gather(*(_query() for _ in range(5))),
    )
    assert len(saves) == 10 and all(isinstance(c, int) and c >= 30 for c in counts)
    res = await query_memory("u1", key_like="race", limit=200)
    assert res["count"] == 10  # all concurrent writes landed
