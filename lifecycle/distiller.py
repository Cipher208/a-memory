"""G1 distiller: atomize → type → canonical key → route (инвариант→L4, событие→L3).

F-G1: текст сообщения режется на атомарные клаузы, каждая типизируется
(kind_for_text), получает канонический ключ (синонимы схлопываются в одну
форму) и маршрутизируется по TypePolicy.decay_rate: инварианты (<= 0.005) →
L4 core_memory, события → L3 episodic. Противоречия ловит ConflictResolver:
запись не затирает старую, а помечается provenance `:contradiction`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from shared.memory_types import MemoryKind, get_policy, kind_for_text

logger = logging.getLogger(__name__)

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

    G4: после сохранения каждый атом попадает в граф узлом (find_or_add fact)
    и сразу обвязывается лёгкими минерами — инкрементальный режим, ночной
    batch не ждём. mem.l3.save — дверь для событий, CoreMemory — для инвариантов.
    Ошибки не глушатся: auto_save_text уже стоит за fire-контрактом registry.
    """
    from core.memory import CoreMemory
    from rag.conflict import ConflictResolver

    cmem = CoreMemory(cm=getattr(mem, "_cm", None), layer="user")
    await cmem._init_db()  # self-healing schema, как ConflictResolver.check — fixture может быть без миграций
    stats = {"l4_saved": 0, "l3_saved": 0, "conflicts": 0}
    resolver = ConflictResolver()
    saved: list[str] = []
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
                saved.append(clause)
                continue
            await cmem.save(user_id, key, clause, importance=score, memory_kind=kind.value, source=event)
            stats["l4_saved"] += 1
            saved.append(clause)
        else:
            await mem.l3.save(user_id, clause[:500], score, [*extra_tags, event, kind.value])
            stats["l3_saved"] += 1
            saved.append(clause)
            if has_conflict:
                stats["conflicts"] += 1
    stats["wired_edges"] = await _wire_atoms(cmem._cm, user_id, saved)
    return stats


async def _wire_atoms(cm: Any, user_id: str, clauses: list[str]) -> int:
    """Инкрементальный режим (G4): узел графа для каждого сохранённого атома + рёбра vs существующие.

    Лёгкие минеры (tags/entities/tokens) по НОВОМУ узлу срабатывают сразу при
    записи — ночной batch (graph_enrich) не нужен для свежих соседей.
    Best-effort: distill_and_route стоит в prod-пути — сбой минеров не глушит
    сохранение памяти, скатывается в ночной batch.
    """
    if not clauses:
        return 0
    try:
        from graph.epistemic import EpistemicGraph
        from lifecycle.graph_miners import wire_new_node

        g = EpistemicGraph(cm=cm, layer="user")
        edges = 0
        for clause in clauses:
            node_id, _created = await g.find_or_add_entity(user_id, clause[:500], "fact")
            edges += await wire_new_node(cm, "user", node_id, clause[:500])
        return edges
    except Exception:
        logger.debug("incremental graph wiring failed", exc_info=True)
        return 0
