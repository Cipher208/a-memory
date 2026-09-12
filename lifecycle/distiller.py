"""G1 distiller: atomize → type → canonical key → route (invariant→L4, event→L3).

F-G1: the message text is split into atomic clauses, each typed
(kind_for_text), given a canonical key (synonyms collapse into a single
form) and routed by TypePolicy.decay_rate: invariants (<= 0.005) →
L4 core_memory, events → L3 episodic. Contradictions are caught by ConflictResolver:
the record does not overwrite the old one but is marked with provenance `:contradiction`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from shared.dialogue import is_dialogic as _is_dialogic
from shared.memory_types import MemoryKind, get_policy, kind_for_text

logger = logging.getLogger(__name__)
_CLAUSE_SPLIT = re.compile(r"[,;]?\s+(?:и|но|причём|а|хотя)\s+|\.\s+")


def _canonical_key(clause: str, kind: MemoryKind) -> str:
    from config import config

    from rag.synonyms import canonical_form, load_counter_signals, load_synonyms
    from shared.morph import normal_form

    syn = load_synonyms()
    counters = load_counter_signals()
    lemmatize = bool(config.get("rag", "lemmatize", default=False))
    words = re.findall(r"[а-яёa-z0-9]+", clause.lower())
    canon: list[str] = []
    for w in words:
        if len(w) <= 2:
            continue
        # S18: a counter-signal pair canonicalizes to the CURRENT name —
        # renames fall into the same key, C4 builds the superseded chain itself.
        if w in counters:
            w = counters[w]
        # S19 tail: the lemma collapses inflection ('zarplaty' -> 'zarplata');
        # drift risk is documented in config.yaml — old keys are not re-hashed.
        if lemmatize and len(w) > 2:
            lemma = normal_form(w)
            if lemma and len(lemma) > 2:
                w = lemma
        # synonyms → one canonical form (alphabetically first), postgres/postgresql/psql → postgres
        canon.append(canonical_form(w, syn))
        if len(canon) == 4:
            break
    return f"{kind.value}:" + "_".join(canon) if canon else f"{kind.value}:misc"


# C8 novelty-gate: paraphrase-Jaccard against already-saved same-key facts;
# above the threshold — duplicate, skip (no proliferation of near-dup L4 keys).
NOVELTY_JACCARD_MAX = 0.85

# S18 item 5: 3rd dedup signal (after exact-SHA on L0 and Jaccard novelty) —
# cosine against existing same-kind facts. Config memory.semantic_dedup.
_SEMANTIC_DEDUP_MAX = 0.92


async def _semantic_duplicate(cm: Any, clause: str, kind: MemoryKind, user_id: str, layer: str) -> str | None:
    """Key of an existing L4 fact with cosine > _SEMANTIC_DEDUP_MAX, else None.

    Flag memory.semantic_dedup (default off — enabled after the No.11-eval).
    Embeddings unavailable (breaker/hash off/failure) → None: dedup degrades,
    saving is not blocked. Comparison is within kind — facts of different
    types do not conflict.
    """
    from config import config

    if not bool(config.get("memory", "semantic_dedup", default=False)):
        return None
    try:
        from shared.embeddings import embed_texts, similarity

        conn = await cm.get("memory.db")
        rows = await (
            await conn.execute(
                "SELECT key, value FROM core_memory WHERE layer=? AND user_id=? AND memory_kind=? LIMIT 50",
                (layer, user_id, kind.value),
            )
        ).fetchall()
        if not rows:
            return None
        vecs = await embed_texts([clause, *[str(r["value"]) for r in rows]])
        if len(vecs) != len(rows) + 1:
            return None
        for i, r in enumerate(rows):
            if similarity(vecs[0], vecs[i + 1]) > _SEMANTIC_DEDUP_MAX:
                return str(r["key"])
    except Exception:
        return None
    return None


def _merged_meta(raw: Any, source_rid: int) -> dict[str, Any]:
    """S6a-4: L4-row metadata with source_raw_id merged in (without wiping the rest)."""
    try:
        meta = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["source_raw_id"] = source_rid
    return meta


def _is_novel(clause: str, existing_values: list[str]) -> bool:
    """Return True if clause is not a paraphrase of the key's existing values (LLM-free)."""
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


