"""Tests for FileWiki path traversal prevention."""

import pytest

from wiki.manager import WikiManager


@pytest.fixture
def wiki(tmp_path):
    return WikiManager(layer="user", base_dir=str(tmp_path / "wiki"))


@pytest.mark.asyncio
async def test_update_rejects_traversal(wiki):
    with pytest.raises(ValueError, match="escapes base directory"):
        await wiki.update("../../etc/passwd", content="pwned")


@pytest.mark.asyncio
async def test_get_rejects_traversal(wiki):
    result = await wiki.get("../../etc/passwd")
    assert result is None


@pytest.mark.asyncio
async def test_delete_rejects_traversal(wiki):
    with pytest.raises(ValueError, match="escapes base directory"):
        await wiki.delete("../../etc/passwd")


@pytest.mark.asyncio
async def test_update_rejects_absolute_path(wiki):
    with pytest.raises(ValueError, match="escapes base directory"):
        await wiki.update("/etc/passwd", content="pwned")


@pytest.mark.asyncio
async def test_get_rejects_absolute_path(wiki):
    result = await wiki.get("/etc/passwd")
    assert result is None


@pytest.mark.asyncio
async def test_delete_rejects_absolute_path(wiki):
    with pytest.raises(ValueError, match="escapes base directory"):
        await wiki.delete("/etc/passwd")
