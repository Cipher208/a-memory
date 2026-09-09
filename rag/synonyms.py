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
    # G4 entity canonization (miner #3): name variants → one entity node
    "лили": ["lily", "лисёныш"],
    "lily": ["лили", "лисёныш"],
    "лисёныш": ["лили", "lily"],
}


def load_synonyms() -> dict[str, list[str]]:
    """Built-in table merged with config `rag.synonyms` overrides."""
    from config import config

    overrides = config.get("rag", "synonyms", default=None) or {}
    return {**_BUILTIN_SYNONYMS, **overrides}


def load_counter_signals() -> dict[str, str]:
    """Config `rag.counter_signals` {superseded_name: current_name} (S17 item 7).

    Tenure counter-signal aliases: superseded entity names (renames) remain
    valid for search but get a negative role in ranking — a hit containing the
    old name without the current one is demoted (not dropped).
    Default {} — the mechanic is neutral until the owner declares pairs.
    """
    from config import config

    raw = config.get("rag", "counter_signals", default=None) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v).lower() for k, v in raw.items()}


def canonical_form(w: str, synonyms: dict[str, list[str]] | None = None) -> str:
    """Canonical form of a token: the synonym class unfolded in BOTH directions.

    Config `rag.synonyms` entries may be one-directional (`{"mamochka": ["mom"]}`
    without the reverse key) — a token that appears only as a value still maps
    to the class. Canon = lexicographically smallest member (stable across
    callers: distiller keys, graph miner entity linking).
    """
    table = synonyms if synonyms is not None else load_synonyms()
    cls = {w, *table.get(w, []), *(k for k, vs in table.items() if w in vs)}
    return min(cls)


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
