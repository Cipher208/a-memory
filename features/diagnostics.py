"""E3: operator diagnostics — health checks + safe auto-fixes. No LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HEAL_ACTIONS = ("remigrate", "reset_breakers", "purge_invalid_l1")


async def run_diagnose(user_id: str = "default") -> dict[str, Any]:
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    base = Path(str(connection_manager.base_dir))
    db_path = base / DB_NAME
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        checks.append({"name": name, "status": "ok" if ok else ("warn" if warn else "fail"), "detail": detail})

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

    # L1 persist files (E1) — valid JSON?
    for p in base.glob("l1_*.json"):
        try:
            json.loads(p.read_text())
            check("l1_file:" + p.name, True)
        except (json.JSONDecodeError, OSError) as exc:
            check("l1_file:" + p.name, False, str(exc))

    # staged proposals backlog
    try:
        from features.staging import list_pending

        pending = await list_pending(user_id, 100)
        check("pending_proposals", len(pending) <= 20, f"{len(pending)} pending", warn=len(pending) > 20)
    except Exception as exc:
        check("pending_proposals", False, str(exc))

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
    return {"status": "ok" if not failed else "degraded", "checks": checks, "failed": len(failed)}


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
        for p in base.glob("l1_*.json"):
            try:
                json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                p.unlink(missing_ok=True)  # buffer re-persists atomically on next add
                purged += 1
        (healed if purged else skipped).append("purge_invalid_l1" if purged else "purge_invalid_l1 (none invalid)")

    return {"status": "ok", "healed": healed, "skipped": skipped}
