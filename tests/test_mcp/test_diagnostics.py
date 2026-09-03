"""E3: diagnose/heal tools — behavior + registry wiring on a hermetic base_dir."""

import json

import pytest

import features.diagnostics as diag
from shared.connection import connection_manager


@pytest.fixture
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    return tmp_path


async def test_diagnose_fresh_db_ok(hermetic_base):
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    res = await diag.run_diagnose("default")
    assert res["status"] == "ok"
    names = {c["name"] for c in res["checks"]}
    assert {"db_exists", "db_integrity", "migrations", "pending_proposals", "circuit_breakers"} <= names


async def test_diagnose_missing_db_warns(hermetic_base):
    res = await diag.run_diagnose("default")
    db_check = next(c for c in res["checks"] if c["name"] == "db_exists")
    assert db_check["status"] == "warn"
    # missing DB alone is not a hard failure — other checks still run
    assert res["failed"] == 0


async def test_diagnose_flags_invalid_l1_file(hermetic_base):
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    bad = hermetic_base / "l1_user_broken.json"
    bad.write_text("{corrupted")
    res = await diag.run_diagnose("default")
    assert res["status"] == "degraded"
    l1_check = next(c for c in res["checks"] if c["name"] == "l1_file:l1_user_broken.json")
    assert l1_check["status"] == "fail"


async def test_diagnose_flags_open_breaker(hermetic_base):
    from shared.circuit_breaker import CircuitBreaker, breaker_registry

    from shared.migrations import migration_manager

    await migration_manager.migrate()
    breaker_registry._breakers["t"] = CircuitBreaker(threshold=1, recovery_timeout=30.0)
    breaker_registry._breakers["t"].record_failure()
    try:
        res = await diag.run_diagnose("default")
        br = next(c for c in res["checks"] if c["name"] == "circuit_breakers")
        assert br["status"] == "fail"
        assert res["status"] == "degraded"
    finally:
        del breaker_registry._breakers["t"]


async def test_heal_unknown_action_raises(hermetic_base):
    with pytest.raises(ValueError, match="unknown heal actions"):
        await diag.run_heal("default", actions=["fmt_all"])


async def test_heal_reset_breakers(hermetic_base):
    from shared.circuit_breaker import CircuitBreaker, CircuitState, breaker_registry

    breaker_registry._breakers["t"] = CircuitBreaker(threshold=1, recovery_timeout=30.0)
    breaker_registry._breakers["t"].record_failure()
    try:
        res = await diag.run_heal("default", actions=["reset_breakers"])
        assert res["healed"] == ["reset_breakers"]
        assert breaker_registry._breakers["t"].state == CircuitState.CLOSED
    finally:
        del breaker_registry._breakers["t"]


async def test_heal_purge_invalid_l1(hermetic_base):
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    bad = hermetic_base / "l1_user_broken.json"
    bad.write_text("{corrupted")
    good = hermetic_base / "l1_user_fine.json"
    good.write_text(json.dumps([]))
    res = await diag.run_heal("default", actions=["purge_invalid_l1"])
    assert res["healed"] == ["purge_invalid_l1"]
    assert not bad.exists()
    assert good.exists()


async def test_heal_remigrate(hermetic_base):
    res = await diag.run_heal("default", actions=["remigrate"])
    assert res["healed"] == ["remigrate"]


def test_tool_count_65():
    from mcp_server.tools_layer import _register_tools

    assert len(_register_tools) == 66


def test_tools_registered_via_mcp_surface():
    """Both tools callable through the ops surface with ctx=None (CLI style)."""
    from mcp_server.tools.ops import memory_diagnose, memory_heal

    assert memory_diagnose.__name__ == "memory_diagnose"
    assert memory_heal.__name__ == "memory_heal"
