"""G1 distiller: atomize → type → canonical key → route (инвариант→L4, событие→L3).

F-G1: текст сообщения режется на атомарные клаузы, каждая типизируется
(kind_for_text), получает канонический ключ (синонимы схлопываются в одну
форму) и маршрутизируется по TypePolicy.decay_rate: инварианты (<= 0.005) →
L4 core_memory, события → L3 episodic. Противоречия ловит ConflictResolver:
запись не затирает старую, а помечается provenance `:contradiction`.
"""

from __future__ import annotations

import json
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
                # C4 condition-splitting: противоречие — не затирание и не
                # молчаливый contradiction-only, а ДВЕ условные записи.
                # Ранняя помечается metadata {'scope': 'earlier'}, новая —
                # {'scope': 'later', 'contradicts': first_key}; обе с
                # importance ×0.9 (конфликт снижает уверенность).
                stats["conflicts"] += 1
                first_key = await _mark_earlier_scope(cmem, user_id, conflict)
                meta_new: dict[str, Any] = {"scope": "later", "contradiction": True}
                if first_key:
                    meta_new["contradicts"] = first_key
                await cmem.save(
                    user_id,
                    key,
                    clause,
                    importance=score * 0.9,
                    memory_kind=kind.value,
                    source=f"{event}:contradiction",
                    metadata=meta_new,
                )
                stats["l4_saved"] += 2 if first_key else 1
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


async def _mark_earlier_scope(cmem: Any, user_id: str, conflict: dict[str, Any]) -> str | None:
    """C4: пометить раннюю сторону конфликта scope='earlier' (importance ×0.9).

    ConflictResolver хранит content обеих сторон в memory_conflicts — по
    conflicts_with_id достаём ранний текст, восстанавливаем его канонический
    ключ (тот же _canonical_key, что при первой записи) и пере-сохраняем через
    cmem.save (LEDGER + bi-temporal). Возврат ключа — для связи contradicts
    у поздней записи; None, если ранняя сторона не найдена в L4.
    """
    from shared.constants import DB_NAME

    prior_id = conflict.get("conflicts_with_id")
    if not prior_id:
        return None
    conn = await cmem._cm.get(DB_NAME)
    row = await (await conn.execute("SELECT content FROM memory_conflicts WHERE id=?", (int(prior_id),))).fetchone()
    if row is None:
        return None
    prior_text = str(row["content"])
    first_key = _canonical_key(prior_text, kind_for_text(prior_text))
    prow = await (
        await conn.execute(
            "SELECT value, importance, memory_kind, source, metadata FROM core_memory WHERE layer=? AND user_id=? AND key=?",
            (cmem.layer, user_id, first_key),
        )
    ).fetchone()
    if prow is None:
        return None
    try:
        meta = json.loads(prow["metadata"] or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    meta["scope"] = "earlier"
    await cmem.save(
        user_id,
        first_key,
        str(prow["value"]),
        importance=float(prow["importance"]) * 0.9,
        memory_kind=str(prow["memory_kind"]) if prow["memory_kind"] else None,
        source=str(prow["source"]),
        metadata=meta,
    )
    return first_key


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
