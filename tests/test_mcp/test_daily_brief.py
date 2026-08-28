"""Tests for the daily_brief tool (3-section status report)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import features.recall_telemetry as rt
from mcp_server.registry import get_all_tools
from mcp_server.server import resolve_exposure
from mcp_server.tools.brief import daily_brief
from core.session import SessionStore
from core.memory import CoreMemory
from graph.temporal import TemporalGraph
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


@pytest.fixture
def mock_recall(monkeypatch):
    """Neutralise count_recalls so the recent section doesn't hit a real table."""
    monkeypatch.setattr(rt, "count_recalls", AsyncMock(return_value=0))
    return rt.count_recalls


# ── Structure / non-fatal (mock app) ──────────────────────────────────


def _make_mock_app():
    fake_app = MagicMock()
    fake_app.mm = MagicMock()
    # _cm.get() -> conn; conn.execute() returns the same mock acting as cursor;
    # cursor.fetchall() -> [] (empty store) so sections render empty markers.
    conn = MagicMock()
    conn.fetchall = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=conn)
    fake_app.mm._cm = MagicMock()
    fake_app.mm._cm.get = AsyncMock(return_value=conn)
    fake_app.temporal = MagicMock()
    fake_app.temporal.get_recent = AsyncMock(return_value=[])
    return fake_app


@pytest.mark.asyncio
async def test_brief_returns_expected_keys(monkeypatch, mock_recall):
    app = _make_mock_app()
    monkeypatch.setattr("mcp_server.tools.brief._get_ctx", lambda c: app)
    res = await daily_brief(layer="user", user_id="u1")
    assert res["status"] == "ok"
    assert set(res.keys()) == {"status", "summary", "pending", "recent", "suggested"}


@pytest.mark.asyncio
async def test_brief_empty_store_marks_all_sections(monkeypatch, mock_recall):
    app = _make_mock_app()
    monkeypatch.setattr("mcp_server.tools.brief._get_ctx", lambda c: app)
    res = await daily_brief(layer="user", user_id="u1")
    assert "_(nothing pending)_" in res["summary"]
    assert "_(no suggested next step)_" in res["summary"]
    # recent section always carries a recall-count line even with no events
    assert any("recall calls" in line for line in res["recent"])


@pytest.mark.asyncio
async def test_brief_nonfatal_when_temporal_fails(monkeypatch, mock_recall):
    app = _make_mock_app()
    app.temporal.get_recent = AsyncMock(side_effect=RuntimeError("temporal down"))
    monkeypatch.setattr("mcp_server.tools.brief._get_ctx", lambda c: app)
    res = await daily_brief(layer="user", user_id="u1")
    assert res["status"] == "ok"
    assert "_(unavailable)_" in res["summary"]


# ── Integration with real stores ──────────────────────────────────────


@pytest.mark.asyncio
async def test_brief_sections_from_real_stores(tmp_path, monkeypatch):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    # init stores
    await CoreMemory(cm=cm)._init_db()
    ss = SessionStore(cm=cm)
    await ss._init_db()
    tg = TemporalGraph(cm=cm)
    await tg.ensure()

    # seed: a todo L4 fact
    core = CoreMemory(cm=cm)
    await core.save("u1", "k1", "ship the brief", importance=0.9, memory_kind="todo", layer="user")
    # an open (unended) session
    conn = await cm.get(DB_NAME)
    await conn.execute(
        "INSERT INTO sessions (session_id, user_id, summary, started_at) VALUES ('s1','u1','in progress', ?)",
        (0,),
    )
    await conn.commit()
    # a temporal event
    await tg.add_event("u1", "think", "decided the plan", layer="user")

    # app with real cm + real temporal
    fake_app = MagicMock()
    fake_app.mm._cm = cm
    fake_app.temporal = tg
    monkeypatch.setattr("mcp_server.tools.brief._get_ctx", lambda c: fake_app)

    res = await daily_brief(layer="user", user_id="u1")
    assert res["status"] == "ok"
    assert any("ship the brief" in line for line in res["pending"])
    assert any("decided the plan" in line for line in res["recent"])
    assert any("resume session" in line for line in res["suggested"])


# ── Registration + exposure ───────────────────────────────────────────


def test_exposure_brief_tier_includes_daily():
    names = {"think", "dream", "daily_brief", "wiki_add"}
    assert "daily_brief" in resolve_exposure("primitives,brief", names)


def test_exposure_default_excludes_daily():
    names = {"think", "dream", "daily_brief"}
    assert "daily_brief" not in resolve_exposure("primitives", names)


def test_registry_contains_daily_brief():
    assert "daily_brief" in get_all_tools()
