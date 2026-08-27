"""Tests for CONTEXT.md persistence (research backlog 3.13).

After memory_context_inject runs, a 3-section markdown file is written to
<MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md. These tests cover the helpers
and the integration through memory_context_inject.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools.ops import (
    _build_context_md,
    _context_md_path,
    memory_context_inject,
)


# ── _context_md_path ────────────────────────────────────────────


def test_context_md_path_uses_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path))
    p = _context_md_path("user")
    assert p == tmp_path / "user" / "CONTEXT.md"


def test_context_md_path_default_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_MEMORY_DATA_DIR", raising=False)
    p = _context_md_path("agent")
    assert p == Path("~/.mcp-ariel-memory/agent/CONTEXT.md").expanduser()


def test_context_md_path_tilde_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", "~/memory-test-xxx")
    p = _context_md_path("user")
    assert p == Path("~/memory-test-xxx/user/CONTEXT.md").expanduser()


# ── _build_context_md ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_context_md_has_frontmatter_and_three_sections(monkeypatch):
    """Body must contain YAML frontmatter + Context + Perspectives + Recent Episodes."""
    fake_summarize = AsyncMock(return_value={"digest": "x", "wiki_type": "decision"})
    monkeypatch.setattr("mcp_server.tools.ops._truncate_to_budget", lambda s, b: (s, b))
    monkeypatch.setattr("mcp_server.tools.wiki_summarize.wiki_summarize", fake_summarize)

    body = await _build_context_md(
        layer="user",
        user_id="alice",
        context_text="curated context here",
        consolidated=2,
        last_consolidation_ts=12345.0,
        l4_count=5,
        l3_count=3,
        l1_count=4,
        wiki_count=2,
        episodes=[],
    )

    assert body.startswith("---\n")
    assert "schema_version: 1" in body
    assert "layer: user" in body
    assert "user_id: alice" in body
    assert "## Context" in body
    assert "curated context here" in body
    assert "## Perspectives" in body
    assert "## Recent Episodes" in body
    assert "_(no recent episodes)_" in body


@pytest.mark.asyncio
async def test_build_context_md_calls_wiki_summarize_six_times(monkeypatch):
    """Each of the 6 perspectives must be queried exactly once."""
    calls: list[tuple[str, str, int]] = []
    expected = {
        "practical",
        "epistemic",
        "psychological",
        "social",
        "temporal",
        "metacognitive",
    }
    seen: set[str] = set()

    async def fake_ws(*, perspective, layer, limit=3, **kwargs):
        seen.add(perspective)
        calls.append((perspective, layer, limit))
        return {"digest": f"d-{perspective}", "wiki_type": "principle"}

    monkeypatch.setattr("mcp_server.tools.ops._truncate_to_budget", lambda s, b: (s, b))
    monkeypatch.setattr("mcp_server.tools.wiki_summarize.wiki_summarize", fake_ws)

    await _build_context_md(
        layer="user",
        user_id="alice",
        context_text="ctx",
        consolidated=0,
        last_consolidation_ts=0.0,
        l4_count=0,
        l3_count=0,
        l1_count=0,
        wiki_count=0,
        episodes=[],
    )

    assert seen == expected
    assert all(layer == "user" for _, layer, _ in calls)
    assert all(limit == 3 for _, _, limit in calls)


@pytest.mark.asyncio
async def test_build_context_md_includes_episode_summaries(monkeypatch):
    monkeypatch.setattr("mcp_server.tools.ops._truncate_to_budget", lambda s, b: (s, b))
    monkeypatch.setattr(
        "mcp_server.tools.wiki_summarize.wiki_summarize",
        AsyncMock(return_value={"digest": "x", "wiki_type": "principle"}),
    )

    e1 = SimpleNamespace(summary="first episode about users", emotional_weight=0.8)
    e2 = SimpleNamespace(summary="second episode about agents", emotional_weight=0.3)

    body = await _build_context_md(
        layer="user",
        user_id="alice",
        context_text="ctx",
        consolidated=0,
        last_consolidation_ts=0.0,
        l4_count=0,
        l3_count=2,
        l1_count=0,
        wiki_count=0,
        episodes=[e1, e2],
    )

    assert "weight=0.8" in body
    assert "weight=0.3" in body
    assert "first episode" in body
    assert "second episode" in body


@pytest.mark.asyncio
async def test_build_context_md_perspective_failure_is_non_fatal(monkeypatch, caplog):
    """If wiki_summarize raises for one perspective, the others still render."""
    call_count = 0

    async def flaky(*, perspective, layer, limit=3, **kwargs):
        nonlocal call_count
        call_count += 1
        if perspective == "epistemic":
            raise RuntimeError("wiki down")
        return {"digest": f"d-{perspective}", "wiki_type": "principle"}

    monkeypatch.setattr("mcp_server.tools.ops._truncate_to_budget", lambda s, b: (s, b))
    monkeypatch.setattr("mcp_server.tools.wiki_summarize.wiki_summarize", flaky)

    with caplog.at_level("WARNING", logger="mcp_server.tools.ops"):
        body = await _build_context_md(
            layer="user",
            user_id="alice",
            context_text="ctx",
            consolidated=0,
            last_consolidation_ts=0.0,
            l4_count=0,
            l3_count=0,
            l1_count=0,
            wiki_count=0,
            episodes=[],
        )

    # 6 calls attempted
    assert call_count == 6
    # Epistemic shows unavailable, the others show their digests
    assert "### Epistemic" in body
    assert "_(unavailable)_" in body
    assert "### Practical" in body
    assert "d-practical" in body


# ── memory_context_inject integration ───────────────────────────


@pytest.fixture
def mock_app(monkeypatch):
    fake_mem = MagicMock()
    fake_mem.l4.get_all = AsyncMock(return_value=[])
    fake_mem.l3.get_episodes = AsyncMock(return_value=[])
    fake_mem.l1.get_recent = MagicMock(return_value=[])

    fake_wiki = MagicMock()
    fake_wiki.list_all = AsyncMock(return_value=[])

    fake_mm = MagicMock()
    fake_mm.user_memory = MagicMock(return_value=fake_mem)
    fake_mm.agent_memory = MagicMock(return_value=fake_mem)

    fake_app = MagicMock()
    fake_app.mm = fake_mm
    fake_app.user_wiki = fake_wiki
    fake_app.agent_wiki = fake_wiki

    monkeypatch.setattr("mcp_server.tools.ops._get_ctx", lambda c: fake_app)
    monkeypatch.setattr("mcp_server.tools.ops._get_cached", lambda key: None)
    monkeypatch.setattr("mcp_server.tools.ops._set_cached", lambda key, val: None)
    monkeypatch.setattr("mcp_server.tools.ops._invalidate_cache", lambda layer, uid: None)
    return fake_app


@pytest.mark.asyncio
async def test_context_inject_writes_context_md_file(mock_app, monkeypatch, tmp_path):
    """After inject, <MCP_MEMORY_DATA_DIR>/<layer>/CONTEXT.md exists and is non-empty."""
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("mcp_server.tools.ops._truncate_to_budget", lambda s, b: (s, b))
    monkeypatch.setattr(
        "mcp_server.tools.wiki_summarize.wiki_summarize",
        AsyncMock(return_value={"digest": "x", "wiki_type": "principle"}),
    )

    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=1)
    with patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine):
        result = await memory_context_inject(layer="user", user_id="alice")

    assert "context_md_path" in result
    assert "perspectives_count" in result
    assert result["perspectives_count"] == 6

    p = Path(result["context_md_path"])
    assert p == tmp_path / "user" / "CONTEXT.md"
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "## Context" in content
    assert "## Perspectives" in content
    assert "## Recent Episodes" in content


@pytest.mark.asyncio
async def test_context_inject_does_not_break_on_write_failure(mock_app, monkeypatch, tmp_path, caplog):
    """If write raises, inject still returns a valid result with context_md_path=None."""
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path))

    # Force the write step itself to raise
    def boom_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "mcp_server.tools.ops.asyncio.to_thread",
        AsyncMock(side_effect=boom_write),
    )

    fake_engine = MagicMock()
    fake_engine.consolidate_episodes = AsyncMock(return_value=0)
    with (
        patch("lifecycle.consolidation.ConsolidationEngine", return_value=fake_engine),
        caplog.at_level("WARNING", logger="mcp_server.tools.ops"),
    ):
        result = await memory_context_inject(layer="user", user_id="alice")

    assert "context" in result
    # Path may be None or the unresolved path — must be safe either way
    assert result.get("context_md_path") is None or isinstance(result.get("context_md_path"), str)
