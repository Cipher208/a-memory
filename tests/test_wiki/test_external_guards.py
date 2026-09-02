"""E7: least-privilege roots — external dirs can't capture the data dir; restore can't escape."""

import pytest

from shared.connection import connection_manager


@pytest.fixture
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    return tmp_path


async def test_sync_external_rejects_dir_inside_base_dir(hermetic_base):
    from wiki.manager import WikiManager

    inner = hermetic_base / "wiki-pages"
    inner.mkdir()
    (inner / "a.md").write_text("# A")
    wm = WikiManager(layer="user", base_dir=str(hermetic_base / "wiki_u"))
    with pytest.raises(ValueError, match="inside the data directory"):
        await wm.sync_external([str(inner)])


async def test_sync_external_still_reads_outside_dirs(tmp_path, monkeypatch):
    """A dir outside the data root must still be processed normally."""
    from wiki.manager import WikiManager

    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(connection_manager, "base_dir", data_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.md").write_text("---\ntitle: B\n---\n# B\n")
    wm = WikiManager(layer="user", base_dir=str(data_root / "wiki_u"))
    res = await wm.sync_external([str(outside)])
    assert res["imported"] + res["skipped"] + res["errors"] >= 1  # dir processed, not rejected


async def test_sync_external_rejects_symlink_alias_of_base_dir(hermetic_base):
    """A symlink pointing at the data dir is the same capture — resolve() must catch it."""
    from wiki.manager import WikiManager

    (hermetic_base / "memory.db").touch()
    alias = hermetic_base / "alias"
    alias.symlink_to(hermetic_base, target_is_directory=True)
    wm = WikiManager(layer="user", base_dir=str(hermetic_base / "wiki_u"))
    with pytest.raises(ValueError, match="inside the data directory"):
        await wm.sync_external([str(alias)])


@pytest.mark.parametrize("bad", ["../../escape", "a/../../escape"])
async def test_restore_rejects_traversal_names(hermetic_base, bad):
    from features.backup import BackupManager

    bm = BackupManager(base_dir=str(hermetic_base))
    res = await bm.restore(bad)
    assert "error" in res
    assert "escape" in res["error"] or "escapes" in res["error"]


async def test_restore_accepts_legit_name(hermetic_base):
    from features.backup import BackupManager

    bm = BackupManager(base_dir=str(hermetic_base))
    await bm.backup(label="e7test")
    res = await bm.restore("e7test")
    assert "error" not in res, res
    assert res["backup"] == "e7test"
