"""L0 raw intake — единственный вход конвейера (append-only, best-effort)."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def capture(
    event: str,
    layer: str,
    user_id: str,
    text: str,
    *,
    source_msg_id: int | None = None,
    raw_type: str | None = None,
    decisions: list[dict[str, Any]] | None = None,
    ts_override: float | None = None,
) -> int | None:
    """Append-only intake. Никогда не бросает — сбой L0 не блокирует поток."""
    try:
        conn = await connection_manager.get(DB_NAME)
        ts = ts_override or time.time()
        rt = raw_type or classify_raw(text)
        cur = await conn.execute(
            "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)",
            (
                ts,
                event,
                source_msg_id,
                layer,
                user_id,
                text,
                rt,
                json.dumps(decisions or [], ensure_ascii=False),
            ),
        )
        rid = int(cur.lastrowid or 0)
        # hash-chain (S1, tamper-evidence): сбой цепочки не блокирует запись
        prev = await (await conn.execute("SELECT hash_self FROM l0_journal WHERE id < ? ORDER BY id DESC LIMIT 1", (rid,))).fetchone()
        hash_prev = (prev[0] if prev else "") or ""
        digest = hashlib.sha256(f"{hash_prev}|{rt}|{ts}|{text}"[:200].encode()).hexdigest()[:16]
        await conn.execute("UPDATE l0_journal SET hash_prev=?, hash_self=? WHERE id=?", (hash_prev, digest, rid))
        await conn.commit()
        return rid
    except Exception:
        return None


async def verify_chain() -> list[dict[str, Any]]:
    """Пересчитать hash-chain по всем записям l0_journal → битые записи.

    Тампер одной записи ломает пересчёт у неё и у всех последующих
    (chain-природа), так что здесь обрезаем до первой битой.
    """
    try:
        conn = await connection_manager.get(DB_NAME)
        rows = list(await (await conn.execute("SELECT id, hash_prev, hash_self, raw_type, ts, text FROM l0_journal ORDER BY id")).fetchall())
    except Exception:
        return [{"id": -1, "error": "verify failed"}]
    broken: list[dict[str, Any]] = []
    expected_prev = ""
    for rid, hash_prev, hash_self, rt, ts, text in rows:
        digest = hashlib.sha256(f"{expected_prev}|{rt}|{ts}|{text}"[:200].encode()).hexdigest()[:16]
        if hash_prev != expected_prev or hash_self != digest:
            broken.append({"id": rid, "hash_prev": hash_prev, "hash_self": hash_self, "expected": digest})
            break
        expected_prev = hash_self
    return broken


def classify_raw(text: str) -> str:
    t = text.strip()
    if t.startswith(("[{", '{"')):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and obj.get("type") == "tool_result":
                return "tool_result"
            if isinstance(obj, dict) and obj.get("type") == "tool_use":
                return "tool_use"
        except ValueError:
            pass
        return "tool_result" if "tool_use_id" in t[:200] else "plain"
    for prefix in ("[ariel recall]", "[ariel memory]", "[ariel proposals]"):
        if t.startswith(prefix):
            return "recall"
    if t.startswith("[EVOLUTION]"):
        return "evolution"
    if "tool_use_id" in t[:200]:
        return "tool_result"
    return "user-message"
