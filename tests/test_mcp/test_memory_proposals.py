"""memory_proposals tool (C1.11 S5)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

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
    conn.commit()
    conn.close()
    return fresh_dir


class _Ctx:
    def __init__(self) -> None:
        self.request_context = SimpleNamespace(lifespan_context=None)


async def _call(ensure_schema: Path, **kwargs):
    from mcp_server.tools.ops import memory_proposals

    return await memory_proposals(ctx=_Ctx(), **kwargs)


async def test_list_returns_pending(ensure_schema: Path) -> None:
    from features.staging import propose

    await propose("auto_save", "core_write", "default", "user", {"key": "k", "value": "v", "importance": 0.9})
    out = await _call(ensure_schema, action="list")
    assert out["status"] == "ok"
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["kind"] == "core_write"


async def test_unknown_action_raises(ensure_schema: Path) -> None:
    with pytest.raises(ValueError, match="unknown action"):
        await _call(ensure_schema, action="bogus")


async def test_unknown_id_raises(ensure_schema: Path) -> None:
    with pytest.raises(ValueError, match="unknown proposal"):
        await _call(ensure_schema, action="decide", proposal_id=424242, approve=True)


async def test_conflict_decision_supersede(fresh_dir: Path) -> None:
    """S18 п.8: 3-option конфликт-контракт — supersede закрывает группу."""
    from rag.conflict import ConflictResolver

    resolver = ConflictResolver()
    r1 = await resolver.check("conflict-u1", "user works at Acme corp")
    r2 = await resolver.check("conflict-u1", "user works at Acme corp office")
    assert r1["is_conflict"] is False
    assert r2["is_conflict"], "setup: конфликт создан"
    gid, old_id = r2["conflict_group_id"], r2["conflicts_with_id"]

    out = await _call(fresh_dir, action="conflict", payload={"group_id": gid, "decision": "supersede", "keep_id": old_id})
    assert out["status"] == "resolved" and out["decision"] == "supersede"
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT COUNT(*) FROM memory_conflicts WHERE conflict_group_id=?", (gid,))).fetchone()
    assert row[0] == 0, "группа закрыта"


async def test_conflict_decision_annotate(fresh_dir: Path) -> None:
    """annotate: обе записи остаются, аннотация в metadata старого факта."""
    from lifecycle.distiller import _canonical_key
    from shared.memory_types import kind_for_text
    from shared.migrations import MigrationManager

    await MigrationManager(cm=connection_manager).migrate()

    text = "deployment target is staging"
    key = _canonical_key(text, kind_for_text(text))
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=connection_manager, layer="user")
    await cmem.save("conflict-u2", key, text, importance=0.8, metadata={"scope": "earlier"})

    from rag.conflict import ConflictResolver

    resolver = ConflictResolver()
    await resolver.check("conflict-u2", text)
    r2 = await resolver.check("conflict-u2", "deployment target is production")
    assert r2["is_conflict"], "setup: конфликт создан"

    out = await _call(
        fresh_dir,
        action="conflict",
        payload={"group_id": r2["conflict_group_id"], "decision": "annotate", "annotation": "обе правды: staging для дев, prod для релиза"},
        user_id="conflict-u2",
    )
    assert out["status"] == "resolved" and out["decision"] == "annotate"
    assert out["annotated_key"] == key
    # аннотация мёржится в существующий metadata (scope=earlier выживает)
    db = await connection_manager.get("memory.db")
    row = await (await db.execute("SELECT metadata, importance FROM core_memory WHERE user_id='conflict-u2' AND key=?", (key,))).fetchone()
    import json

    meta = json.loads(row[0])
    assert meta["annotated"] == "обе правды: staging для дев, prod для релиза"
    assert meta["scope"] == "earlier", "существующий metadata мёржится, не затирается"
    assert row[1] == 0.8, "importance сохраняется"


async def test_conflict_decision_validation_errors(fresh_dir: Path) -> None:
    out = await _call(fresh_dir, action="conflict", payload={"group_id": "nope", "decision": "bogus"})
    assert out["status"] == "error"
    out2 = await _call(fresh_dir, action="conflict", payload={"group_id": "nope", "decision": "retain", "keep_id": 0})
    assert out2["status"] == "error"
    out3 = await _call(fresh_dir, action="conflict", payload={"group_id": "nope", "decision": "annotate", "annotation": ""})
    assert out3["status"] == "error"
    out4 = await _call(fresh_dir, action="conflict", payload={"group_id": "ghost-group", "decision": "annotate", "annotation": "x"})
    assert out4["status"] == "error", "неизвестная группа → error"


# ── meta-tool dispatch path (2026-09-11 incident: review tier was unusable) ──


async def test_list_without_ctx_serves_pending(ensure_schema: Path) -> None:
    """action='list' не требует MCP-контекста: CLI/прямые вызовы с ctx=None работают."""
    from features.staging import propose

    from mcp_server.tools.ops import memory_proposals

    await propose("auto_save", "core_write", "default", "user", {"key": "k-ctx-less", "value": "v", "importance": 0.9})
    out = await memory_proposals("list", ctx=None)
    assert out["status"] == "ok"
    assert any(p["payload"].get("key") == "k-ctx-less" for p in out["proposals"])


def test_dispatcher_ctx_annotation_is_context() -> None:
    """ctx диспетчера должен быть Context-типизирован: SDK инжектит только
    Context-аннотированные параметры (find_context_parameter). Any = нет инъекции."""
    import inspect

    from mcp_server.meta_tools import _make_dispatcher

    dispatcher = _make_dispatcher("review", {"memory_proposals"}, {})
    ann = inspect.signature(dispatcher).parameters["ctx"].annotation
    assert "Context" in str(ann), f"ctx annotation lost Context typing: {ann!r}"


async def test_meta_dispatch_list_without_ctx(ensure_schema: Path) -> None:
    """R1+R2 end-to-end: review(action='memory_proposals', args={'action':'list'}, ctx=None)."""
    from features.staging import propose

    from mcp_server.meta_tools import _make_dispatcher
    from mcp_server.tools.ops import memory_proposals

    await propose("auto_save", "core_write", "default", "user", {"key": "k-meta", "value": "v", "importance": 0.9})
    dispatcher = _make_dispatcher("review", {"memory_proposals"}, {"memory_proposals": memory_proposals})
    out = await dispatcher("memory_proposals", {"action": "list", "user_id": "default"}, ctx=None)
    assert out["status"] == "ok"
    assert any(p["payload"].get("key") == "k-meta" for p in out["proposals"])


def test_catalog_exposes_param_names() -> None:
    """Каталог мета-tyла показывает имена параметров члена — модели не гадают
    ('unexpected keyword argument context' был догадкой вслепую)."""
    from mcp_server.meta_tools import _catalog
    from mcp_server.tools.ops import memory_proposals

    entries = _catalog({"memory_proposals"}, {"memory_proposals": memory_proposals})
    params = entries["memory_proposals"]["params"]
    assert "proposal_id" in params
    assert "approve" in params
    assert "ctx" not in params, "ctx — серверная инъекция, в схеме клиенту не светится"
