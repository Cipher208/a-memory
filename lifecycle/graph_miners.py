"""Graph miners (Phase G): deterministic edge factories over existing data.

Task 1 stubs — bodies are filled by Tasks 2-5. Every edge a real miner writes
MUST carry the tag `heuristic:<name>` (rollback: DELETE WHERE tags LIKE
'%heuristic:%'). No LLM calls anywhere.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from shared.connection import AsyncConnectionManager

Miner = Callable[[AsyncConnectionManager, str], Awaitable[dict[str, int]]]


async def miner_tags(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#1: shared epi_tags → `tagged` edges."""
    return {"edges": 0}


async def miner_tokens(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#2: shared rare tokens (FTS5, Jaccard) → `topic_overlap` edges."""
    return {"edges": 0}


async def miner_entities(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#3: NER co-mentions → `co_mentions` edges."""
    return {"edges": 0}


async def miner_sessions(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#4: facts of one session → `same_session` edges."""
    return {"edges": 0}


async def miner_provenance(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#5: core fact → episode → wiki metadata.parents → `sourced_from` edges."""
    return {"edges": 0}


async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges."""
    return {"edges": 0}


async def miner_embedding(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#9: embedding similarity >= 0.7 → `semantic_overlap` edges."""
    return {"edges": 0}


MINERS: dict[str, Miner] = {
    "tags": miner_tags,
    "tokens": miner_tokens,
    "entities": miner_entities,
    "sessions": miner_sessions,
    "provenance": miner_provenance,
    "co_retrieval": miner_co_retrieval,
    "embedding": miner_embedding,
}
