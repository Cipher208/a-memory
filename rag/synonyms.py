"""A3.1: synonyms + query expansion for FTS retrieval (5-layer hard-trigger pair).

Config-driven (rag.synonyms) over a small built-in RU/EN table. The expanded
MATCH expression reaches search_fts5; the LIKE fallback keeps the ORIGINAL
query (substring semantics). Expansion only appends known alnum terms — FTS5
syntax errors still degrade to LIKE via the existing safety net.
"""

from __future__ import annotations

_BUILTIN_SYNONYMS: dict[str, list[str]] = {
    "postgres": ["postgresql", "psql"],
    "postgresql": ["postgres", "psql"],
    "память": ["memory"],
    "memory": ["память"],
    "деплой": ["deploy", "deployment"],
    "deploy": ["деплой", "deployment"],
    "бэкап": ["backup"],
    "backup": ["бэкап"],
    # G4 entity-канонизация (минер #3): варианты имени → один узел сущности
    "лили": ["lily", "лисёныш"],
    "lily": ["лили", "лисёныш"],
    "лисёныш": ["лили", "lily"],
}


def load_synonyms() -> dict[str, list[str]]:
    """Built-in table merged with config `rag.synonyms` overrides."""
    from config import config

    overrides = config.get("rag", "synonyms", default=None) or {}
    return {**_BUILTIN_SYNONYMS, **overrides}


def expand_fts_query(query: str, synonyms: dict[str, list[str]] | None = None) -> str:
    """Expand query tokens with synonyms → an FTS5 MATCH expression.

    Tokens with known synonyms become `(term OR syn1 OR syn2)` groups; other
    tokens pass through unchanged. Returns the original query when no
    expansion applies (zero behavioral change for unaffected queries).
    """
    table = synonyms if synonyms is not None else load_synonyms()
    tokens = str(query).split()
    out: list[str] = []
    changed = False
    for tok in tokens:
        key = tok.lower().strip(".,!?;:\"'()")
        syns = table.get(key)
        if syns:
            group = [key] + [s for s in syns if s.lower() != key]
            out.append("(" + " OR ".join(group) + ")")
            changed = True
        else:
            out.append(tok)
    if not changed:
        return str(query)
    # FTS5: an explicit group followed by a bare token is a syntax error —
    # adjacent phrases need an explicit AND ("(a OR b) AND c"), otherwise the
    # whole MATCH raises and search_fts5 silently degrades to LIKE (chaos/E2E
    # finding: expansion never reached the engine).
    return " AND ".join(out)
