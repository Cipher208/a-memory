"""Integration test: WikiManager.add()/sync_external() never block on secrets.

The core guarantee is "warn-only, never blocks": content with a real secret
still saves. Detection itself is covered by the pure scan_secrets tests in
test_secrets.py; this proves the write path stays functional.
"""

from __future__ import annotations

import pathlib

import pytest

from wiki import WikiManager
from shared.connection import AsyncConnectionManager


@pytest.mark.asyncio
async def test_add_with_secret_still_saves(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()

    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    path = await wm.add("diary", "Note", f"contains {secret} here")

    assert path  # warn-only: the write is never blocked


@pytest.mark.asyncio
async def test_sync_external_with_secret_still_imports(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    wm = WikiManager(layer="user", base_dir=str(tmp_path / "w"), cm=cm)
    await wm.init_db()

    ext = pathlib.Path(str(tmp_path)) / "ext"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "note.md").write_text(
        "---\ntitle: Leaky\nwiki_type: diary\n---\nsecret sk-abcdefghijklmnopqrstuvwxyz1234567890",
        encoding="utf-8",
    )

    res = await wm.sync_external([str(ext)])

    assert res["imported"] >= 1  # imported despite the secret
