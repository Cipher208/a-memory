"""S18-хвост offload: тяжёлые tool-логи → work_notes/refs-*.md + compact-узел."""

from __future__ import annotations

import pytest

from features.offload import REFS_THRESHOLD_CHARS, offload_tool_log, slugify
from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def wave_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=connection_manager)
    await wm.init_db()
    from graph.epistemic import EpistemicGraph

    yield wm, EpistemicGraph(cm=connection_manager, layer="user")
    connection_manager._conns.clear()


async def test_offload_light_log_returns_none(wave_db) -> None:
    wm, _graph = wave_db
    result = await offload_tool_log(wm, "ou", "deploy", "короткий лог" * 10)
    assert result is None, "лёгкий текст — статус-кво, ничего не оффлоадится"


async def test_offload_heavy_log_wiki_page_and_compact_node(wave_db) -> None:
    wm, _graph = wave_db  # graph НЕ трогаем: F-T9 single-entry, узел даст wiki_graph_builder
    heavy = "monitoring report line\n" * (REFS_THRESHOLD_CHARS // 20 + 50)

    result = await offload_tool_log(wm, "ou", "selfcheck", heavy)

    assert result is not None and result["chars"] == len(heavy.strip())
    ref_path = result["ref_path"]
    assert ref_path.startswith("work_notes/refs-") and "selfcheck" in ref_path, f"refs-страница: {ref_path}"

    # страница существует и несёт полный лог (wm.get по wiki-пути с .md)
    page = await wm.get(ref_path)
    assert page is not None and "monitoring report line" in page.content

    # страница видна через wiki-поиск (это её recall-поверхность)
    found = await wm.search("selfcheck", limit=5)
    assert any("refs-" in str(r.get("file_path", "")) for r in found), f"refs-страница в FTS: {found}"


async def test_slugify_deterministic_and_safe() -> None:
    a = slugify("Self-Monitoring Report!", 1700000000.0)
    b = slugify("Self-Monitoring Report!", 1700000000.0)
    c = slugify("self monitoring report", 1700000001.0)
    assert a == b, "детерминизм"
    assert a != c, "время/имя различают"
    assert a.startswith("refs-") and "/" not in a and " " not in a
