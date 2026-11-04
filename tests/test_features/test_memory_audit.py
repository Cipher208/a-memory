"""H2 memory_audit: content_checks — conflict reader + dup/stale/dead-link checks.

audit_content() reads the orphan memory_conflicts table (writer-only until now),
detects same-kind duplicates via smart_similarity, stale un-recalled facts and
dead [[fact:key]] links in wiki content.
"""

import asyncio
import sqlite3
import time

import pytest

from rag.conflict import ConflictResolver, smart_similarity
from shared.connection import connection_manager


@pytest.fixture
async def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()  # cached conns pin the old tmp dir
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


def _fact(db, key: str, value: str, *, kind: str = "fact", age_days: float = 0.0, user_id: str = "u1") -> None:
    now = time.time() - age_days * 86400
    with sqlite3.connect(str(db / "memory.db")) as conn:
        conn.execute(
            "INSERT INTO core_memory (layer, user_id, key, value, importance, memory_kind, created_at, updated_at)"
            " VALUES ('user', ?, ?, ?, 0.5, ?, ?, ?)",
            (user_id, key, value, kind, now, now),
        )


async def test_audit_finds_all_four_problem_types(audit_db):
    # (a) contradiction — real write path: two similar facts through the resolver
    resolver = ConflictResolver()
    await resolver.check("u1", "user works at Acme corp")
    res = await resolver.check("u1", "user works at Acme corp office")
    assert res["is_conflict"] is True

    # (b) duplicate: same kind, different keys, near-identical values
    v1, v2 = "Meeting with Anna every Monday morning", "Meeting with Anna every Monday mornings"
    assert smart_similarity(v1, v2) > 0.85
    _fact(audit_db, "color.theme", v1, kind="preference")
    _fact(audit_db, "color.theme2", v2, kind="preference")

    # (c) stale: >90d, archivable kind, never recalled
    _fact(audit_db, "old.note", "some stale note", age_days=100)
    # protected: never_archive kind must not be flagged
    _fact(audit_db, "rule.important", "do not deploy on friday", kind="rule", age_days=100)
    # recalled: recall_useful audit row must not be flagged
    _fact(audit_db, "old.but.hot", "hot despite age", age_days=100)
    with sqlite3.connect(str(audit_db / "memory.db")) as conn:
        eid = conn.execute("SELECT entry_id FROM core_memory WHERE key='old.but.hot'").fetchone()[0]
        conn.execute(
            "INSERT INTO audit_log (user_id, action, layer, target_id, details, timestamp) VALUES ('u1', 'recall_useful', 'core_memory', ?, '{}', ?)",
            (str(eid), time.time()),
        )
    # fresh fact — must not be flagged
    _fact(audit_db, "fresh.note", "written today")

    # (d) dead link: [[fact:X]] where X is not a core_memory key
    now = time.time()
    with sqlite3.connect(str(audit_db / "memory.db")) as conn:
        conn.execute(
            "INSERT INTO wiki_index (layer, wiki_type, title, file_path, content, created_at, updated_at)"
            " VALUES ('user', 'concept', 'Notes', 'notes', 'see [[fact:ghost.key]] and [[fact:old.note]]', ?, ?)",
            (now, now),
        )

    from features.diagnostics import audit_content

    checks = {c["type"]: c for c in await audit_content("u1")}

    # S13 wire: file↔DB reconciliation добавил свой чек — строка 'notes'
    # (file_path='notes') не имеет файла на диске → stale index
    assert set(checks) == {"contradiction", "duplicate", "stale", "dead_link", "wiki_reconciliation"}
    assert checks["contradiction"]["severity"] == "fail"
    assert len(checks["contradiction"]["items"]) == 1
    assert "Acme" in checks["contradiction"]["items"][0]["content"]

    dup = checks["duplicate"]
    assert dup["severity"] == "warn"
    assert {dup["items"][0]["a_key"], dup["items"][0]["b_key"]} == {"color.theme", "color.theme2"}

    assert [i["key"] for i in checks["stale"]["items"]] == ["old.note"]

    assert checks["dead_link"]["items"] == [{"title": "Notes", "key": "ghost.key"}]
    for c in checks.values():
        assert c["suggestion"]


async def test_audit_clean_db_returns_empty(audit_db):
    _fact(audit_db, "solo", "nothing wrong here")
    from features.diagnostics import audit_content

    assert await audit_content("u1") == []


async def test_diagnose_includes_content_checks(audit_db):
    from features.diagnostics import run_diagnose

    _fact(audit_db, "m1", "Meeting with Anna every Monday morning", kind="fact")
    _fact(audit_db, "m2", "Meeting with Anna every Monday mornings", kind="fact")

    res = await run_diagnose("u1")
    assert "content_checks" in res
    types = {c["type"] for c in res["content_checks"]}
    assert "duplicate" in types


async def test_diagnose_checks_l0_hash_chain(audit_db):
    """T12 (аудит 05.09): verify_chain ожил в diagnose — цепь целая → ok,
    подмена текста → fail."""
    from features.diagnostics import run_diagnose
    from shared.l0 import capture

    assert await capture("new_message", "user", "u1", "первая запись цепи") is not None
    res = await run_diagnose("u1")
    names = {c["name"]: c for c in res["checks"]}
    assert names["l0_hash_chain"]["status"] == "ok"

    conn = sqlite3.connect(str(audit_db / "memory.db"))
    conn.execute("UPDATE l0_journal SET text='подмена' ")
    conn.commit()
    conn.close()
    res2 = await run_diagnose("u1")
    names2 = {c["name"]: c for c in res2["checks"]}
    assert names2["l0_hash_chain"]["status"] == "fail"


async def test_audit_duplicates_does_not_freeze_event_loop(audit_db):
    """F91: 160 near-identical same-kind facts → 12 720 pairs (~1s naive CPU scan).

    The pairwise duplicate scan must run off the event loop: a heartbeat task
    ticking every 20ms must never observe a gap >= 0.5s while audit_content
    works. The first seeded pair is generated first, so the [:100] cap cannot
    cut it from items.
    """
    stem = "the user drinks dark roast coffee with oat milk and one cinnamon stick every morning"
    for i in range(160):
        _fact(audit_db, f"dup.{i:03d}", f"{stem} note {i:03d}", kind="preference")

    from features.diagnostics import audit_content

    gaps: list[float] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def heartbeat() -> None:
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = loop.time()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(heartbeat())
    try:
        findings = await audit_content("u1")
    finally:
        stop.set()
        await hb  # no dangling tasks

    assert gaps, "heartbeat never ticked"
    assert max(gaps) < 0.5, f"event loop frozen for {max(gaps):.2f}s during duplicate scan"

    dup = next((c for c in findings if c["type"] == "duplicate"), None)
    assert dup is not None, "duplicate finding missing"
    assert any({i["a_key"], i["b_key"]} == {"dup.000", "dup.001"} for i in dup["items"])
