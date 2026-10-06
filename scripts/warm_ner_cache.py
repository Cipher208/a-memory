"""Warm the NER cache for a live base, OUTSIDE the 120 s nightly budget.

Fills `ner_cache` only. Like `warm_embedding_cache.py` it deliberately does NOT
write edges: `miner_entities` turns cached entity sets into `co_mentions` edges
and applies lateral inhibition, and none of that belongs here. The point is to
make the next nightly pass fast, not to do the pass's graph work early.

The cache key is `(sha256(node content), _ner_cache_tag(syn, nlp))`, and this
script reproduces it by calling the miner's own `_layer_nodes`, `load_synonyms`,
`_get_ner` and `_entities_cached`. Reusing those exact helpers matters: a tag
built from a differently-loaded spaCy model, or a differently-fingerprinted
synonym dictionary, would look warmed here and still miss in the nightly. The
script therefore prints the tag it used, so a mismatch is visible rather than
silent.

Measured on hermes: the first (cold) enrich pass spends 35.9 s in
`miner:entities` because `_layer_nodes` has no LIMIT and every node is re-parsed;
with this cache warm the same phase is 5.4 s.

Usage: python3 warm_ner_cache.py            # warm, then report
       python3 warm_ner_cache.py --verify-only
"""

import asyncio
import hashlib
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)

BASE = os.environ["MCP_MEMORY_DATA_DIR"]
LAYER = "user"
CHUNK = 500
VERIFY_ONLY = "--verify-only" in sys.argv


async def main() -> None:
    from lifecycle.graph_miners import (
        _ensure_ner_cache,
        _entities_cached,
        _get_ner,
        _layer_nodes,
        _ner_cache_tag,
    )
    from rag.synonyms import load_synonyms
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    nodes = await _layer_nodes(conn, LAYER)
    texts = [str(c) for _, c in nodes]
    unique = list(dict.fromkeys(texts))

    syn = load_synonyms()
    nlp = _get_ner()
    tag = _ner_cache_tag(syn, nlp)
    await _ensure_ner_cache(conn)

    hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in unique]
    have: set[str] = set()
    for start in range(0, len(hashes), CHUNK):
        chunk = hashes[start : start + CHUNK]
        rows = await (
            await conn.execute(
                f"SELECT text_hash FROM ner_cache WHERE tag=? AND text_hash IN ({','.join('?' * len(chunk))})",
                (tag, *chunk),
            )
        ).fetchall()
        have.update(str(r[0]) for r in rows)

    backend = "dict-only" if nlp is None else f"{getattr(nlp, 'meta', {}).get('name', 'ner')}"
    print(
        f"BASE {BASE}\n"
        f"  nodes={len(texts)} unique_texts={len(unique)}\n"
        f"  ner_backend={backend} tag={tag}\n"
        f"  already_cached={len(have)} missing={len(unique) - len(have)}",
        flush=True,
    )
    if VERIFY_ONLY or len(have) == len(unique):
        print("  nothing to do", flush=True)
        return

    started = time.monotonic()
    # `_entities_cached` returns one set per text and computes only the misses,
    # writing them in one transaction — this IS the miner's cache-fill path.
    ents = await _entities_cached(conn, texts, syn, nlp)
    elapsed = time.monotonic() - started
    empty = sum(1 for e in ents if not e)
    print(
        f"  warmed {len(unique) - len(have)} in {elapsed:.1f}s ({len(unique) / max(elapsed, 1e-9):.0f}/s)\n  entity_sets={len(ents)} empty={empty}",
        flush=True,
    )


asyncio.run(main())

# aiosqlite worker threads are non-daemon, so without closing the connections the
# interpreter parks in `threading._shutdown` forever after the work is done — the
# script looks hung while it has actually finished.
from shared.connection import connection_manager as _cm

asyncio.run(_cm.close_all())
