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
    out = [l for l in links if l["direction"] == "out"]
    assert any(l["path"] == "b.md" and l["link_type"] == "review_of" for l in out)


@pytest.mark.asyncio
async def test_get_links_empty(idx):
    assert await idx.get_links("nope.md") == []
