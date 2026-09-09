"""S19.2: collect 2-class training data from live core_memory (read-only).

Stable kinds (decay <= 0.005) → 'stable'; episodic → 'ephemeral'.
Single collector for the train script: all ~/.mcp-ariel-memory* instances.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

STABLE_KINDS = {"instruction", "commitment", "rule", "procedural", "relationship", "preference", "decision", "goal", "todo"}
EPHEMERAL_KINDS = {"fact", "observation", "hypothesis", "question", "context", "bug"}


def instance_dbs() -> list[Path]:
    """All live per-instance data dirs (read-only sources)."""
    out: list[Path] = []
    for p in sorted(Path.home().glob(".mcp-ariel-memory*")):
        if p.is_dir():
            db = p / "memory.db"
            if db.is_file() and p.name != ".mcp-ariel-memory-mimocode":
                out.append(db)  # mimocode = this agent's host; training on it is fair too — included below
    # include mimocode as well: a fact does not depend on who wrote it
    host = Path.home() / ".mcp-ariel-memory-mimocode" / "memory.db"
    if host.is_file() and host not in out:
        out.insert(0, host)
    return out


def collect_rows(min_len: int = 20) -> list[tuple[str, str]]:
    """(text, label) pairs from L4 of all instances; label ∈ {stable, ephemeral}."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for db in instance_dbs():
        conn = sqlite3.connect(db)
        try:
            for kind, value in conn.execute("SELECT memory_kind, value FROM core_memory WHERE memory_kind IS NOT NULL"):
                text = str(value or "").strip()
                if len(text) < min_len or text in seen:
                    continue
                seen.add(text)
                if kind in STABLE_KINDS:
                    rows.append((text, "stable"))
                elif kind in EPHEMERAL_KINDS:
                    rows.append((text, "ephemeral"))
        finally:
            conn.close()
    return rows


def class_balance(rows: list[tuple[str, str]]) -> dict[str, int]:
    balance: dict[str, int] = {"stable": 0, "ephemeral": 0}
    for _, label in rows:
        balance[label] += 1
    return balance


if __name__ == "__main__":
    rows = collect_rows()
    print(f"collected {len(rows)} rows: {class_balance(rows)}")
    for text, label in rows[:5]:
        print(f"  [{label}] {text[:80]!r}")