# C8 topic classification: dictionary markers → tag for epi_tags/wiki-type (LLM-free).
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
    """Invariant→l4, event→l3 — by TypePolicy.decay_rate (0 = never dies)."""
    return "l4" if get_policy(kind).decay_rate <= 0.005 else "l3"


def score_text(text: str, event: str = "new_message") -> float:
    """Text importance via ImportanceScorer (8 signals) — instead of hardcoding 0.6.

    Audit 05.09: replay/bridge/import distill with a constant — scoring was
    not called anywhere raw material is actually distilled. Here is the single
    entry point. A scorer error does not silence saving: degrade to 0.6 (the
    historical value).
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
) -> dict[str, Any]:
    """Atomize text and route the atoms across layers.

    G4: after saving, each atom enters the graph as a node (find_or_add fact)
    and is immediately wired with the light miners — incremental mode, no
    waiting for the nightly batch. mem.l3.save is the door for events,
    CoreMemory for invariants. Errors are not silenced: auto_save_text already
    sits behind the registry's fire-contract.
    source_rid (S6a-4): id of the l0_journal source row — written into the
    metadata of L4 records (source_raw_id) and as a raw:<rid> tag on L3
    episodes → drill-down to the original raw material.
    """
    from core.memory import CoreMemory
    from rag.conflict import ConflictResolver

    cmem = CoreMemory(cm=getattr(mem, "_cm", None), layer="user")
    await cmem._init_db()  # self-healing schema, like ConflictResolver.check — the fixture may lack migrations
    stats: dict[str, Any] = {
        "l4_saved": 0,
        "l3_saved": 0,
        "conflicts": 0,
        "novelty_skipped": 0,
        "semantic_skipped": 0,  # S18 item 5: cosine>0.92 dedup
        "guard_skipped": 0,  # F1 2026-09-12: dialogic/misc atoms never reach L4
        "similar_to": [],  # S17 A2-advisory: keys the clause intersected with (near-dup/conflict)
    }
    # S17 ENGRAM: procedural («how to do X»-style) — agent-self track, L4 of the agent layer.
    agent_cmem: CoreMemory | None = None
    resolver = ConflictResolver()
    saved: list[str] = []
    for clause in atomize(text):
        kind = kind_for_text(clause)
        key = _canonical_key(clause, kind)
        # ENGRAM procedural: how-to lives in the agent's L4-namespace, not the user's.
        target: CoreMemory = cmem
        # S19.2 textcat: a keyword-unmatched FACT (decay 0.010 → l3) with a confident
        # 'stable' must not die in episodes — promote to L4 (kind=FACT).
        # Keyword matches are not touched; flag rag.textcat default OFF (pilot).
        route = route_kind(kind)
        if route == "l3" and kind == MemoryKind.FACT:
            from shared.textcat import route_promote_stable

            if route_promote_stable(clause):
                route = "l4"
        if route == "l4" and kind == MemoryKind.PROCEDURAL:
            if agent_cmem is None:
                agent_cmem = CoreMemory(cm=getattr(mem, "_cm", None), layer="agent")
                await agent_cmem._init_db()
            target = agent_cmem
        if route == "l4" and (_is_dialogic(clause) or key.endswith(":misc")):
            # F1 2026-09-12: conversational register and unkeyable atoms are
            # not durable facts — the raw text stays in L0/L3 where it belongs.
            stats["guard_skipped"] += 1
            continue
        if route_kind(kind) == "l4":
            # C8 novelty-gate: a paraphrase of already-saved same-key facts — skip.
            # NOTE on visibility: hidden rows DELIBERATELY stay in this select —
            # 'hidden' is key-quarantine, so paraphrase attempts against a
            # quarantined key must be suppressed by the novelty gate too.
            conn = await cmem._cm.get("memory.db")
            rows = await (
                await conn.execute(
                    "SELECT value, metadata FROM core_memory WHERE layer=? AND user_id=? AND key=? AND visibility != 'private'",
                    (target.layer, user_id, key),
                )
            ).fetchall()
            if not _is_novel(clause, [str(r["value"]) for r in rows]):
                stats["novelty_skipped"] += 1
                stats["similar_to"].append(key)  # A2-advisory: a paraphrase of this key is already stored
                continue
            # S18 item 5: semantic dedup — 3rd signal, before the conflict-check (a close
            # paraphrase must not spawn a conflict pair; it is simply a duplicate).
            sem_dup = await _semantic_duplicate(cmem._cm, clause, kind, user_id, cmem.layer)
            if sem_dup is not None:
                stats["semantic_skipped"] += 1
                stats["similar_to"].append(sem_dup)
                continue
        conflict = await resolver.check(user_id, clause)
        has_conflict = bool(conflict.get("is_conflict"))
        if route == "l4":
            if has_conflict:
                # C4 condition-splitting: a contradiction is neither an overwrite
                # nor a silent contradiction-only record, but TWO conditional
                # records. The earlier one is marked metadata {'scope': 'earlier'},
                # the new one — {'scope': 'later', 'contradicts': first_key};
                # both with importance ×0.9 (conflict lowers confidence).
                stats["conflicts"] += 1
                first_key = await _mark_earlier_scope(target, user_id, conflict)
                meta_new: dict[str, Any] = _merged_meta(rows[0]["metadata"] if rows else None, source_rid) if source_rid is not None else {}
                meta_new["scope"] = "later"
                meta_new["contradiction"] = True
                if first_key:
                    meta_new["contradicts"] = first_key
                # S17 A2-advisory: the agent decides itself — rephrase or deliberately append.
                stats["similar_to"].append(first_key or key)
                # Audit 05.09 (P0): save is an upsert on UNIQUE(layer,user,key);
                # a later record under the same canonical key would silently wipe
                # the existing row (both earlier and any same-key one). The key
                # is taken → version it ::vN so both records survive.
                later_key = key
                if rows:
                    vcur = await (
                        await conn.execute(
                            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND (key=? OR key LIKE ?)",
                            (target.layer, user_id, key, key + "::v%"),
                        )
                    ).fetchone()
                    later_key = f"{key}::v{int((vcur[0] if vcur else 0) or 0) + 1}"
                await target.save(
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
            await target.save(user_id, key, clause, importance=score, memory_kind=kind.value, source=event, metadata=l4_meta)
            stats["l4_saved"] += 1
            saved.append(clause)
        else:
            # C8 topic classification: dictionary topic → epi_tags of the episode.
            # S6a-4: episodes have no metadata column — provenance goes as a raw:<rid> tag.
            l3_tags = [*extra_tags, event, kind.value, f"topic:{_topic_of(clause)}"]
            if source_rid is not None:
                l3_tags.append(f"raw:{source_rid}")
            await mem.l3.save(user_id, clause[:500], score, l3_tags)
            stats["l3_saved"] += 1
            saved.append(clause)
            if has_conflict:
                stats["conflicts"] += 1
    stats["wired_edges"] = await _wire_atoms(cmem._cm, user_id, saved)
    # A2-advisory: unique keys in order of first occurrence.
    stats["similar_to"] = list(dict.fromkeys(stats["similar_to"]))
    return stats


async def _mark_earlier_scope(cmem: Any, user_id: str, conflict: dict[str, Any]) -> str | None:
    """C4: mark the earlier side of the conflict with scope='earlier' (importance ×0.9).

    ConflictResolver stores the content of both sides in memory_conflicts — via
    conflicts_with_id we fetch the earlier text, restore its canonical key
    (the same _canonical_key as at first save) and re-save via cmem.save
    (LEDGER + bi-temporal). The returned key is used for the contradicts link
    on the later record; None if the earlier side is not found in L4.
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
    """Incremental mode (G4): a graph node for each saved atom + edges vs existing ones.

    The light miners (tags/entities/tokens) fire on the NEW node immediately at
    write time — the nightly batch (graph_enrich) is not needed for fresh neighbors.
    Best-effort: distill_and_route sits in the prod path — a miner failure does not
    silence memory saving; it degrades to the nightly batch.
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
