"""Tests for wiki promotion pipeline (L4 rule/preference -> wiki pages)."""
from __future__ import annotations

import pytest

from core.memory import CoreMemory
from shared.connection import AsyncConnectionManager
from wiki import WikiManager


def _type_for(kind: str, layer: str) -> str:
    table = {
        ("rule", "agent"): "principle_log",
        ("rule", "user"): "work_notes",
        ("preference", "user"): "preferences",
        ("preference", "agent"): "emotional_context",
    }
    return table[(kind, layer)]


def _sanitize_title(key: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in key).strip().replace(" ", "_")


@pytest.fixture
async def setup(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="user")
    await core._init_db()
    return cm, wm, core


@pytest.mark.asyncio
async def test_type_for_maps_correctly():
    assert _type_for("rule", "agent") == "principle_log"
    assert _type_for("preference", "user") == "preferences"
    assert _type_for("rule", "user") == "work_notes"
    assert _type_for("preference", "agent") == "emotional_context"


@pytest.mark.asyncio
async def test_promotes_rule_to_agent_type(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="agent", base_dir=str(tmp_path / "wa"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="agent")
    await core._init_db()
    await core.save("u1", "always_backup", "Always back up before deploy", importance=0.9, memory_kind="rule", layer="agent")

    res = await wm.promote_from_core(cm, "agent", "u1", min_importance=0.8)
    assert res["promoted"] == 1
    pages = await wm.list_by_type("principle_log")
    assert any(p.title == "always_backup" for p in pages)


@pytest.mark.asyncio
async def test_promotes_preference_user(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="user")
    await core._init_db()
    await core.save("u1", "dark_mode", "User prefers dark mode", importance=0.9, memory_kind="preference", layer="user")

    res = await wm.promote_from_core(cm, "user", "u1", min_importance=0.8)
    assert res["promoted"] == 1
    pages = await wm.list_by_type("preferences")
    assert any(p.title == "dark_mode" for p in pages)


@pytest.mark.asyncio
async def test_idempotent_second_run_skips(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="user")
    await core._init_db()
    await core.save("u1", "dark_mode", "User prefers dark mode", importance=0.9, memory_kind="preference", layer="user")

    r1 = await wm.promote_from_core(cm, "user", "u1", min_importance=0.8)
    r2 = await wm.promote_from_core(cm, "user", "u1", min_importance=0.8)
    assert r1["promoted"] == 1
    assert r2["promoted"] == 0
    assert r2["skipped"] >= 1


@pytest.mark.asyncio
async def test_below_threshold_not_promoted(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="user")
    await core._init_db()
    await core.save("u1", "low_value", "low importance fact", importance=0.3, memory_kind="rule", layer="user")

    res = await wm.promote_from_core(cm, "user", "u1", min_importance=0.8)
    assert res["promoted"] == 0


@pytest.mark.asyncio
async def test_non_target_kind_not_promoted(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    core = CoreMemory(cm=cm, layer="user")
    await core._init_db()
    await core.save("u1", "fact_key", "just a fact", importance=0.95, memory_kind="fact", layer="user")

    res = await wm.promote_from_core(cm, "user", "u1", min_importance=0.8)
    assert res["promoted"] == 0
