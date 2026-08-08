"""Tests to boost coverage for features/ modules."""

import asyncio

import pytest

# ── typed_export ──


def test_typed_export_import():
    from features.typed_export import do_backfill, do_export, do_reclassify

    assert callable(do_export)
    assert callable(do_reclassify)
    assert callable(do_backfill)


def test_typed_export_main():
    from features.typed_export import main

    assert callable(main)


# ── backup ──


def test_backup_list_and_cleanup(tmp_path):
    from features.backup import BackupManager

    bm = BackupManager(base_dir=str(tmp_path))
    asyncio.run(bm.backup("test1"))
    asyncio.run(bm.backup("test2"))
    backups = bm.list_backups()
    assert len(backups) >= 2
    assert all("name" in b for b in backups)


def test_backup_restore_not_found(tmp_path):
    from features.backup import BackupManager

    bm = BackupManager(base_dir=str(tmp_path))
    result = asyncio.run(bm.restore("nonexistent"))
    assert "error" in result


def test_backup_cleanup_old(tmp_path):
    from features.backup import BackupManager

    bm = BackupManager(base_dir=str(tmp_path))
    asyncio.run(bm.backup("old"))
    removed = bm.cleanup_old()
    assert isinstance(removed, int)


# ── audit_trail ──


def test_audit_log_and_history(tmp_path):
    from features.audit_trail import AuditTrail
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    at = AuditTrail(cm=cm)
    asyncio.run(at._init_db())
    asyncio.run(at.log("u1", "action1", layer="user", target_id="t1", details={"k": "v"}))
    asyncio.run(at.log("u1", "action2", layer="agent"))

    history = asyncio.run(at.get_history("u1"))
    assert len(history) >= 2
    assert history[0]["action"] in ("action1", "action2")

    history_filtered = asyncio.run(at.get_history("u1", action="action1"))
    assert all(h["action"] == "action1" for h in history_filtered)


def test_audit_count(tmp_path):
    from features.audit_trail import AuditTrail
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    at = AuditTrail(cm=cm)
    asyncio.run(at._init_db())
    asyncio.run(at.log("u1", "a1"))
    asyncio.run(at.log("u2", "a2"))

    assert asyncio.run(at.count("u1")) >= 1
    assert asyncio.run(at.count_all()) >= 2


def test_audit_cleanup_and_archive(tmp_path):
    from features.audit_trail import AuditTrail
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    at = AuditTrail(cm=cm)
    asyncio.run(at._init_db())
    asyncio.run(at.log("u1", "old_action"))

    removed = asyncio.run(at.cleanup_old(retention_days=0))
    assert isinstance(removed, int)

    asyncio.run(at.log("u1", "new_action"))
    archive_dir = str(tmp_path / "archive")
    result = asyncio.run(at.archive_and_prune(retention_days=0, archive_dir=archive_dir))
    assert "archived" in result
    assert "pruned" in result


# ── rate_limiting ──


def test_rate_limiter_check_and_stats(tmp_path):
    from features.rate_limiting import RateLimiter
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    rl = RateLimiter(cm=cm)
    asyncio.run(rl._init_db())

    result = asyncio.run(rl.check("u1"))
    assert result["allowed"] is True
    assert result["remaining"] > 0

    stats = asyncio.run(rl.get_stats("u1"))
    assert "requests_last_minute" in stats
    assert stats["requests_last_minute"] >= 1


def test_rate_limiter_cleanup(tmp_path):
    from features.rate_limiting import RateLimiter
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    rl = RateLimiter(cm=cm)
    asyncio.run(rl._init_db())
    asyncio.run(rl.check("u1"))
    removed = asyncio.run(rl.cleanup_old())
    assert isinstance(removed, int)


def test_connection_limiter():
    from features.rate_limiting import ConnectionLimiter

    cl = ConnectionLimiter(max_connections_per_user=2, max_total=5)

    r1 = cl.acquire("u1", "conn1")
    assert r1["allowed"] is True

    r2 = cl.acquire("u1", "conn2")
    assert r2["allowed"] is True

    r3 = cl.acquire("u1", "conn3")
    assert r3["allowed"] is False
    assert r3["reason"] == "user_limit"

    cl.release("u1", "conn1")
    r4 = cl.acquire("u1", "conn4")
    assert r4["allowed"] is True

    stats = cl.get_stats()
    assert stats["total_connections"] >= 2


def test_connection_limiter_total_limit():
    from features.rate_limiting import ConnectionLimiter

    cl = ConnectionLimiter(max_connections_per_user=10, max_total=2)
    cl.acquire("u1", "c1")
    cl.acquire("u2", "c2")
    r = cl.acquire("u3", "c3")
    assert r["allowed"] is False
    assert r["reason"] == "total_limit"


# ── agent_hooks ──


def test_agent_hooks_importance_gate():
    from hooks.agent_hooks import AgentHooks

    ah = AgentHooks("test_hooks")
    r = ah._importance_gate({"text": "error in database connection"})
    assert r["importance"] > 0.3
    assert r["bypass"] is False

    r2 = ah._importance_gate({"text": ""})
    assert r2["bypass"] is True


def test_agent_hooks_error_occurred():
    from hooks.agent_hooks import AgentHooks

    ah = AgentHooks("test_hooks")
    r = ah._error_occurred({"error": "NullPointerException"})
    assert "node_id" in r
    assert r["action"] == "error_analyzed"


def test_agent_hooks_decision_made():
    from hooks.agent_hooks import AgentHooks

    ah = AgentHooks("test_hooks")
    r = ah._decision_made({"decision": "Use async", "rationale": "performance"})
    assert "node_id" in r
    assert r["action"] == "decision_logged"


