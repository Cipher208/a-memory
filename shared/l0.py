"""L0 raw intake — the pipeline's single entry point (append-only, best-effort)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.fractional_index import midpoint

# Serializes capture() across asyncio tasks AND processes: the hash-chain
# read-head + insert + chain-write must be atomic, or concurrent writers
# fork the chain (chaos-probe finding: two writers chained from the same
# head → verify_chain reported a broken link).
_capture_lock = asyncio.Lock()


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
    """Append-only intake. Never raises — an L0 failure must not block the flow.

    S17 #5: SHA-256 block dedup — re-emitting the same output (same text and
    layer/user, content_hash column) does not create a row; the rid of the
    first record is returned. Hash-chain v2 is maintained for EVERY capture
    attempt (including dedup hits), so tamper-evidence does not depend on dedup.
    """
    try:
        # In-process serialization; cross-process safety comes from
        # BEGIN IMMEDIATE below (SQLite single-writer) plus the CAS-style
        # chain write (UPDATE only applies if the chain head is unchanged).
        async with _capture_lock:
            return await _capture_inner(event, layer, user_id, text, source_msg_id, raw_type, decisions, ts_override)
    except Exception:
        return None


async def _capture_inner(
    event: str,
    layer: str,
    user_id: str,
    text: str,
    source_msg_id: int | None,
    raw_type: str | None,
    decisions: list[dict[str, Any]] | None,
    ts_override: float | None,
) -> int | None:
    try:
        conn = await connection_manager.get(DB_NAME)
        ts = ts_override or time.time()
        rt = raw_type or classify_raw(text)
        content_hash = hashlib.sha256(f"{layer}|{user_id}|{text}".encode()).hexdigest()
        # S17 #5: dedup of an already-stored block — a repeat does not create a row;
        # the rid of the original record is returned (link to the first entry). No
        # content_hash column (pre-g23-migration DB) → dedup inactive, we write as is.
        try:
            prior = await (
                await conn.execute(
                    "SELECT id FROM l0_journal WHERE content_hash=? ORDER BY id LIMIT 1",
                    (content_hash,),
                )
            ).fetchone()
        except Exception:
            prior = None
        if prior is not None:
            return int(prior["id"])
        # S1 order_key: fractional index after the last row. The column may be absent
        # in live pre-migration DBs — then we write without order_key.
        # BEGIN IMMEDIATE: take the write lock BEFORE reading the chain head so
        # concurrent processes cannot chain from the same head (chaos finding).
        # If a transaction is already open (pre-migration callers) this is a no-op.
        with contextlib.suppress(Exception):
            await conn.execute("BEGIN IMMEDIATE")
        prev: Any | None = None
        try:
            prev = await (await conn.execute("SELECT hash_self, order_key FROM l0_journal ORDER BY id DESC LIMIT 1")).fetchone()
        except Exception:
            with contextlib.suppress(Exception):
                prev = await (await conn.execute("SELECT hash_self FROM l0_journal ORDER BY id DESC LIMIT 1")).fetchone()
        prev_key: str | None = None
        if prev is not None:
            with contextlib.suppress(IndexError):  # fallback SELECT without order_key
                prev_key = prev[1]
        order_key: str | None = None
        with contextlib.suppress(Exception):
            order_key = midpoint(prev_key) if prev_key else midpoint(None)
        params = (ts, event, source_msg_id, layer, user_id, text, rt, json.dumps(decisions or [], ensure_ascii=False))
        try:
            cur = await conn.execute(
                "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions, order_key, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, ?)",
                (*params, order_key, content_hash),
            )
        except Exception:  # content_hash/order_key columns not present yet (pre-migration DB) — write without them
            cur = await conn.execute(
                "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)",
                params,
            )
        rid = int(cur.lastrowid or 0)
        # hash-chain (S1, tamper-evidence): a chain failure does not block the write.
        # v2: full text (the [:200] truncation allowed collisions between records with
        # a shared prefix); v1 is the historical format, verify_chain accepts both.
        # Inside the BEGIN IMMEDIATE window, so head-read and chain-write are atomic
        # across processes.
        hash_prev = (prev[0] if prev is not None else "") or ""
        digest = hashlib.sha256(f"{hash_prev}|{rt}|{ts}|{text}".encode()).hexdigest()[:16]
        await conn.execute("UPDATE l0_journal SET hash_prev=?, hash_self=? WHERE id=?", (hash_prev, digest, rid))
        await conn.commit()
        return rid
    except Exception:
        with contextlib.suppress(Exception):
            await conn.execute("ROLLBACK")  # release the IMMEDIATE lock on any failure
        return None


def _chain_digest(hash_prev: str, rt: str, ts: float, text: str) -> str:
    """Compute the record's hash_self. v2 — full text; v1 — [:200] truncation (historical)."""
    return hashlib.sha256(f"{hash_prev}|{rt}|{ts}|{text}".encode()).hexdigest()[:16]


def _chain_digest_v1(hash_prev: str, rt: str, ts: float, text: str) -> str:
    return hashlib.sha256(f"{hash_prev}|{rt}|{ts}|{text}"[:200].encode()).hexdigest()[:16]


async def verify_chain() -> list[dict[str, Any]]:
    """Recompute the hash-chain over all l0_journal rows → broken records.

    Tampering with one record breaks the recomputation for it and for every
    subsequent record (chain nature), so the scan stops at the first broken
    entry. Both formats are accepted: v2 (full text, current) and v1
    ([:200] truncation, records predating the removal of the crypto weakness).
    """
    try:
        conn = await connection_manager.get(DB_NAME)
        rows = list(await (await conn.execute("SELECT id, hash_prev, hash_self, raw_type, ts, text FROM l0_journal ORDER BY id")).fetchall())
    except Exception:
        return [{"id": -1, "error": "verify failed"}]
    broken: list[dict[str, Any]] = []
    expected_prev = ""
    for rid, hash_prev, hash_self, rt, ts, text in rows:
        digest = _chain_digest(expected_prev, rt, ts, text)
        digest_v1 = _chain_digest_v1(expected_prev, rt, ts, text)
        if hash_prev != expected_prev or (hash_self != digest and hash_self != digest_v1):
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
