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
    from rag.synonyms import canonical_form, load_synonyms

    syn = load_synonyms()
    words = re.findall(r"[а-яёa-z0-9]+", clause.lower())
    canon: list[str] = []
    for w in words:
        if len(w) <= 2:
            continue
        # синонимы → одна каноническая форма (алфавитно-первая), postgres/postgresql/psql → postgres
        canon.append(canonical_form(w, syn))
        if len(canon) == 4:
            break
    return f"{kind.value}:" + "_".join(canon) if canon else f"{kind.value}:misc"


# C8 novelty-gate: paraphrase-Jaccard против уже сохранённых same-key фактов;
# выше порога — дубликат, skip (не плодим near-dup L4-ключи).
NOVELTY_JACCARD_MAX = 0.85


def _merged_meta(raw: Any, source_rid: int) -> dict[str, Any]:
    """S6a-4: metadata L4-строки с вмерженным source_raw_id (не затирая остальное)."""
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["source_raw_id"] = source_rid
    return meta


def _is_novel(clause: str, existing_values: list[str]) -> bool:
    """Вернуть True, если clause не парафраз существующих значений ключа (LLM-free)."""
    if not existing_values:
        return True
    from rag.edm import tokens

    ct = tokens(clause)
    if not ct:
        return True
    for val in existing_values:
        vt = tokens(val)
        union = ct | vt
        if union and len(ct & vt) / len(union) > NOVELTY_JACCARD_MAX:
            return False
    return True


# C8 topic-классификация: словарные маркеры → тег для epi_tags/wiki-типа (LLM-free).
_TOPIC_MARKERS: dict[str, tuple[str, ...]] = {
    "deploy": ("деплой", "deploy", "release", "выкатил", "мигр"),
    "error": ("ошибк", "error", "exception", "упал", "fail", "npe", "traceback"),
    "decision": ("решил", "решено", "decision", "выбрал", "остановил", "выбран"),
    "config": ("конфиг", "config", "настро", "yaml", "toml", "env"),
    "performance": ("медленн", "latency", "перформ", "perf", "оптимиз", "кэш", "cache"),
    "security": ("секрет", "токен", "пароль", "secret", "auth", "уязвим"),
    "memory": ("памят", "memory", "запом", "эпизод", "факт"),
}


def _topic_of(clause: str) -> str:
    low = clause.lower()
    for topic, markers in _TOPIC_MARKERS.items():
        if any(m in low for m in markers):
            return topic
    return "general"


def atomize(text: str) -> list[str]:
    parts = _CLAUSE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 8][:10]


def route_kind(kind: MemoryKind) -> str:
    """Инвариант→l4, событие→l3 — по TypePolicy.decay_rate (0 = никогда не умирает)."""
    return "l4" if get_policy(kind).decay_rate <= 0.005 else "l3"


def score_text(text: str, event: str = "new_message") -> float:
    """Важность текста через ImportanceScorer (8 сигналов) — вместо хардкода 0.6.

    Аудит 05.09: replay/bridge/import дистиллируют с константой — скоринг не
    вызывался нигде, где сырьё реально дистиллируется. Здесь — единая точка.
    Ошибка скорера не глушит сохранение: деградация к 0.6 (историческое
    значение).
    """
    try:
        from shared.importance import ImportanceScorer

        result = ImportanceScorer().score(text=text, event=event)
        return max(0.0, min(1.0, float(result.total())))
    except Exception as exc:
        logger.debug("importance scorer degraded to 0.6: %s", exc)
        return 0.6


async def distill_and_route(
    mem: Any,
    graph: Any,
    user_id: str,
    text: str,
    score: float,
    *,
    event: str = "new_message",
    extra_tags: tuple[str, ...] | list[str] = (),
    source_rid: int | None = None,
) -> dict[str, int]:
    """Разложить text на атомы и развести по слоям.

    G4: после сохранения каждый атом попадает в граф узлом (find_or_add fact)
    и сразу обвязывается лёгкими минерами — инкрементальный режим, ночной
    batch не ждём. mem.l3.save — дверь для событий, CoreMemory — для инвариантов.
    Ошибки не глушатся: auto_save_text уже стоит за fire-контрактом registry.
    source_rid (S6a-4): id строки l0_journal-источника — пишется в metadata
    L4-записей (source_raw_id) и тегом raw:<rid> на L3-эпизодах → drill-down
    до исходного сырья.
    """
    from core.memory import CoreMemory
    from rag.conflict import ConflictResolver

    cmem = CoreMemory(cm=getattr(mem, "_cm", None), layer="user")
    await cmem._init_db()  # self-healing schema, как ConflictResolver.check — fixture может быть без миграций
    stats: dict[str, Any] = {"l4_saved": 0, "l3_saved": 0, "conflicts": 0, "novelty_skipped": 0}
    resolver = ConflictResolver()
    saved: list[str] = []
    for clause in atomize(text):
        kind = kind_for_text(clause)
        key = _canonical_key(clause, kind)
        if route_kind(kind) == "l4":
            # C8 novelty-gate: парафраз уже сохранённых same-key фактов — skip.
            conn = await cmem._cm.get("memory.db")
            rows = await (
                await conn.execute(
                    "SELECT value, metadata FROM core_memory WHERE layer=? AND user_id=? AND key=? AND visibility != 'private'",
                    (cmem.layer, user_id, key),
                )
            ).fetchall()
            if not _is_novel(clause, [str(r["value"]) for r in rows]):
                stats["novelty_skipped"] += 1
                continue
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
                meta_new: dict[str, Any] = _merged_meta(rows[0]["metadata"] if rows else None, source_rid) if source_rid is not None else {}
                meta_new["scope"] = "later"
                meta_new["contradiction"] = True
                if first_key:
                    meta_new["contradicts"] = first_key
                # Аудит 05.09 (P0): save — upsert по UNIQUE(layer,user,key);
                # запись later под тем же канон-ключом молча затёрла бы
                # существующую строку (и earlier, и любую same-key). Ключ
                # занят → версонируем ::vN, чтобы обе записи жили.
                later_key = key
                if rows:
                    vcur = await (
                        await conn.execute(
                            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND (key=? OR key LIKE ?)",
                            (cmem.layer, user_id, key, key + "::v%"),
                        )
                    ).fetchone()
                    later_key = f"{key}::v{int((vcur[0] if vcur else 0) or 0) + 1}"
                await cmem.save(
                    user_id,
                    later_key,
                    clause,
                    importance=score * 0.9,
                    memory_kind=kind.value,
                    source=f"{event}:contradiction",
                    metadata=meta_new,
                )
                stats["l4_saved"] += 2 if first_key else 1
                saved.append(clause)
                continue
            l4_meta: dict[str, Any] | None = None
            if source_rid is not None:
                l4_meta = _merged_meta(rows[0]["metadata"] if rows else None, source_rid)
            await cmem.save(user_id, key, clause, importance=score, memory_kind=kind.value, source=event, metadata=l4_meta)
            stats["l4_saved"] += 1
            saved.append(clause)
        else:
            # C8 topic-классификация: словарный топик → epi_tags эпизода.
            # S6a-4: у episodes нет metadata-колонки — провенанс уходит тегом raw:<rid>.
            l3_tags = [*extra_tags, event, kind.value, f"topic:{_topic_of(clause)}"]
            if source_rid is not None:
                l3_tags.append(f"raw:{source_rid}")
            await mem.l3.save(user_id, clause[:500], score, l3_tags)
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
