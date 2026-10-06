"""Warm the embedding cache for a live base, OUTSIDE the 120 s nightly budget.

Fills `embedding_cache` only. It deliberately does NOT write edges, roles or
anomaly tags: the point is to make the next nightly pass fast, not to do the
pass's graph work early. `miner_embedding` builds its cache key as
"<content> <sorted canonical tags>" and this script reproduces that key by
calling the same helpers the miner calls (`_layer_nodes`, `_canon`), so a key
drift shows up as a 0% hit rate in the verification below rather than as a
silently useless cache.

Usage: BASE=<name> python3 warm_embedding_cache.py [--verify-only]
"""

import asyncio
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)

BASE = os.environ["MCP_MEMORY_DATA_DIR"]
LAYER = "user"
CHUNK = 500
TAG = "intfloat/multilingual-e5-small"


async def rich_texts(conn) -> list[tuple[int, str]]:
    """(node_id, rich text) exactly as `miner_embedding` builds it."""
    from lifecycle.graph_miners import _canon, _layer_nodes
    from rag.synonyms import load_synonyms

    nodes = await _layer_nodes(conn, LAYER)
    syn = load_synonyms()

    tags: dict[int, list[str]] = {}
    ids = [nid for nid, _ in nodes]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = await (
            await conn.execute(
                f"SELECT node_id, tag FROM epi_tags WHERE node_id IN ({','.join('?' * len(chunk))})",
                tuple(chunk),
            )
        ).fetchall()
        for r in rows:
            tags.setdefault(int(r["node_id"]), []).append(_canon(str(r["tag"]), syn))

    return [(nid, f"{c} {' '.join(sorted(t for t in tags.get(nid, []) if not t.startswith('anomaly:')))}") for nid, c in nodes]


async def main() -> None:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME
    from shared.embeddings import EmbeddingCache

    conn = await connection_manager.get(DB_NAME)
    pairs = await rich_texts(conn)
    texts = [t for _, t in pairs]
    unique = list(dict.fromkeys(texts))
    print(f"BASE {BASE}\n  nodes={len(texts)} unique_texts={len(unique)}", flush=True)

    cache = EmbeddingCache(cm=connection_manager)
    hashes = [cache._hash_text(t) for t in unique]

    have: set[str] = set()
    for start in range(0, len(hashes), CHUNK):
        chunk = hashes[start : start + CHUNK]
        rows = await (
            await conn.execute(
                f"SELECT text_hash FROM embedding_cache WHERE model_name=? AND text_hash IN ({','.join('?' * len(chunk))})",
                (TAG, *chunk),
            )
        ).fetchall()
        have.update(str(r[0]) for r in rows)

    missing = [t for t, h in zip(unique, hashes, strict=True) if h not in have]
    print(f"  already_cached={len(have)} missing={len(missing)}", flush=True)
    if not missing:
        print("  nothing to do", flush=True)
        return

    started = time.monotonic()
    done = 0
    for start in range(0, len(missing), CHUNK):
        chunk = missing[start : start + CHUNK]
        await cache.embed(chunk)
        done += len(chunk)
        rate = done / max(time.monotonic() - started, 1e-9)
        print(f"  {done}/{len(missing)} ({rate:.0f}/s)", flush=True)

    print(f"  warmed {done} in {time.monotonic() - started:.1f}s", flush=True)


asyncio.run(main())

# aiosqlite worker threads are non-daemon, so without closing the connections the
# interpreter parks in `threading._shutdown` forever after the work is done — the
# script looks hung while it has actually finished.
from shared.connection import connection_manager as _cm

asyncio.run(_cm.close_all())
