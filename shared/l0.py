"""L0 raw intake — the pipeline's single entry point (append-only, best-effort)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import zlib
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.fractional_index import midpoint

# Serializes capture() across asyncio tasks AND processes: the hash-chain
# read-head + insert + chain-write must be atomic, or concurrent writers
# fork the chain (chaos-probe finding: two writers chained from the same
# head → verify_chain reported a broken link).
_capture_lock = asyncio.Lock()

# `received` means "arrived, awaiting processing" and is given an expiry by the
# nightly pass. `parked` means "deliberately sleeping, wake on request" — an
# import that restores a history without distilling it. One status cannot carry
# both meanings: an expiry on `received` would destroy the import's whole point,
# and no expiry leaves every early return accumulating forever. `parked` rows are
# exempt from the expiry, from tiering and from window replay; `replay --ids`
# wakes them by name.
STATUS_RECEIVED = "received"
STATUS_PARKED = "parked"


def _content_hash(layer: str, user_id: str, text: str) -> str:
    """S17 #5 dedup key: identical (layer, user_id, text) is the same block."""
    return hashlib.sha256(f"{layer}|{user_id}|{text}".encode()).hexdigest()


async def find_block(layer: str, user_id: str, text: str) -> int | None:
    """Return the rid of an already-captured identical block, or None. Never raises.

    capture() uses this to return the prior rid instead of inserting a second
    row. Callers that run a pipeline after capture use it to skip a replay
    outright: capture() guards the journal, not the pipeline — a replayed
    message returned the same rid while the distiller re-emitted every clause
    under that one `raw:<rid>` tag (raw:164 → 120 rows across three days,
    decaying 60/40/20). Pre-migration DB without the content_hash column →
    None (dedup inactive, write as is).
    """
    try:
        conn = await connection_manager.get(DB_NAME)
        prior = await (
            await conn.execute(
                "SELECT id FROM l0_journal WHERE content_hash=? ORDER BY id LIMIT 1",
                (_content_hash(layer, user_id, text),),
            )
        ).fetchone()
    except Exception:
        return None
    return int(prior["id"]) if prior is not None else None


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
    park: bool = False,
) -> int | None:
    """Append-only intake. Never raises — an L0 failure must not block the flow.

    S17 #5: SHA-256 block dedup — re-emitting the same output (same text and
    layer/user, content_hash column) does not create a row; the rid of the
    first record is returned. A dedup hit writes no row of its own and therefore
    no chain link either; the chain stays whole because it covers the rows that
    exist, not the attempts that were folded into them.

    `park=True` writes STATUS_PARKED instead of STATUS_RECEIVED: the row is a
    deliberate "keep the raw text, decide later" import, exempt from the nightly
    expiry. See the comment on the two statuses above.
    """
    try:
        # In-process serialization; cross-process safety comes from
        # BEGIN IMMEDIATE below (SQLite single-writer) plus the CAS-style
        # chain write (UPDATE only applies if the chain head is unchanged).
        async with _capture_lock:
            return await _capture_inner(event, layer, user_id, text, source_msg_id, raw_type, decisions, ts_override, park)
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
    park: bool = False,
) -> int | None:
    try:
        conn = await connection_manager.get(DB_NAME)
        ts = ts_override or time.time()
        rt = raw_type or classify_raw(text)
        content_hash = _content_hash(layer, user_id, text)
        # S17 #5: dedup of an already-stored block — a repeat does not create a row;
        # the rid of the original record is returned (link to the first entry). No
        # content_hash column (pre-g23-migration DB) → dedup inactive, we write as is.
        prior_id = await find_block(layer, user_id, text)
        if prior_id is not None:
            return prior_id
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
        status = STATUS_PARKED if park else STATUS_RECEIVED
        params = (ts, event, source_msg_id, layer, user_id, text, rt, status, json.dumps(decisions or [], ensure_ascii=False))
        try:
            cur = await conn.execute(
                "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions, order_key, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*params, order_key, content_hash),
            )
        except Exception:  # content_hash/order_key columns not present yet (pre-migration DB) — write without them
            cur = await conn.execute(
                "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def _captured_text(text: Any, text_z: Any) -> str:
    """Return the text the chain was computed over, undoing the warm tier.

    The digest covers the text as it was CAPTURED, but the warm tier replaces
    `text` with an extractive preview and leaves the full value only
    zlib-compressed in `text_z`. Recomputing over the preview made a correctly
    tiered row read as tampered with — the first row to cross 30 days would have
    broken the chain by being processed exactly as designed.
    """
    if not text_z:
        return text or ""
    try:
        return zlib.decompress(bytes(text_z)).decode("utf-8")
    except Exception:
        return text or ""


async def _archived_chain_rows(conn: Any) -> list[tuple[Any, ...]]:
    """Chain rows from l0_cold_archive in id order, or [] when they cannot be trusted.

    Tiering DELETES a row from the journal once it is archived, and the chain was
    recomputed over the journal alone — so the next surviving row carried a
    `hash_prev` naming a row that no longer existed and the chain read as broken
    at the very moment tiering did its job. The archive carries its own
    hash_prev/hash_self, which is what lets the union be walked in capture order.
    Bases whose archive predates those columns contribute no rows: their chain
    starts at the oldest surviving journal row, and that gap is visible as such
    rather than reported as tampering.
    """
    try:
        cols = {r[1] for r in await (await conn.execute("PRAGMA table_info(l0_cold_archive)")).fetchall()}
        if not {"hash_prev", "hash_self"} <= cols:
            return []
        rows = await (await conn.execute("SELECT id, hash_prev, hash_self, raw_type, ts, text FROM l0_cold_archive ORDER BY id")).fetchall()
    except Exception:
        return []
    return [(int(r[0]), r[1] or "", r[2] or "", r[3], r[4], r[5] or "") for r in rows]


async def verify_chain() -> list[dict[str, Any]]:
    """Recompute the hash-chain over every captured row → broken records.

    The chain spans the journal AND the cold archive, because tiering moves rows
    between them: a link that did not survive the move would fail its own
    tiering. Text is read as captured, so a warm row is verified against the
    full text decompressed from `text_z` rather than against its preview.

    Rows are ordered by id, which is capture order — not by ts, because imported
    messages carry original timestamps and would otherwise interleave wrongly.

    Tampering with one record breaks the recomputation for it and for every
    subsequent record (chain nature), so the scan stops at the first broken
    entry. Both formats are accepted: v2 (full text, current) and v1
    ([:200] truncation, records predating the removal of the crypto weakness).
    """
    try:
        conn = await connection_manager.get(DB_NAME)
        jcols = {r[1] for r in await (await conn.execute("PRAGMA table_info(l0_journal)")).fetchall()}
        if not jcols:
            return [{"id": -1, "error": "verify failed"}]
        has_z = "text_z" in jcols
        cols = "id, hash_prev, hash_self, raw_type, ts, text" + (", text_z" if has_z else "")
        jrows = list(await (await conn.execute(f"SELECT {cols} FROM l0_journal ORDER BY id")).fetchall())
        combined: list[tuple[Any, ...]] = [
            (int(r[0]), r[1] or "", r[2] or "", r[3], r[4], _captured_text(r[5], r[6] if has_z else None)) for r in jrows
        ]
        combined.extend(await _archived_chain_rows(conn))
        combined.sort(key=lambda r: r[0])
    except Exception:
        return [{"id": -1, "error": "verify failed"}]
    broken: list[dict[str, Any]] = []
    expected_prev = ""
    for rid, hash_prev, hash_self, rt, ts, text in combined:
        digest = _chain_digest(expected_prev, rt, ts, text)
        digest_v1 = _chain_digest_v1(expected_prev, rt, ts, text)
        if hash_prev != expected_prev or (hash_self != digest and hash_self != digest_v1):
            broken.append({"id": rid, "hash_prev": hash_prev, "hash_self": hash_self, "expected": digest})
            break
        expected_prev = hash_self
    return broken


async def close_row(l0_id: int, status: str, *, reason: str | None = None) -> None:
    """Give one captured row a terminal status. Never raises.

    WHY THIS IS HERE AND NOT IN THE HOOK. `capture` writes `received` and six
    different call sites are responsible for stamping it afterwards — the external
    hook, the agent hook, `think`, `remember`, the bridge and the importers. Three
    of them simply never did, so the rows they captured sat `received` forever:
    `received` is the status `l0_tiers` promises never to tier or archive, and
    nothing else reads it, so those rows accumulated with no reader and no expiry
    (353 on one live base, the oldest from 26.07). A watermark helper that only the
    first call site can reach is how the other two were missed; this lives next to
    `capture` so every writer can close what it wrote.

    WHY `reason` MATTERS AS MUCH AS THE STATUS. A closed row is only honest if a
    later `replay` can tell "deliberately closed" from "never processed". Replay
    skips rows whose `decisions` already record the current (gate, config_hash)
    pair, so the same pair is written here. The effect is exactly right: replay
    under an unchanged config leaves the row alone, and replay after the config
    changed re-opens it — which is the point of `config_hash`, and the reason a
    fixed distiller can reconsider rows a broken one refused.

    `gate` is recorded as "g1" to match every `gated_out` row already in the live
    bases and the default of `l0_cli.py replay --gate`. The true cause lives in
    `reason`, because the importance gate is usually what refused these, not the
    distiller.

    Callers pass `reason=None` when their own entry already wrote the content
    addressably (a save, a `routed_direct`) — there is then no refusal to explain,
    and adding a decision entry would make an unchanged config treat a legitimate
    save as skippable.
    """
    from shared.connection import connection_manager

    try:
        conn = await connection_manager.get(DB_NAME)
        now = time.time()
        if reason is None:
            await conn.execute(
                "UPDATE l0_journal SET status=?, processed_at=? WHERE id=?",
                (status, now, l0_id),
            )
        else:
            from features.replay import config_hash

            row = await (await conn.execute("SELECT decisions FROM l0_journal WHERE id=?", (l0_id,))).fetchone()
            try:
                decisions = json.loads((row["decisions"] if row else None) or "[]")
            except (json.JSONDecodeError, TypeError):
                decisions = []
            decisions.append({"gate": "g1", "config_hash": config_hash(), "ts": now, "reason": reason})
            await conn.execute(
                "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=?",
                (status, now, json.dumps(decisions, ensure_ascii=False), l0_id),
            )
        await conn.commit()
    except Exception as exc:
        # Best-effort, like `capture`: a watermark failure must not break the write
        # path that already succeeded. Logged at debug because the caller's flow is
        # what matters; the nightly expiry is the backstop that closes the row.
        logging.getLogger(__name__).debug("l0_journal status update failed: %s", exc)


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
