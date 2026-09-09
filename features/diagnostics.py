"""E3: operator diagnostics — health checks + safe auto-fixes + content audit. No LLM.

Structure: audit_content is the orchestrator of seven independent content
checks (_audit_* helpers, one per finding type); run_diagnose composes infra
checks via a check() callback. Each helper preserves the original exception
granularity — an advisory check failing never aborts the audit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

HEAL_ACTIONS = ("remigrate", "reset_breakers", "purge_invalid_l1")

STALE_DAYS = 90  # mirrors shared.memory_types.can_archive default

# content-audit severities: contradiction = data-integrity fail; rest advisory warn

# C7 gap-reader (S13): reader must SURFACE unknown, not just fuse
QUESTION_STALE_DAYS = 7  # older than this — open question, an answer is owed
HIGH_IMPORTANCE = 0.8  # create_safety threshold: important facts require L0 confirmation
L0_SCAN_LIMIT = 2000  # ponytail: python-side substring scan — fine at audit scale


async def _resolve_uri_entry(conn: Any, entry_id: str, user_id: str) -> tuple[Any, dict[str, Any] | None]:
    """Resolve an ariel-URI entry reference.

    Returns (row_or_None, early_response). When early_response is not None the
    caller must return it (unresolvable URI / non-fact store / missing row);
    otherwise row is the core_memory row.
    """
    from shared.uris import parse_uri

    parsed = parse_uri(entry_id)
    if parsed is None or parsed.get("reserved"):
        return None, {"source_raw_id": None, "error": f"unresolvable URI: {entry_id!r}"}
    if parsed["store"] != "fact":
        # non-L4 URIs carry no provenance chain — return the raw resolution
        from shared.uris import resolve_uri

        resolved = await resolve_uri(connection_manager_ref(), parsed["layer"], user_id, entry_id)
        return None, ({"source_raw_id": None, "resolved": resolved} if resolved else {"source_raw_id": None})
    row = await (
        await conn.execute(
            "SELECT entry_id, key, value, metadata FROM core_memory WHERE layer=? AND user_id=? AND key=?",
            (parsed["layer"], user_id, parsed["key"]),
        )
    ).fetchone()
    if row is None:
        return None, {"source_raw_id": None}
    return row, None


def connection_manager_ref() -> Any:
    """Lazy indirection so the module stays import-light (mirror of drill_down)."""
    from shared.connection import connection_manager

    return connection_manager


async def drill_down(entry_id: int | str, user_id: str, layer: str = "user") -> dict[str, Any]:
    """S6a-4 provenance reader: L4 entry → original raw l0_journal record.

    Via metadata.source_raw_id (written by the distiller) we fetch the raw
    l0_journal row: text, fixation moment and the source event — downward
    hydration of "why we decided this". No provenance → {'source_raw_id': None}.

    Stage 2-A: entry_id may be an ariel-URI (`ariel://user/fact/<key>`) —
    resolved to an entry_id before the provenance read.
    """
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    if isinstance(entry_id, str) and entry_id.startswith("ariel://"):
        row_meta, early = await _resolve_uri_entry(conn, entry_id, user_id)
        if early is not None:
            return early
        entry_id = int(row_meta["entry_id"])
        row = row_meta
    elif isinstance(entry_id, str) and not entry_id.isdigit():
        # Bare fact key (not a numeric id, not a URI) — resolve by key.
        # Pre-existing crash: int(entry_id) raised ValueError on key strings.
        row = await (
            await conn.execute(
                "SELECT key, value, metadata FROM core_memory WHERE layer=? AND user_id=? AND key=?",
                (layer, user_id, entry_id),
            )
        ).fetchone()
    else:
        row = await (
            await conn.execute(
                "SELECT key, value, metadata FROM core_memory WHERE entry_id=? AND user_id=?",
                (int(entry_id), user_id),
            )
        ).fetchone()
    if row is None:
        return {"source_raw_id": None}
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    rid = meta.get("source_raw_id")
    if rid is None:
        return {"source_raw_id": None}
    raw = await (await conn.execute("SELECT text, ts, event FROM l0_journal WHERE id=? AND user_id=?", (int(rid), user_id))).fetchone()
    if raw is not None:
        return _provenance_result(entry_id, row, rid, raw, archived=False)
    # S17 addendum 8: the row survived tier_l0 — it moved to l0_cold_archive
    # (>180d processed). Drill-down must not dead-end: fallback by id.
    with contextlib.suppress(Exception):  # the table may not exist yet (before the first tiering)
        raw = await (
            await conn.execute(
                "SELECT text, ts, event FROM l0_cold_archive WHERE id=? AND user_id=?",
                (int(rid), user_id),
            )
        ).fetchone()
        if raw is not None:
            return _provenance_result(entry_id, row, rid, raw, archived=True)
    return {"source_raw_id": int(rid), "raw_text": None, "raw_ts": None, "raw_event": None, "key": str(row["key"]), "value": str(row["value"])}


def _provenance_result(entry_id: Any, row: Any, rid: int, raw: Any, *, archived: bool) -> dict[str, Any]:
    # entry_id may be a numeric id or a bare fact key (string) — keep the
    # caller's form instead of crashing on int() coercion.
    try:
        eid: int | str = int(entry_id)
    except (TypeError, ValueError):
        eid = str(entry_id)
    result = {
        "entry_id": eid,
        "key": str(row["key"]),
        "value": str(row["value"]),
        "source_raw_id": int(rid),
        "raw_text": str(raw["text"]),
        "raw_ts": float(raw["ts"]),
        "raw_event": str(raw["event"]),
    }
    if archived:
        result["archived"] = True  # raw from the cold tier (l0_cold_archive)
    return result


async def _audit_contradictions(conn: Any) -> dict[str, Any] | None:
    """Check (a): unresolved contradictions — ConflictResolver marks originals is_conflict=1."""
    rows = await (await conn.execute("SELECT id, user_id, conflict_group_id, content FROM memory_conflicts WHERE is_conflict=1 LIMIT 100")).fetchall()
    if rows:
        return {
            "severity": "fail",
            "type": "contradiction",
            "items": [{"id": r["id"], "user_id": r["user_id"], "group_id": r["conflict_group_id"], "content": r["content"][:200]} for r in rows],
            "suggestion": "Resolve via ConflictResolver.resolve(conflict_group_id, keep_id) — one keeper per group.",
        }
    return None


async def _audit_duplicates(conn: Any, user_id: str) -> dict[str, Any] | None:
    """(b) duplicates: pairwise same-kind, different keys, smart_similarity > 0.85.

    ponytail: O(n²) pairwise scan — fine at audit scale (<10k facts); blocking or
    FTS prefilter if user memories grow past that.
    """
    from rag.conflict import smart_similarity

    rows = await (
        await conn.execute(
            "SELECT entry_id, memory_kind, key, value FROM core_memory WHERE user_id=? AND memory_kind IS NOT NULL ORDER BY memory_kind",
            (user_id,),
        )
    ).fetchall()
    by_kind: dict[str, list[Any]] = {}
    for r in rows:
        by_kind.setdefault(str(r["memory_kind"]), []).append(r)
    dup_items: list[dict[str, Any]] = []
    for group in by_kind.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a["key"] == b["key"]:
                    continue
                sim = smart_similarity(str(a["value"]), str(b["value"]))
                if sim > 0.85:
                    dup_items.append({"a_key": a["key"], "b_key": b["key"], "kind": str(a["memory_kind"]), "similarity": round(sim, 3)})
    if dup_items:
        return {
            "severity": "warn",
            "type": "duplicate",
            "items": dup_items[:100],
            "suggestion": "Merge near-identical same-kind facts — keep one key, delete the twin.",
        }
    return None


async def _audit_stale(conn: Any, user_id: str) -> dict[str, Any] | None:
    """(c) stale: updated_at older than STALE_DAYS, archivable kind, never recalled.

    Recall signal = audit_log action='recall_useful' (features/replay.py record_recall_useful);
    recall_events has no per-entry linkage, so it cannot serve here.
    """
    from shared.memory_types import MemoryKind, get_policy

    never_archive = tuple(k.value for k in MemoryKind if get_policy(k).never_archive)
    cutoff = time.time() - STALE_DAYS * 86400
    rows = await (
        await conn.execute(
            "SELECT entry_id, key, memory_kind, updated_at FROM core_memory"
            f" WHERE user_id=? AND updated_at < ? AND (memory_kind IS NULL OR memory_kind NOT IN ({','.join('?' * len(never_archive))}))"
            " AND entry_id NOT IN (SELECT DISTINCT CAST(target_id AS INTEGER) FROM audit_log WHERE action='recall_useful')"
            " LIMIT 100",
            (user_id, cutoff, *never_archive),
        )
    ).fetchall()
    if rows:
        return {
            "severity": "warn",
            "type": "stale",
            "items": [{"key": r["key"], "kind": r["memory_kind"], "days_old": int((time.time() - r["updated_at"]) / 86400)} for r in rows],
            "suggestion": f"Not recalled in any audit trail and untouched for {STALE_DAYS}d — archive via lifecycle sweep or bump importance.",
        }
    return None


async def _audit_dead_links(conn: Any, keys: set[str]) -> dict[str, Any] | None:
    """(d) dead links: [[fact:key]] in wiki content pointing to a non-existent core key."""
    wiki_rows = await (await conn.execute("SELECT title, content FROM wiki_index WHERE content LIKE '%[[fact:%' LIMIT 200")).fetchall()
    dead: list[dict[str, Any]] = []
    for r in wiki_rows:
        for ref in re.findall(r"\[\[fact:([^\]]+)\]\]", str(r["content"])):
            if ref not in keys:
                dead.append({"title": r["title"], "key": ref})
    if dead:
        return {
            "severity": "warn",
            "type": "dead_link",
            "items": dead[:100],
            "suggestion": "Wiki references a fact key that no longer exists — fix or remove the [[fact:…]] link.",
        }
    return None


async def _audit_unknown_questions(conn: Any, user_id: str, keys: set[str]) -> dict[str, Any] | None:
    """(e) gap-reader `unknown`: questions older than QUESTION_STALE_DAYS with no answer.

    Answers conventionally live as a sibling '<key>.answer' fact — no
    convention existed before C7, declared here.
    """
    q_rows = await (await conn.execute("SELECT key, updated_at FROM core_memory WHERE user_id=? AND memory_kind='question'", (user_id,))).fetchall()
    open_questions = [
        {"key": str(r["key"]), "age_days": int((time.time() - r["updated_at"]) / 86400)}
        for r in q_rows
        if time.time() - r["updated_at"] > QUESTION_STALE_DAYS * 86400 and f"{r['key']}.answer" not in keys
    ]
    if open_questions:
        return {
            "severity": "warn",
            "type": "unknown",
            "items": open_questions[:100],
            "suggestion": f"Open question older than {QUESTION_STALE_DAYS}d — answer it (save the reply as '<key>.answer') or archive the question.",
        }
    return None


async def _audit_create_safety(conn: Any, user_id: str) -> dict[str, Any] | None:
    """(f) gap-reader create_safety: high-importance facts get an evidence verdict.

    Facts with importance >= HIGH_IMPORTANCE are checked against the L0
    journal — exists (value verbatim in l0 text), probable (significant token
    found), unknown (no L0 trace).
    """
    fact_rows = await (
        await conn.execute(
            "SELECT key, value, importance FROM core_memory WHERE user_id=? AND importance >= ?",
            (user_id, HIGH_IMPORTANCE),
        )
    ).fetchall()
    if not fact_rows:
        return None
    l0_texts = [str(r["text"]).lower() for r in (await (await conn.execute(f"SELECT text FROM l0_journal LIMIT {L0_SCAN_LIMIT}")).fetchall())]
    safety_items: list[dict[str, Any]] = []
    for r in fact_rows:
        value = str(r["value"])
        if any(value.lower() in t for t in l0_texts):
            verdict = "exists"
        else:
            toks = re.findall(r"\w{4,}", value.lower())
            verdict = "probable" if any(tok in t for t in l0_texts for tok in toks) else "unknown"
        safety_items.append({"key": str(r["key"]), "importance": float(r["importance"]), "verdict": verdict})
    if any(i["verdict"] != "exists" for i in safety_items):
        return {
            "severity": "warn",
            "type": "create_safety",
            "items": safety_items[:100],
            "suggestion": "High-importance fact without L0 evidence — re-confirm with the user before asserting it downstream (verdict: exists/probable/unknown).",
        }
    return None


async def _audit_wiki_reconciliation(user_id: str) -> dict[str, Any] | None:
    """Check (g): S13 file↔DB reconciliation — wiki .md files vs wiki_index rows.

    Orphans — file without a row; stale — row without a file.
    """
    from features.wiki_reconciliation import reconcile

    rec = await reconcile(user_id)
    rec_items = [{"orphan": p} for p in rec["orphans"]] + [{"stale_index": p} for p in rec["stale"]]
    if rec_items:
        return {
            "severity": "warn",
            "type": "wiki_reconciliation",
            "items": rec_items[:100],
            "suggestion": "Orphan .md → WikiManager.reindex_all(); stale index row → WikiManager.delete(path).",
        }
    return None


async def audit_content(user_id: str = "default") -> list[dict[str, Any]]:
    """H2 memory_audit: content-level checks over stored memory (read-only).

    Reads the memory_conflicts table (first reader — the ConflictResolver only
    wrote it), plus duplicate/stale/dead-link scans over core_memory and wiki.
    Returns [{severity: 'warn'|'fail', type, items, suggestion}, ...].
    """
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    out: list[dict[str, Any]] = []

    for finding in (
        await _audit_contradictions(conn),
        await _audit_duplicates(conn, user_id),
        await _audit_stale(conn, user_id),
    ):
        if finding is not None:
            out.append(finding)

    # (d)+(e) share the full key set — one read for both checks
    keys = {str(r["key"]) for r in await (await conn.execute("SELECT key FROM core_memory")).fetchall()}
    for finding in (
        await _audit_dead_links(conn, keys),
        await _audit_unknown_questions(conn, user_id, keys),
        await _audit_create_safety(conn, user_id),
    ):
        if finding is not None:
            out.append(finding)

    finding = await _audit_wiki_reconciliation(user_id)
    if finding is not None:
        out.append(finding)

    return out


def _check_db(check: Any) -> None:
    """db_exists + integrity + migrations checks."""
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    base = Path(str(connection_manager.base_dir))
    db_path = base / DB_NAME
    check("db_exists", db_path.exists(), str(db_path), warn=not db_path.exists())
    if db_path.exists():
        import sqlite3

        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                row = conn.execute("PRAGMA quick_check").fetchone()
                check("db_integrity", row is not None and row[0] == "ok", str(row[0] if row else "?"))
                try:
                    ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                    check("migrations", ver is not None, f"alembic at {ver[0]}" if ver else "alembic_version empty")
                except sqlite3.OperationalError:
                    check("migrations", False, "alembic_version missing")
        except sqlite3.Error as exc:
            check("db_integrity", False, str(exc))


async def _check_l1_files(check: Any) -> None:
    """Check L1 persist files (E1) — valid JSON."""
    from shared.connection import connection_manager

    base = Path(str(connection_manager.base_dir))
    l1_paths = await asyncio.to_thread(lambda: sorted(base.glob("l1_*.json")))
    for p in l1_paths:
        try:
            content = await asyncio.to_thread(p.read_text)
            json.loads(content)
            check("l1_file:" + p.name, True)
        except (json.JSONDecodeError, OSError) as exc:
            check("l1_file:" + p.name, False, str(exc))


async def run_diagnose(user_id: str = "default") -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        checks.append({"name": name, "status": "ok" if ok else ("warn" if warn else "fail"), "detail": detail})

    _check_db(check)
    await _check_l1_files(check)

    # staged proposals backlog
    try:
        from features.staging import list_pending

        pending = await list_pending(user_id, 100)
        check("pending_proposals", len(pending) <= 20, f"{len(pending)} pending", warn=len(pending) > 20)
    except Exception as exc:
        check("pending_proposals", False, str(exc))

    # S1 tamper-evidence: L0 hash-chain recheck (verify_chain; v1/v2 formats).
    try:
        from shared.l0 import verify_chain

        broken = await verify_chain()
        check("l0_hash_chain", not broken, f"{len(broken)} broken" if broken else "chain intact", warn=False)
    except Exception as exc:
        check("l0_hash_chain", False, str(exc))

    # circuit breakers (E2)
    try:
        from shared.circuit_breaker import breaker_registry

        open_brs = {n: m for n, m in breaker_registry.get_all_metrics().items() if m["state"] != "closed"}
        check(
            "circuit_breakers",
            not open_brs,
            f"{len(open_brs)} open: {sorted(open_brs)}" if open_brs else "all closed",
        )
    except Exception as exc:
        check("circuit_breakers", False, str(exc))

    failed = [c for c in checks if c["status"] == "fail"]

    # H2 memory_audit: content-level checks (separate from infra status)
    content_checks: list[dict[str, Any]] = []
    db_path = Path(str(connection_manager.base_dir)) / DB_NAME
    if db_path.exists():
        try:
            content_checks = await audit_content(user_id)
        except Exception as exc:  # missing tables on unmigrated DB — audit is advisory
            content_checks = [{"severity": "warn", "type": "audit_error", "items": [str(exc)], "suggestion": "Run migrations."}]

    return {
        "status": "ok" if not failed else "degraded",
        "checks": checks,
        "failed": len(failed),
        "content_checks": content_checks,
    }


async def run_heal(user_id: str = "default", actions: list[str] | None = None) -> dict[str, Any]:
    from shared.connection import connection_manager

    wanted = set(actions) if actions else set(HEAL_ACTIONS)
    unknown = wanted - set(HEAL_ACTIONS)
    if unknown:
        raise ValueError(f"unknown heal actions: {sorted(unknown)}; valid: {list(HEAL_ACTIONS)}")

    healed: list[str] = []
    skipped: list[str] = []
    base = Path(str(connection_manager.base_dir))

    if "remigrate" in wanted:
        from shared.migrations import migration_manager

        await migration_manager.migrate()
        healed.append("remigrate")

    if "reset_breakers" in wanted:
        from shared.circuit_breaker import breaker_registry

        breaker_registry.reset_all()
        healed.append("reset_breakers")

    if "purge_invalid_l1" in wanted:
        purged = 0
        l1_paths = await asyncio.to_thread(lambda: sorted(base.glob("l1_*.json")))
        for p in l1_paths:
            try:
                json.loads(await asyncio.to_thread(p.read_text))
            except (json.JSONDecodeError, OSError):
                await asyncio.to_thread(p.unlink, True)  # buffer re-persists atomically on next add
                purged += 1
        (healed if purged else skipped).append("purge_invalid_l1" if purged else "purge_invalid_l1 (none invalid)")

    return {"status": "ok", "healed": healed, "skipped": skipped}
