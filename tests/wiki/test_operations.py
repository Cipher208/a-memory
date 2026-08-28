"""Tests for wiki organic operations (split / merge / retire)."""
from __future__ import annotations

import pathlib

import pytest

from shared.connection import AsyncConnectionManager
from wiki import WikiManager


@pytest.fixture
async def wm(tmp_path):
    m = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=AsyncConnectionManager(base_dir=str(tmp_path)))
    await m.init_db()
    return m


@pytest.mark.asyncio
async def test_split_creates_new_pages(wm):
    src = await wm.add("work_notes", "BigNote", "# Big\n\n## Architecture\ncontent a\n\n## Data\ncontent b")
    res = await wm.split(src, [("Architecture", "content a"), ("Data", "content b")])
    assert res["split"] == 2
    assert len(res["new_pages"]) == 2
    # new pages are searchable
    pages = await wm.list_by_type("work_notes")
    titles = {p.title for p in pages}
    assert {"Architecture", "Data"} <= titles


@pytest.mark.asyncio
async def test_split_empty_parts_error(wm):
    src = await wm.add("work_notes", "BigNote", "content")
    res = await wm.split(src, [])
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_split_missing_path_error(wm):
    res = await wm.split("nope.md", [("A", "x")])
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_merge_concatenates(wm):
    a = await wm.add("work_notes", "A", "content A")
    b = await wm.add("work_notes", "B", "content B")
    res = await wm.merge([a, b], "Merged")
    assert res["status"] == "ok"
    merged = await wm.get(res["dest"])
    assert merged is not None
    assert "content A" in merged.content
    assert "content B" in merged.content


@pytest.mark.asyncio
async def test_merge_less_than_two_error(wm):
    a = await wm.add("work_notes", "A", "content A")
    res = await wm.merge([a], "OnlyOne")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_retire_moves_and_drops_from_index(wm, tmp_path):
    p = await wm.add("work_notes", "OldPage", "old content")
    res = await wm.retire(p, reason="superseded")
    assert res["status"] == "ok"
    assert "archive_path" in res
    # gone from search
    assert await wm.get(p) is None
    # archived under _retired
    assert pathlib.Path(res["archive_path"]).exists()


@pytest.mark.asyncio
async def test_retire_missing_path_error(wm):
    res = await wm.retire("nope.md")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_retire_idempotent(wm):
    p = await wm.add("work_notes", "OldPage", "old content")
    await wm.retire(p, reason="r")
    # second retire of the same logical path: file already gone from active dir
    res = await wm.retire(p, reason="again")
    assert res["status"] in ("error", "ok")  # must not crash
