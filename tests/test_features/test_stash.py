"""D1.12 — memory stash: git-stash for the working context (L1 + scratchpad)."""

from types import SimpleNamespace

import pytest

from core.reflex import ReflexBuffer
from features import stash as st
from features.scratchpad import read_entries, write_entry


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Object with .l1 + scratchpad riding a tmp global DB."""
    from shared.connection import connection_manager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    buf = ReflexBuffer(max_size=50)
    yield SimpleNamespace(l1=buf)
    buf.clear()


async def test_save_captures_and_clears(mem):
    mem.l1.add("user", "hello context one", 0)
    mem.l1.add("assistant", "reply two", 0)
    await write_entry("su", "user", "hyp", "hypothesis A")

    out = await st.stash_save(mem, "su", "user", "proj-a")
    assert out == {"name": "proj-a", "l1_items": 2, "scratchpad_items": 1}

    assert mem.l1.size() == 0
    assert read_entries("su", "user") == []


async def test_save_empty_context_rejected(mem):
    with pytest.raises(ValueError, match="nothing to stash"):
        await st.stash_save(mem, "su", "user", "empty")
    with pytest.raises(ValueError, match="invalid stash name"):
        await st.stash_save(mem, "su", "user", "BAD NAME")


async def test_duplicate_name_rejected(mem):
    mem.l1.add("user", "some chatter", 0)
    await st.stash_save(mem, "su", "user", "proj-a")
    mem.l1.add("user", "more chatter", 0)
    with pytest.raises(ValueError, match="already exists"):
        await st.stash_save(mem, "su", "user", "proj-a")


async def test_list_is_payload_free(mem):
    mem.l1.add("user", "chatter here", 0)
    await st.stash_save(mem, "su", "user", "proj-a")
    listed = st.stash_list("su", "user")
    assert len(listed) == 1 and listed[0]["name"] == "proj-a"
    assert "l1_json" not in listed[0] and "scratchpad_json" not in listed[0]
    assert listed[0]["l1_items"] == 1 and listed[0]["scratchpad_items"] == 0


async def test_pop_restores_exactly_and_drops_row(mem):
    mem.l1.add("user", "first line", 3)
    mem.l1.add("assistant", "second line", 0)
    await write_entry("su", "user", "plan", "step one")
    await st.stash_save(mem, "su", "user", "proj-a")

    # work happens elsewhere; context is clean now
    assert mem.l1.size() == 0

    out = await st.stash_pop(mem, "su", "user", "proj-a")
    assert out == {"name": "proj-a", "l1_items": 2, "scratchpad_items": 1}

    entries = [e.content for e in mem.l1.get_full()]
    assert entries == ["first line", "second line"]
    roles = [e.role for e in mem.l1.get_full()]
    assert roles == ["user", "assistant"]
    pad = read_entries("su", "user")
    assert [(e["key"], e["content"]) for e in pad] == [("plan", "step one")]
    assert st.stash_list("su", "user") == []  # row consumed by pop


async def test_pop_refuses_nonempty_scratchpad(mem):
    await write_entry("su", "user", "current", "unsaved work")
    mem.l1.add("user", "chatter", 0)
    await st.stash_save(mem, "su", "user", "proj-a")
    await write_entry("su", "user", "later", "new work")

    with pytest.raises(ValueError, match="stash the current context first"):
        await st.stash_pop(mem, "su", "user", "proj-a")
    # stash row still intact
    assert len(st.stash_list("su", "user")) == 1


async def test_drop(mem):
    mem.l1.add("user", "chatter", 0)
    await st.stash_save(mem, "su", "user", "proj-a")
    out = st.stash_drop("su", "user", "proj-a")
    assert out["dropped"] is True
    assert st.stash_list("su", "user") == []
    assert st.stash_drop("su", "user", "ghost")["dropped"] is False
