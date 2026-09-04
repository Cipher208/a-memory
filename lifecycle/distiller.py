"""G1 distiller: atomize → type → canonical key → route (инвариант→L4, событие→L3).

F-G1: текст сообщения режется на атомарные клаузы, каждая типизируется
(kind_for_text), получает канонический ключ (синонимы схлопываются в одну
форму) и маршрутизируется по TypePolicy.decay_rate: инварианты (<= 0.005) →
L4 core_memory, события → L3 episodic. Противоречия ловит ConflictResolver:
запись не затирает старую, а помечается provenance `:contradiction`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from shared.memory_types import MemoryKind, get_policy, kind_for_text

_CLAUSE_SPLIT = re.compile(r"[,;]?\s+(?:и|но|причём|а|хотя)\s+|\.\s+")


@dataclass
class Atom:
    clause: str
    kind: MemoryKind
    importance: float
    key: str


def _canonical_key(clause: str, kind: MemoryKind) -> str:
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    words = re.findall(r"[а-яёa-z0-9]+", clause.lower())
    canon: list[str] = []
    for w in words:
        if len(w) <= 2:
            continue
        # синонимы → одна каноническая форма (алфавитно-первая), postgres/postgresql/psql → postgres
        canon.append(min([w, *syn.get(w, [])]))
        if len(canon) == 4:
            break
    return f"{kind.value}:" + "_".join(canon) if canon else f"{kind.value}:misc"


def atomize(text: str) -> list[str]:
    parts = _CLAUSE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 8][:10]


def route_kind(kind: MemoryKind) -> str:
    """Инвариант→l4, событие→l3 — по TypePolicy.decay_rate (0 = никогда не умирает)."""
    return "l4" if get_policy(kind).decay_rate <= 0.005 else "l3"


async def distill_and_route(
    mem: Any,
    graph: Any,
    user_id: str,
    text: str,
    score: float,
    *,
    event: str = "new_message",
    extra_tags: tuple[str, ...] | list[str] = (),
) -> dict[str, int]:
    """Разложить text на атомы и развести по слоям.

    graph не пишется напрямую (граф наполняют минеры, F-T9); mem.l3.save —
    единственная дверь для событий, CoreMemory(cm из mem._cm) — для инвариантов.
    Ошибки не глушатся: auto_save_text уже стоит за fire-контрактом registry.
    """
    from core.memory import CoreMemory
    from rag.conflict import ConflictResolver

    cmem = CoreMemory(cm=getattr(mem, "_cm", None), layer="user")
    await cmem._init_db()  # self-healing schema, как ConflictResolver.check — fixture может быть без миграций
    stats = {"l4_saved": 0, "l3_saved": 0, "conflicts": 0}
    resolver = ConflictResolver()
    for clause in atomize(text):
        kind = kind_for_text(clause)
        key = _canonical_key(clause, kind)
        conflict = await resolver.check(user_id, clause)
        has_conflict = bool(conflict.get("is_conflict"))
        if route_kind(kind) == "l4":
            if has_conflict:
                stats["conflicts"] += 1
                await cmem.save(
                    user_id,
                    key,
                    clause,
                    importance=score,
                    memory_kind=kind.value,
                    source=f"{event}:contradiction",
                    metadata={"contradiction": True},
                )
                continue
            await cmem.save(user_id, key, clause, importance=score, memory_kind=kind.value, source=event)
            stats["l4_saved"] += 1
        else:
            await mem.l3.save(user_id, clause[:500], score, [*extra_tags, event, kind.value])
            stats["l3_saved"] += 1
            if has_conflict:
                stats["conflicts"] += 1
    return stats
