"""Typed memory — 14 categories with per-type retention/decay/boost policy.

14 types: instruction, fact, decision, goal, preference, commitment,
relationship, observation, rule, todo, question, hypothesis, context,
procedural (S17, ENGRAM: "how to do X" — the data foundation for Stage 2
behavior annotations).

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
import re
from dataclasses import dataclass
from typing import Any


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
    PROCEDURAL = "procedural"


# S4 (spec 2026-09-13): kinds a client may DECLARE — authored, not harvested.
# Declaration (not a regex) certifies the content, so these bypass the
# pattern-guessing gates; anything else ("fact", "observation", ...) stays
# heuristic all the way down.
L4_DECLARABLE_KINDS = frozenset({"preference", "commitment", "rule", "instruction", "relationship", "decision"})


@dataclass(frozen=True)
class TypePolicy:
    kind: MemoryKind
    default_importance: float
    decay_rate: float
    never_archive: bool
    requires_expires_at: bool
    boost_on_keywords: tuple[str, ...]
    description: str
    retrieval_priority: float = 0.5


_REGISTRY: dict[MemoryKind, TypePolicy] = {
    MemoryKind.INSTRUCTION: TypePolicy(
        MemoryKind.INSTRUCTION,
        0.9,
        0.0,
        True,
        False,
        ("обязательно", "важно", "critical", "never forget", "rule", "инструкция"),
        "Правило/инструкция, не подлежит забыванию",
        0.85,
    ),
    MemoryKind.FACT: TypePolicy(
        MemoryKind.FACT,
        0.5,
        0.01,
        False,
        False,
        ("факт", "fact", "имя", "возраст", "день рождения"),
        "Атомарный факт",
        0.9,
    ),
    MemoryKind.DECISION: TypePolicy(
        MemoryKind.DECISION,
        0.7,
        0.005,
        False,
        False,
        ("решение", "decided", "chose", "decision"),
        "Принятое решение с обоснованием",
        0.9,
    ),
    MemoryKind.GOAL: TypePolicy(
        MemoryKind.GOAL,
        0.8,
        0.005,
        False,
        True,
        ("цель", "goal", "plan", "к концу"),
        "Цель с дедлайном",
        0.8,
    ),
    MemoryKind.PREFERENCE: TypePolicy(
        MemoryKind.PREFERENCE,
        0.7,
        0.003,
        False,
        False,
        ("предпочитаю", "prefer", "like", "нравится", "не люблю"),
        "Предпочтение",
        0.6,
    ),
    MemoryKind.COMMITMENT: TypePolicy(
        MemoryKind.COMMITMENT,
        0.85,
        0.0,
        True,
        True,
        ("обещаю", "обязуюсь", "commit", "promise", "согласен"),
        "Обязательство с дедлайном",
        0.8,
    ),
    MemoryKind.RELATIONSHIP: TypePolicy(
        MemoryKind.RELATIONSHIP,
        0.6,
        0.002,
        False,
        False,
        ("знаком", "друг", "коллега", "knows", "friend"),
        "Связь",
        0.7,
    ),
    MemoryKind.OBSERVATION: TypePolicy(
        MemoryKind.OBSERVATION,
        0.4,
        0.02,
        False,
        False,
        ("видел", "заметил", "noticed", "observed"),
        "Наблюдение",
        0.3,
    ),
    MemoryKind.RULE: TypePolicy(
        MemoryKind.RULE,
        0.85,
        0.0,
        True,
        False,
        ("запрещено", "нельзя", "do not", "forbidden"),
        "Жёсткое правило",
        0.9,
    ),
    MemoryKind.TODO: TypePolicy(
        MemoryKind.TODO,
        0.6,
        0.005,
        False,
        True,
        ("todo", "to-do", "do later", "remind", "напомни"),
        "Задача с дедлайном",
        0.6,
    ),
    MemoryKind.QUESTION: TypePolicy(
        MemoryKind.QUESTION,
        0.5,
        0.05,
        False,
        False,
        ("вопрос", "уточнить", "ask later"),
        "Открытый вопрос",
        0.3,
    ),
    MemoryKind.HYPOTHESIS: TypePolicy(
        MemoryKind.HYPOTHESIS,
        0.45,
        0.03,
        False,
        False,
        ("возможно", "наверное", "probably", "hypothesis"),
        "Гипотеза",
        0.4,
    ),
    MemoryKind.CONTEXT: TypePolicy(
        MemoryKind.CONTEXT,
        0.3,
        0.05,
        False,
        False,
        ("контекст", "background", "context"),
        "Фоновый контекст",
        0.1,
    ),
    MemoryKind.PROCEDURAL: TypePolicy(
        MemoryKind.PROCEDURAL,
        0.8,
        0.0,
        True,
        False,
        ("сделай так", "порядок действий", "инструкция по", "how to", "шаги"),
        "Процедура/how-to (ENGRAM): как сделать X",
        0.8,
    ),
}

# Interrogative words that make a sentence a QUESTION when they OPEN it.
#
# NOTE ON SCOPE: the QUESTION entry below keeps its keyword list for READABILITY and
# for callers that introspect `_KEYWORD_MAP`, but `kind_for_text` does not match them
# as substrings — it calls `_is_question`. That is what removes the mid-sentence
# false positives WITHOUT resorting to word boundaries, which broke inflected forms.
# The verbatim failing examples live in tests/test_kind_tagger_gate.py: this file is
# held to the English-comments rule, and the evidence belongs in a test anyway.
#
# WHY POSITION MATTERS. `kind_for_text` used to accept any one of these as a bare
# substring ANYWHERE in the text, so an ordinary clause carrying the Russian word for
# "as/like" mid-sentence was filed as a question. Measured on one live base: 174 of 276
# `question` episodes matched on that one substring alone,
# and 179 of 276 contained no question mark and no interrogative at all. The tag feeds
# L3 episodes and the retrieval boost, so the register was ranking roleplay.
_QUESTION_WORDS = frozenset(
    {
        "как",
        "почему",
        "зачем",
        "что",
        "кто",
        "где",
        "когда",
        "какой",
        "какая",
        "какие",
        "каком",
        "чем",
        "куда",
        "откуда",
        "сколько",
        "why",
        "how",
        "what",
        "who",
        "where",
        "when",
        "which",
    }
)

# Imperative forms that make a clause a REQUEST for work when they open it.
_TASK_OPENERS = frozenset({"сделать", "сделай", "сделайте", "починить", "исправить", "запланировать", "проверить"})

# Leading decoration a sentence may carry: asterisks from roleplay, quotes, dashes.
_LEAD_JUNK = "*_«»\"'“”„-—– \t"


def _opens_with(text: str, words: frozenset[str]) -> bool:
    """Report whether any sentence of `text` begins with one of `words`.

    Position, not presence: the same word opening a clause and nested inside one
    mean different things. `_LEAD_JUNK` strips the asterisks roleplay wraps lines in,
    and the trailing punctuation strip keeps hyphenated and indefinite forms from being
    read as their bare interrogative root.
    """
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text.strip()):
        parts = sentence.strip().lstrip(_LEAD_JUNK).split(maxsplit=1)
        if parts and parts[0].lower().rstrip(",.:;!?—–") in words:
            return True
    return False


def _is_question(text: str) -> bool:
    """Decide whether the text ACTUALLY asks something.

    Two sufficient signals: a question mark, or a sentence that OPENS with an
    interrogative word. The Russian for "as/like" mid-sentence is not a question;
    the same word opening a clause is. Everything else falls through to the other kinds, and ultimately
    to FACT, which is the honest answer for text nobody classified: under-tagging
    costs a coarser retrieval hint, while over-tagging poisoned 65% of a register.
    """
    return "?" in text or _opens_with(text, _QUESTION_WORDS)


def _is_task_request(text: str) -> bool:
    """Decide whether the text ASKS FOR WORK to be done.

    The mirror of `_is_question`, and needed for the same reason. Dropping the bare
    the bare verb "to do/make" keyword removed the junk but also stopped a bare
    imperative ("Make a backup before deploy") from being a task, so the imperative is
    accepted when it OPENS a clause. It is refused when nested, because narration uses
    the same verb: a past-tense report of work already done was one of the eleven live
    rows filed as a task by the substring.

    The Russian obligation particles are deliberately NOT openers despite sounding like
    tasks: the same past-tense report OPENS with one of them, so position does not
    separate them the way it separates an interrogative from a statement.
    """
    return _opens_with(text, _TASK_OPENERS)


# Heuristic priority order (first match wins).
#
# QUESTION sits ABOVE TODO on purpose. A line can both ask something and contain an
# explicit task phrase, and the asking wins: the
# interrogative form is a statement about the sentence, while the phrase is one word
# inside it. Measured on live data, the old order filed that line as a task.
_KEYWORD_MAP: list[tuple[MemoryKind, tuple[str, ...]]] = [
    (MemoryKind.PROCEDURAL, ("сделай так", "порядок действий", "инструкция по", "how to", "пошагово", "step 1")),
    (MemoryKind.COMMITMENT, ("обещаю", "обязуюсь", "commit", "promise", "согласен")),
    (MemoryKind.INSTRUCTION, ("обязательно", "запомни", "никогда не", "remember to", "never forget")),
    (MemoryKind.RULE, ("запрещено", "нельзя", "do not", "forbidden", "никогда")),
    (MemoryKind.GOAL, ("цель", "хочу достичь", "plan to", "by next", "к концу")),
    (MemoryKind.DECISION, ("решил", "decided", "chose", "going with", "выбираю")),
    (MemoryKind.PREFERENCE, ("предпочитаю", "prefer", "нравится", "не люблю", "не нравится")),
    (MemoryKind.RELATIONSHIP, ("мой друг", "мой коллега", "мой брат", "my friend", "knows")),
    (MemoryKind.QUESTION, ("?", "почему", "как", "зачем", "что", "кто", "где", "когда", "какой", "why", "how", "what", "who", "where", "when")),
    (MemoryKind.TODO, ("todo", "to-do", "нужно сделать", "надо сделать", "нужно будет", "надо будет", "remind me", "напомни", "не забыть")),
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
    """Best-effort heuristic for auto-classification. Returns FACT if nothing matches.

    Two gates apply, and they are the whole fix. QUESTION is decided by `_is_question`
    and TODO by `_is_task_request` — by the SHAPE of the text, not by one word appearing
    in it. Everything else still matches by substring, deliberately.

    WHY SUBSTRING IS KEPT. Whole-word matching was tried first, because `kw in text`
    is what let "how" match inside "show". Measured against 10404 live rows it also
    broke Russian inflection and English stems — feminine and prefixed verb forms
    stopped matching their dictionary keys, and `committed` stopped matching `commit` —
    losing 148 classifications. This house writes in feminine grammatical forms, so a
    silently became FACT. The semantic gates remove the real defect without that cost,
    so the blunt instrument was withdrawn.

    Returning FACT is not a failure: it is the honest answer for text that matched no
    pattern, and it costs a coarser retrieval hint. Over-tagging is what this replaces.
    """
    tl = text.lower()
    for kind, kws in _KEYWORD_MAP:
        if kind is MemoryKind.QUESTION:
            if _is_question(text):
                return kind
            continue
        if kind is MemoryKind.TODO and _is_task_request(text):
            return kind
        if any(kw in tl for kw in kws):
            return kind
    return MemoryKind.FACT


def boost_for_query(query: str, candidate_kind: MemoryKind | str, base_boost: float = 0.0) -> float:
    """Boost for retrieval if query matches type keywords. Capped at 0.5."""
    p = get_policy(candidate_kind)
    q = query.lower()
    matches = sum(1.0 for kw in p.boost_on_keywords if kw in q)
    # A question-kind memory is boosted because the QUERY asks something. The old
    # list carried a bare "?" for this, which meant every question-marked query
    # lifted every question-kind memory — and owners mostly ask questions, so the
    # boost was constant and ranked nothing. Interrogativity is the real condition.
    if p.kind is MemoryKind.QUESTION and _is_question(query):
        matches += 1.0
    if not matches:
        return base_boost
    return min(matches * 0.1, 0.5)


async def backfill_null_kinds(cm: Any, dry_run: bool = True) -> int:
    """Set NULL memory_kind to 'fact'. Returns count of affected rows."""
    conn = await cm.get("memory.db")
    if dry_run:
        row = await (await conn.execute("SELECT COUNT(*) c FROM core_memory WHERE memory_kind IS NULL")).fetchone()
        return int(row["c"])
    cur = await conn.execute("UPDATE core_memory SET memory_kind = 'fact' WHERE memory_kind IS NULL")
    await conn.commit()
    return int(cur.rowcount)
