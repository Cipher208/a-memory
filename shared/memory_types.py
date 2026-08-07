"""Typed memory — 13 categories with per-type retention/decay/boost policy.

13 types: instruction, fact, decision, goal, preference, commitment,
relationship, observation, rule, todo, question, hypothesis, context.

Each type has its own:
- default_importance (auto-fill on save)
- decay_rate (0 = never decays)
- never_archive flag
- requires_expires_at flag
- boost_on_keywords for retrieval
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass


class MemoryKind(str, enum.Enum):
    INSTRUCTION = "instruction"
    FACT = "fact"
    DECISION = "decision"
    GOAL = "goal"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    RELATIONSHIP = "relationship"
    OBSERVATION = "observation"
    RULE = "rule"
    TODO = "todo"
    QUESTION = "question"
    HYPOTHESIS = "hypothesis"
    CONTEXT = "context"


@dataclass(frozen=True)
class TypePolicy:
    kind: MemoryKind
    default_importance: float
    decay_rate: float
    never_archive: bool
    requires_expires_at: bool
    boost_on_keywords: tuple[str, ...]
    description: str


_REGISTRY: dict[MemoryKind, TypePolicy] = {
    MemoryKind.INSTRUCTION: TypePolicy(
        MemoryKind.INSTRUCTION,
        0.9,
        0.0,
        True,
        False,
        ("обязательно", "важно", "critical", "never forget", "rule", "инструкция"),
        "Правило/инструкция, не подлежит забыванию",
    ),
    MemoryKind.FACT: TypePolicy(
        MemoryKind.FACT,
        0.5,
        0.01,
        False,
        False,
        ("факт", "fact", "имя", "возраст", "день рождения"),
        "Атомарный факт",
    ),
    MemoryKind.DECISION: TypePolicy(
        MemoryKind.DECISION,
        0.7,
        0.005,
        False,
        False,
        ("решение", "decided", "chose", "decision"),
        "Принятое решение с обоснованием",
    ),
    MemoryKind.GOAL: TypePolicy(
        MemoryKind.GOAL,
        0.8,
        0.005,
        False,
        True,
        ("цель", "goal", "plan", "к концу"),
        "Цель с дедлайном",
    ),
    MemoryKind.PREFERENCE: TypePolicy(
        MemoryKind.PREFERENCE,
        0.7,
        0.003,
        False,
        False,
        ("предпочитаю", "prefer", "like", "нравится", "не люблю"),
        "Предпочтение",
    ),
    MemoryKind.COMMITMENT: TypePolicy(
        MemoryKind.COMMITMENT,
        0.85,
        0.0,
        True,
        True,
        ("обещаю", "обязуюсь", "commit", "promise", "согласен"),
        "Обязательство с дедлайном",
    ),
    MemoryKind.RELATIONSHIP: TypePolicy(
        MemoryKind.RELATIONSHIP,
        0.6,
        0.002,
        False,
        False,
        ("знаком", "друг", "коллега", "knows", "friend"),
        "Связь",
    ),
    MemoryKind.OBSERVATION: TypePolicy(
        MemoryKind.OBSERVATION,
        0.4,
        0.02,
        False,
        False,
        ("видел", "заметил", "noticed", "observed"),
        "Наблюдение",
    ),
    MemoryKind.RULE: TypePolicy(
        MemoryKind.RULE,
        0.85,
        0.0,
        True,
        False,
        ("запрещено", "нельзя", "do not", "forbidden"),
        "Жёсткое правило",
    ),
    MemoryKind.TODO: TypePolicy(
        MemoryKind.TODO,
        0.6,
        0.005,
        False,
        True,
        ("todo", "сделать", "do later", "remind"),
        "Задача с дедлайном",
    ),
    MemoryKind.QUESTION: TypePolicy(
        MemoryKind.QUESTION,
        0.5,
        0.05,
        False,
        False,
        ("вопрос", "уточнить", "ask later", "?"),
        "Открытый вопрос",
    ),
    MemoryKind.HYPOTHESIS: TypePolicy(
        MemoryKind.HYPOTHESIS,
        0.45,
        0.03,
        False,
        False,
        ("возможно", "наверное", "probably", "hypothesis"),
        "Гипотеза",
    ),
    MemoryKind.CONTEXT: TypePolicy(
        MemoryKind.CONTEXT,
        0.3,
        0.05,
        False,
        False,
        ("контекст", "background", "context"),
        "Фоновый контекст",
    ),
}

# Heuristic priority order (first match wins)
_KEYWORD_MAP: list[tuple[MemoryKind, tuple[str, ...]]] = [
    (MemoryKind.COMMITMENT, ("обещаю", "обязуюсь", "commit", "promise", "согласен")),
    (MemoryKind.INSTRUCTION, ("обязательно", "запомни", "никогда не", "remember to", "never forget")),
    (MemoryKind.RULE, ("запрещено", "нельзя", "do not", "forbidden", "никогда")),
    (MemoryKind.GOAL, ("цель", "хочу достичь", "plan to", "by next", "к концу")),
    (MemoryKind.DECISION, ("решил", "decided", "chose", "going with", "выбираю")),
    (MemoryKind.PREFERENCE, ("предпочитаю", "prefer", "нравится", "не люблю", "не нравится")),
    (MemoryKind.RELATIONSHIP, ("мой друг", "мой коллега", "мой брат", "my friend", "knows")),
    (MemoryKind.TODO, ("сделать", "todo", "нужно сделать", "to-do", "remind me")),
    (MemoryKind.QUESTION, ("?", "почему", "как", "зачем", "why", "how")),
    (MemoryKind.HYPOTHESIS, ("возможно", "наверное", "похоже что", "probably", "perhaps")),
    (MemoryKind.OBSERVATION, ("видел", "заметил", "noticed", "observed", "оказывается")),
]


def get_policy(kind: MemoryKind | str) -> TypePolicy:
    """Get policy for a kind. Unknown kinds fall back to FACT."""
    try:
        k = MemoryKind(kind) if isinstance(kind, str) else kind
    except ValueError:
        k = MemoryKind.FACT
    return _REGISTRY[k]


def validate_kind(kind: str) -> bool:
    """Check if kind is a valid MemoryKind."""
    try:
        MemoryKind(kind)
        return True
    except ValueError:
        return False


def default_importance(kind: MemoryKind | str) -> float:
    """Get default importance for a memory kind."""
    return get_policy(kind).default_importance


def apply_decay(value: float, kind: MemoryKind | str, days_since_update: float) -> float:
    """Apply type-aware exponential decay."""
    p = get_policy(kind)
    if p.decay_rate == 0:
        return value
    decayed = value * math.exp(-p.decay_rate * days_since_update)
    return max(0.01, decayed)


def can_archive(
    kind: MemoryKind | str,
    importance: float,
    days_since_update: float,
    archive_threshold_days: int = 90,
    archive_min_importance: float = 0.3,
) -> bool:
    """Check if memory can be archived, respecting type policy."""
    p = get_policy(kind)
    if p.never_archive:
        return False
    if days_since_update < archive_threshold_days:
        return False
    return not importance >= archive_min_importance


def kind_for_text(text: str) -> MemoryKind:
    """Best-effort heuristic for auto-classification. Returns FACT if nothing matches."""
    tl = text.lower()
    for kind, kws in _KEYWORD_MAP:
        if any(kw in tl for kw in kws):
            return kind
    return MemoryKind.FACT


def boost_for_query(query: str, candidate_kind: MemoryKind | str, base_boost: float = 0.0) -> float:
    """Boost for retrieval if query matches type keywords. Capped at 0.5."""
    p = get_policy(candidate_kind)
    q = query.lower()
    if not p.boost_on_keywords:
        return base_boost
    matches = sum(1.0 for kw in p.boost_on_keywords if kw in q)
    return min(matches * 0.1, 0.5)


async def backfill_null_kinds(cm, dry_run: bool = True) -> int:
    """Set NULL memory_kind to 'fact'. Returns count of affected rows."""
    conn = await cm.get("memory.db")
    if dry_run:
        row = await (await conn.execute("SELECT COUNT(*) c FROM core_memory WHERE memory_kind IS NULL")).fetchone()
        return int(row["c"])
    cur = await conn.execute("UPDATE core_memory SET memory_kind = 'fact' WHERE memory_kind IS NULL")
    await conn.commit()
    return cur.rowcount
