import pytest

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch
from repositories import build_repositories

CREATE_MEM = (
    "CREATE TABLE IF NOT EXISTS core_memory ("
    "entry_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    'user_id TEXT, "key" TEXT, value TEXT, importance REAL,'
    "memory_kind TEXT, expires_at REAL, created_at REAL, updated_at REAL)"
)
CREATE_GRAPH = [
    ("CREATE TABLE IF NOT EXISTS epi_nodes (node_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, kind TEXT)"),
    ("CREATE TABLE IF NOT EXISTS epi_edges (edge_id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, label TEXT)"),
]


@pytest.fixture
async def bundle(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    conn = await m.get(DB_NAME)
    for stmt in (CREATE_MEM, *CREATE_GRAPH):
        await conn.execute(stmt)
    await conn.commit()
    await m.close_all()
    d = WriteDispatch(AsyncConnectionManager(base_dir=str(tmp_path)))
    yield d, build_repositories(d)
    await d.close()


@pytest.mark.asyncio
async def test_decay_batch_writes_via_dispatch(bundle):
    d, repos = bundle
    for eid, imp in ((1, 0.5), (2, 0.4)):
        await d.write(
            DB_NAME,
            'INSERT INTO core_memory (entry_id, user_id, "key", value, importance, memory_kind, created_at, updated_at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, "u", f"k{eid}", "v", imp, "fact", 1000.0, 1000.0),
        )
    await repos.memory.decay_batch([(0.1, 1), (0.2, 2)])
    rows = await d.query(DB_NAME, "SELECT entry_id, importance FROM core_memory ORDER BY entry_id")
    assert [(r["entry_id"], r["importance"]) for r in rows] == [(1, 0.1), (2, 0.2)]


@pytest.mark.asyncio
async def test_prune_dangling_edges(bundle):
    d, repos = bundle
    await d.write(
        DB_NAME,
        "INSERT INTO epi_nodes (node_id, title, kind) VALUES (1, 'a', 'x'), (2, 'b', 'y')",
    )
    await d.write(
        DB_NAME,
        "INSERT INTO epi_edges (source_id, target_id, label) VALUES (1, 2, 'keep'), (1, 999, 'drop-target'), (999, 1, 'drop-source')",
    )
    removed = await repos.graph.prune_dangling_edges()
    assert removed == 2
    rows = await d.query(DB_NAME, "SELECT source_id, target_id FROM epi_edges")
    assert [(r["source_id"], r["target_id"]) for r in rows] == [(1, 2)]
