"""F-T7 L2 enrichment: пересборка summaries из фактических текстов L0.

L0-строки окна (raw_type='user-message') биндятся к sessions по времени:
ближайшая сессия с started_at <= ts, открытая на ts (ended_at IS NULL
или ended_at >= ts). Summary пересобирается из первых N тем-строк,
state_deltas/topics/quality не трогаются.
"""

from __future__ import annotations

import time

from shared.connection import connection_manager
from shared.constants import DB_NAME

_TOP_LINES = 5


def _topic_lines(texts: list[str], limit: int = _TOP_LINES) -> list[str]:
    # ponytail: тема = нормализованная строка текста; token-frequency апгрейд,
    # когда первых N строк перестанет различать сессии.
    lines: list[str] = []
    seen: set[str] = set()
    for t in texts:
        line = " ".join(t.split())[:100]
        key = line.lower()
        if not line or key in seen:
            continue
        seen.add(key)
        lines.append(f"- {line}")
        if len(lines) >= limit:
            break
    return lines


async def enrich_sessions(*, days: int = 1) -> dict[str, int]:
    """Пересобрать summaries сессий окна из L0-текстов. Возврат счётчиков."""
    from core.session import SessionStore

    await SessionStore(cm=connection_manager)._init_db()  # self-healing schema
    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - days * 86400
    l0_rows = await (
        await conn.execute(
            "SELECT user_id, ts, text FROM l0_journal WHERE ts > ? AND raw_type = 'user-message' ORDER BY ts",
            (cutoff,),
        )
    ).fetchall()
    sessions = await (await conn.execute("SELECT session_id, user_id, started_at, ended_at FROM sessions ORDER BY started_at")).fetchall()

    bound: dict[str, list[str]] = {}
    for r in l0_rows:
        cand = [
            s for s in sessions if s["user_id"] == r["user_id"] and s["started_at"] <= r["ts"] and (s["ended_at"] is None or s["ended_at"] >= r["ts"])
        ]
        if not cand:
            continue
        nearest = max(cand, key=lambda s: s["started_at"])
        bound.setdefault(str(nearest["session_id"]), []).append(str(r["text"]))

    updated = 0
    for sid, texts in bound.items():
        topics = _topic_lines(texts)
        if not topics:
            continue
        await conn.execute("UPDATE sessions SET summary = ? WHERE session_id = ?", ("\n".join(topics), sid))
        updated += 1
    await conn.commit()
    return {"sessions_updated": updated, "l0_bound": sum(len(v) for v in bound.values())}