# ── wiki_manager ──


def test_wiki_add_and_list(tmp_path):
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(wiki_dir), cm=cm)
    asyncio.run(wm.init_db())

    asyncio.run(wm.add("diary", "TestEntry", "Some content", tags=["test"]))
    count = asyncio.run(wm.count())
    assert count >= 1


def test_wiki_agent_layer(tmp_path):
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    wiki_dir = tmp_path / "wiki_agent"
    wiki_dir.mkdir()
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="agent", base_dir=str(wiki_dir), cm=cm)
    asyncio.run(wm.init_db())

    path = asyncio.run(wm.add("decision_log", "Use YAGNI", "Always prefer simplicity"))
    assert path.endswith(".md")

    by_type = asyncio.run(wm.list_by_type("decision_log"))
    assert len(by_type) >= 1


def test_wiki_disabled_type_raises(tmp_path):
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    wiki_dir = tmp_path / "wiki_disabled"
    wiki_dir.mkdir()
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(wiki_dir), cm=cm)
    asyncio.run(wm.init_db())

    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(wm.add("nonexistent_type", "Title", "Content"))


def test_wiki_enabled_types(tmp_path):
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    wiki_dir = tmp_path / "wiki_types"
    wiki_dir.mkdir()
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(wiki_dir), cm=cm)
    types = wm.get_enabled_types()
    assert "diary" in types
    assert "relationships" in types


def test_wiki_list_and_count(tmp_path):
    from shared.connection import AsyncConnectionManager
    from wiki import WikiManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki"), cm=cm)
    asyncio.run(wm.init_db())

    asyncio.run(wm.add("diary", "Entry 1", "content"))
    asyncio.run(wm.add("relationships", "Friend", "Best friend"))

    by_type = asyncio.run(wm.list_by_type("diary"))
    assert len(by_type) >= 1

    all_entries = asyncio.run(wm.list_all())
    assert len(all_entries) >= 2

    count = asyncio.run(wm.count())
    assert count >= 2


# ── backup_cron ──


def test_backup_cron_backup_now(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    path = bc.backup_now()
    assert path is not None
    assert (tmp_path / "backups").exists()


def test_backup_cron_start_stop(tmp_path):
    import os

    from features.backup_cron import BackupCron

    os.environ.pop("BACKUP_CRON_DISABLED", None)
    bc = BackupCron(base_dir=str(tmp_path))
    bc.start()
    assert bc._running is True
    bc.stop()
    assert bc._running is False


def test_backup_cron_status():
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir="/tmp/test_cron_bc")
    status = bc.status()
    assert "running" in status
    assert "interval_hours" in status


def test_backup_cron_restore(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    bc.backup_now()
    backups = bc.list_backups()
    assert len(backups) >= 1
    result = bc.restore(backups[0]["name"])
    assert "restored" in result


def test_backup_cron_restore_not_found(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    result = bc.restore("nonexistent")
    assert "error" in result


def test_backup_cron_list_backups(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    bc.backup_now()
    bc.backup_now()
    backups = bc.list_backups()
    assert len(backups) >= 2
    assert all("name" in b for b in backups)


def test_backup_cron_status_details(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    status = bc.status()
    assert status["running"] is False
    assert status["interval_hours"] > 0
    assert status["jitter_seconds"] >= 0
    assert status["retention_days"] > 0
    assert status["backup_count"] >= 0


def test_backup_cron_state_persistence(tmp_path):
    from features.backup_cron import BackupCron

    bc = BackupCron(base_dir=str(tmp_path))
    bc.backup_now()
    assert bc._last_backup > 0

    bc2 = BackupCron(base_dir=str(tmp_path))
    assert bc2._last_backup > 0


# ── saga (additional coverage) ──


def test_saga_save_load_state(tmp_path):
    from shared import saga as saga_mod
    from shared.saga import Saga

    orig_dir = saga_mod.SAGA_DIR
    saga_mod.SAGA_DIR = tmp_path
    try:
        s = Saga("state_test")
        s.add_step("s1", lambda d: {"ok": True})
        s._saga_id = "st_1"
        s._save_state()
        assert (tmp_path / "st_1.json").exists()

        state = s._load_state("st_1")
        assert state is not None
        assert state["name"] == "state_test"
    finally:
        saga_mod.SAGA_DIR = orig_dir


def test_saga_cleanup_state(tmp_path):
    from shared import saga as saga_mod
    from shared.saga import Saga

    orig_dir = saga_mod.SAGA_DIR
    saga_mod.SAGA_DIR = tmp_path
    try:
        s = Saga("cleanup_test")
        s._saga_id = "ct_1"
        s._save_state()
        assert (tmp_path / "ct_1.json").exists()

        s._cleanup_state()
        assert not (tmp_path / "ct_1.json").exists()
    finally:
        saga_mod.SAGA_DIR = orig_dir


def test_saga_compute_idempotency_key():
    from shared.saga import Saga, SagaStep

    s = Saga("idem_key")
    step = SagaStep(
        name="test_step",
        action=lambda d: {"ok": True},
        idempotency_key_fn=lambda d: f"user:{d.get('user_id', 'x')}",
    )
    key = s._compute_idempotency_key(step)
    assert key is not None
    assert len(key) == 64  # SHA-256 hex

    step_no_key = SagaStep(name="no_key", action=lambda d: {"ok": True})
    assert s._compute_idempotency_key(step_no_key) is None
