"""Tests for wiki ref-chain links (typed page-to-page relationships)."""

from __future__ import annotations

import pytest

from shared.connection import AsyncConnectionManager
from wiki.index import WikiIndex


@pytest.fixture
async def idx(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    i = WikiIndex(cm, "user")
    await i.init_db()
    return i


@pytest.mark.asyncio
async def test_add_link_idempotent(idx):
    id1 = await idx.add_link("a.md", "b.md", "review_of")
    id2 = await idx.add_link("a.md", "b.md", "review_of")
    assert id1 == id2  # same triple -> same row (INSERT OR IGNORE + SELECT)
    assert id1 > 0


@pytest.mark.asyncio
async def test_get_links_both_directions(idx):
    await idx.add_link("a.md", "b.md", "review_of")
    links = await idx.get_links("a.md")
    out = [ln for ln in links if ln["direction"] == "out"]
    assert any(ln["path"] == "b.md" and ln["link_type"] == "review_of" for ln in out)


@pytest.mark.asyncio
async def test_get_links_empty(idx):
    assert await idx.get_links("nope.md") == []


# ── Auto-linking via WikiManager ──────────────────────────────────────


@pytest.mark.asyncio
async def test_add_autolinks_wikilink(tmp_path):
    from wiki import WikiManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    target = await wm.add("diary", "Target", "target content")
    src = await wm.add("diary", "Source", "see [[Target]]")
    links = await wm.get_links(src)
    assert any(ln["path"] == target and ln["link_type"] == "follows" for ln in links)


@pytest.mark.asyncio
async def test_add_skips_unresolvable_wikilink(tmp_path):
    from wiki import WikiManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    src = await wm.add("diary", "Source", "see [[MissingPage]]")
    assert await wm.get_links(src) == []  # silently skipped, no crash


# ── wiki_link tool ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wiki_link_tool_add_and_list(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from wiki import WikiManager
    from mcp_server.tools.wiki_link import wiki_link

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    a = await wm.add("diary", "Alpha", "content")
    b = await wm.add("diary", "Beta", "content")

    fake_app = MagicMock()
    fake_app.user_wiki = wm
    fake_app.agent_wiki = wm
    monkeypatch.setattr("mcp_server.tools.wiki_link._get_ctx", lambda c: fake_app)
    monkeypatch.setattr("mcp_server.tools.wiki_link._get_wiki", lambda app, layer: wm)

    r_add = await wiki_link(layer="user", action="add", from_path=a, to_path=b, link_type="review_of", ctx=None)
    assert r_add["status"] == "ok"
    r_list = await wiki_link(layer="user", action="list", from_path=a, ctx=None)
    assert any(ln["path"] == b and ln["link_type"] == "review_of" for ln in r_list["links"])


@pytest.mark.asyncio
async def test_wiki_link_bad_action(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from wiki import WikiManager
    from mcp_server.tools.wiki_link import wiki_link

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()
    fake_app = MagicMock()
    fake_app.user_wiki = wm
    fake_app.agent_wiki = wm
    monkeypatch.setattr("mcp_server.tools.wiki_link._get_ctx", lambda c: fake_app)
    monkeypatch.setattr("mcp_server.tools.wiki_link._get_wiki", lambda app, layer: wm)
    r = await wiki_link(layer="user", action="bogus", ctx=None)
    assert r["status"] == "error"
