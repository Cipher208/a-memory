"""D1.3 steering hints — publish the best-tool routes for common intents.

Deterministic route table + keyword intent match (RU/EN). Ariel publishes
the routes; the harness decides when to consult them (advisory, never
enforced). v2 upgrade path: rank by tool-call telemetry (needs per-call
audit rows — deliberately not built in v1).
"""

from __future__ import annotations

_HINT_CAP = 3

ROUTE_TABLE: tuple[dict[str, str], ...] = (
    {
        "when": "recover context in a new session",
        "match": "новая сессия|с чего начали|what were we|recap|continuit|resume",
        "use": "memory_recap",
        "instead": "re-reading raw history",
        "why": "~2K token recovery pack (D1.2)",
    },
    {
        "when": "recall what was said or decided earlier",
        "match": "найди|вспомни|что говорил|обсуждали|what did|where did|recall|earlier|discussed",
        "use": "memory_recall_protocol(query, budget)",
        "instead": "memory_search / memory_recall",
        "why": "5 axes ranked, deduped, budget-capped (D1.1)",
    },
    {
        "when": "keep a working note across compaction",
        "match": "гипотез|черновик|запиши пока|hypothesis|working note|for now",
        "use": "memory_scratchpad(action='write')",
        "instead": "memory_remember",
        "why": "pad re-injects and is evictable; L4 is for durable facts (D1.15)",
    },
    {
        "when": "store a durable fact or decision",
        "match": "запомни|помни|навсегда|remember|durable|important fact",
        "use": "memory_remember or DREAM: memory:/fact: marker",
        "instead": "memory_scratchpad",
        "why": "durable facts feed inject + markers (C1.12)",
    },
    {
        "when": "procedural / skill knowledge",
        "match": "how do i|how to|procedure|skill|инструкция|как делать|шаги",
        "use": "memory_procedure for repeatable procedures; wiki_search then wiki_read for rich skill docs",
        "instead": "memory_episode_recall",
        "why": "procedures carry execution stats (D2.5); skills are wiki pages (D2.1)",
    },
    {
        "when": "rate whether recalled memory helped",
        "match": "помогло|было полезно|was useful|feedback|useful signal",
        "use": "memory_quality(action='feedback')",
        "instead": "skipping the signal",
        "why": "feeds ACT-R frequency + importance (D1.17/D1.19)",
    },
    {
        "when": "review staged memory mutations",
        "match": "proposal|staged|на согласовании|pending review",
        "use": "memory_proposals(action='list' or 'decide')",
        "instead": "direct writes bypassing review",
        "why": "staging is the C1.11 contract",
    },
    {
        "when": "memory health / stats overview",
        "match": "статистик|сколько записей|how many|health|report card|stats",
        "use": "memory_stats / memory_report_card",
        "instead": "manual SELECT scans",
        "why": "curated health surfaces (C1.14)",
    },
)


def steering_hints(query: str = "") -> list[dict[str, str]]:
    """Match the query intent against the route table (empty query = full table)."""
    q = (query or "").lower()
    if not q.strip():
        return [dict(r) for r in ROUTE_TABLE]
    hints = [dict(r) for r in ROUTE_TABLE if any(tok in q for tok in str(r["match"]).split("|"))]
    return hints[:_HINT_CAP]
