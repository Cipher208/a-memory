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
from shared.write_dispatch import WriteDispatch, get_write_dispatch

# Serializes capture() across asyncio tasks AND processes: the hash-chain
# read-head + insert + chain-write must be atomic, or concurrent writers
# fork the chain (chaos-probe finding: two writers chained from the same
# head → verify_chain reported a broken link).
_capture_lock = asyncio.Lock()

logger = logging.getLogger(__name__)

# `received` means "arrived, awaiting processing" and is given an expiry by the
# nightly pass. `parked` means "deliberately sleeping, wake on request" — an
# import that restores a history without distilling it. One status cannot carry
# both meanings: an expiry on `received` would destroy the import's whole point,
# and no expiry leaves every early return accumulating forever. `parked` rows are
# exempt from the expiry, from tiering and from window replay; `replay --ids`
# wakes them by name.
STATUS_RECEIVED = "received"
STATUS_PARKED = "parked"

#: The close reason for a row the capture path captured but never stamped:
#: the body returned or raised before the write happened, so NOTHING was
#: written and the row must be re-enterable. It is the one close reason that
#: records no (gate, config_hash) decision pair -- see `close_row`.
UNACCOUNTED_EXIT = "unaccounted_exit"


def _dispatch() -> WriteDispatch:
    """Return the process-wide dispatcher; L0 never writes on the shared connection."""
    return get_write_dispatch(connection_manager)


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

    A ROW WHOSE WRITE NEVER HAPPENED IS NOT A DUPLICATE. The capture commits
    BEFORE the write, so a failed write leaves the row behind with nothing
    written next to it. Such a row is closed as `gated_out` with the reason
    `unaccounted_exit`, and it is skipped here: the retry has to be able to
    re-enter the write, or the message is lost between the capture and the
    memory. A row that WAS written (or one the gate refused on purpose) still
    blocks the duplicate, exactly as before.
    """
    try:
        rows = await _dispatch().query(
            DB_NAME,
            "SELECT id, status, decisions FROM l0_journal WHERE content_hash=? ORDER BY id",
            (_content_hash(layer, user_id, text),),
        )
    except Exception:
        # A pre-migration DB whose journal has no status/decisions columns: the
        # row cannot be judged, so the old answer stands -- it is a duplicate.
        # (The dedup key column itself may be missing too, in which case the
        # dedup is inactive and there is nothing to find.)
        try:
            prior = await _dispatch().select_one(
                DB_NAME,
                "SELECT id FROM l0_journal WHERE content_hash=? ORDER BY id LIMIT 1",
                (_content_hash(layer, user_id, text),),
            )
        except Exception:
            return None
        return int(prior["id"]) if prior is not None else None
    for row in rows:
        if str(row["status"] or "") == STATUS_RECEIVED:
            # Read-time heal (spec 2026-10-22, S4): the row's status is a
            # claim, not a fact. Judge it by what memory holds before letting
            # it block the duplicate: unaccounted -> nothing was written, the
            # retry re-enters; anything else (saved/promoted/fresh/error)
            # -> the old answer stands and the block holds. The VERDICT is
            # authoritative, not a re-read of the status: close_row may fail
            # to write silently, and the verdict still describes what memory
            # holds (the row then heals on the next sweep).
            try:
                verdict = await classify_received(int(row["id"]))
            except Exception:
                verdict = None  # classify's contract is never-raise; keep find_block's own guarantee local
            if verdict == UNACCOUNTED_EXIT:
                continue
            return int(row["id"])
        if _write_never_happened(row):
            continue
        return int(row["id"])
    return None


def _write_never_happened(row: Any) -> bool:
    """Report whether the row was closed because the write never happened.

    The close reason is the marker: `unaccounted_exit` says the pipeline
    left the row without stamping it, which means the write did not happen.
    The status has to agree, and the marker only holds while no real verdict
    exists: once a close has recorded a (gate, config_hash) pair, the write
    DID happen and the row blocks duplicates again.
    """
    try:
        status = str(row["status"] or "")
        decisions_raw = row["decisions"]
    except (KeyError, IndexError, TypeError):
        return False
    if status != "gated_out":
        return False
    return _awaiting_verdict(decisions_raw)


def _awaiting_verdict(decisions_raw: Any) -> bool:
    """Report whether a closed row still awaits a verdict. One predicate, two readers.

    A row closed as an unaccounted exit with NO config-pair verdict still awaits
    one: the write never happened, so there is nothing to skip and nothing to
    block. That is the retryable state, and it is a STATE — not a permanent key.
    Once any close has appended `{"gate", "config_hash"}`, the row HAS a verdict:
    the marker is history, and both readers must stop honouring it.

    The defect this closes: the marker was never cleared, so a row carrying BOTH
    `unaccounted_exit` and a real verdict kept the exemption. Every later closer
    won (verified: three consecutive closes all returned True), and the
    duplicate guard re-entered a row whose write had already happened.

    Both readers are here because they must agree. `_write_never_happened` asks
    "may a retry re-enter the write?"; `close_row` asks "may this close still
    stamp the row?". They are the same question about the same state.
    """
    try:
        decisions = json.loads(decisions_raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(decisions, list):
        return False
    has_marker = any(isinstance(d, dict) and d.get("reason") == UNACCOUNTED_EXIT for d in decisions)
    has_verdict = any(isinstance(d, dict) and d.get("gate") and d.get("config_hash") for d in decisions)
    return has_marker and not has_verdict


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
        # atomic() owns the commit and rolls back if anything raises.
        async with _capture_lock:
            captured = await _dispatch().atomic(
                DB_NAME,
                lambda conn: _capture_inner(conn, event, layer, user_id, text, source_msg_id, raw_type, decisions, ts_override, park),
            )
        return int(captured) if captured is not None else None
    except Exception:
        return None


async def _capture_inner(
    conn: Any,
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
        ts = ts_override or time.time()
        rt = raw_type or classify_raw(text)
        content_hash = _content_hash(layer, user_id, text)
        # S17 #5: dedup of an already-stored block — a repeat does not create a row;
        # the rid of the original record is returned (link to the first entry). No
        # content_hash column (pre-g23-migration DB) → dedup inactive, we write as is.
        try:
            prior = await (
                await conn.execute(
                    "SELECT id FROM l0_journal WHERE content_hash=? ORDER BY id LIMIT 1",
                    (_content_hash(layer, user_id, text),),
                )
            ).fetchone()
        except Exception:  # pre-migration DB without content_hash
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
        return rid
    except Exception as exc:
        logger.debug("l0 capture failed, transaction will roll back: %s", exc)
        raise


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


async def _archived_chain_rows(dispatch: WriteDispatch) -> list[tuple[Any, ...]]:
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
        cols = {r["name"] for r in await dispatch.query(DB_NAME, "PRAGMA table_info(l0_cold_archive)")}
        if not {"hash_prev", "hash_self"} <= cols:
            return []
        rows = await dispatch.query(DB_NAME, "SELECT id, hash_prev, hash_self, raw_type, ts, text FROM l0_cold_archive ORDER BY id")
    except Exception:
        return []
    return [(int(r["id"]), r["hash_prev"] or "", r["hash_self"] or "", r["raw_type"], r["ts"], r["text"] or "") for r in rows]


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
        dispatch = _dispatch()
        jcols = {r["name"] for r in await dispatch.query(DB_NAME, "PRAGMA table_info(l0_journal)")}
        if not jcols:
            return [{"id": -1, "error": "verify failed"}]
        has_z = "text_z" in jcols
        cols = "id, hash_prev, hash_self, raw_type, ts, text" + (", text_z" if has_z else "")
        jrows = await dispatch.query(DB_NAME, f"SELECT {cols} FROM l0_journal ORDER BY id")
        combined: list[tuple[Any, ...]] = [
            (
                int(r["id"]),
                r["hash_prev"] or "",
                r["hash_self"] or "",
                r["raw_type"],
                r["ts"],
                _captured_text(r["text"], r["text_z"] if has_z else None),
            )
            for r in jrows
        ]
        combined.extend(await _archived_chain_rows(dispatch))
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


async def close_row(
    l0_id: int,
    status: str,
    *,
    reason: str | None = None,
    expect_status: str = STATUS_RECEIVED,
) -> bool:
    """Give one captured row a terminal status, from the status it expects. Never raises.

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

    THE ONE EXCEPTION IS AN UNACCOUNTED EXIT. That pair means "the gate
    refused this deliberately", and an unaccounted exit is the opposite: the
    write never happened, so the row is not a decision at all. Recording the
    pair would make the nightly replay skip the very row that still needs
    rescuing — the message would sit in the journal forever, refused by its
    own failed attempt. So that close records the reason alone, and the reason
    is what tells the duplicate guard (`find_block`) that the row may be
    written again.

    `gate` is recorded as "g1" to match every `gated_out` row already in the live
    bases and the default of `l0_cli.py replay --gate`. The true cause lives in
    `reason`, because the importance gate is usually what refused these, not the
    distiller.

    Callers pass `reason=None` when their own entry already wrote the content
    addressably (a save, a `routed_direct`) — there is then no refusal to explain,
    and adding a decision entry would make an unchanged config treat a legitimate
    save as skippable.

    WHY THIS IS A COMPARE-AND-SET. Two owners close the same row: the live hook
    right after capture, and the nightly sweep/replay. Without a status
    precondition, whichever wrote last won — a row already saved to L3 could be
    overwritten with `gated_out` by a later sweep, and the record then claimed a
    refusal that never happened. The update therefore applies only while the row
    still carries `expect_status` (default `received`) and the result says
    whether this caller won. A lost CAS is not an error — the row belongs to
    another writer — so it returns `False` and logs at debug.

    AN UNACCOUNTED EXIT STILL AWAITS ITS VERDICT — UNTIL ONE EXISTS. The one
    caller that does not close a freshly captured row is the retry: `capture`
    hands back the dedup row, `find_block` lets the retry re-enter the write, and
    the close then arrives at a row already stamped `gated_out`/`unaccounted_exit`.
    That row carries no verdict — the write never happened — so it is treated
    exactly like `received` for the default `expect_status`. Without this the
    retry would write the memory and leave the journal claiming a refusal
    (measured: the delivery-contract retry test).

    The exemption is the STATE, not the marker: it holds only while the decisions
    carry no (gate, config_hash) pair (`_awaiting_verdict`). Once a close has
    recorded a real verdict, the row belongs to that verdict and a later close
    loses like any other — otherwise the marker would be a permanent skeleton key.

    The read and the write share ONE `BEGIN IMMEDIATE`, so two closers cannot
    both read the same decisions list and append to it. The guard is
    `conn.in_transaction`, NOT a swallowed exception: a BEGIN that fails with
    SQLITE_BUSY leaves the read outside any transaction, where it can see the
    pre-commit state of the writer that holds the lock — that writer then
    commits, the status never changed, and this UPDATE would overwrite its
    append. A failed BEGIN must abort the close, not silently continue.
    """
    try:
        now = time.time()
        from features.replay import config_hash

        async def _txn(conn: Any) -> bool:
            # BEGIN IMMEDIATE: take the write lock before reading the row, so two
            # closers cannot append to the same base list. `in_transaction` is the
            # repo's nesting check: when the caller already holds a transaction
            # this joins it instead of raising "cannot start a transaction within
            # a transaction". Swallowing that error would also swallow a real
            # SQLITE_BUSY and read outside any lock — see the docstring.
            if not conn.in_transaction:
                await conn.execute("BEGIN IMMEDIATE")
            row = await (await conn.execute("SELECT status, decisions FROM l0_journal WHERE id=?", (l0_id,))).fetchone()
            if row is None:
                return False
            current = str(row["status"] or "")
            awaiting = current == expect_status or (
                expect_status == STATUS_RECEIVED and current == "gated_out" and _awaiting_verdict(row["decisions"])
            )
            if not awaiting:
                return False
            if reason is None:
                cur = await conn.execute(
                    "UPDATE l0_journal SET status=?, processed_at=? WHERE id=? AND status=?",
                    (status, now, l0_id, current),
                )
                return int(cur.rowcount or 0) == 1
            try:
                decisions = json.loads(row["decisions"] or "[]")
            except (json.JSONDecodeError, TypeError):
                decisions = []
            if reason == UNACCOUNTED_EXIT:
                decisions.append({"reason": reason, "ts": now})
            else:
                decisions.append({"gate": "g1", "config_hash": config_hash(), "ts": now, "reason": reason})
            cur = await conn.execute(
                "UPDATE l0_journal SET status=?, processed_at=?, decisions=? WHERE id=? AND status=?",
                (status, now, json.dumps(decisions, ensure_ascii=False), l0_id, current),
            )
            return int(cur.rowcount or 0) == 1

        won = await _dispatch().atomic(DB_NAME, _txn)
        if not won:
            logger.debug("close_row(%s, %s): row is not %s any more, left alone", l0_id, status, expect_status)
        return won
    except Exception as exc:
        # Best-effort, like `capture`: a watermark failure must not break the write
        # path that already succeeded. Logged at debug because the caller's flow is
        # what matters; the nightly expiry is the backstop that closes the row.
        logging.getLogger(__name__).debug("l0_journal status update failed: %s", exc)
        return False


async def classify_received(l0_id: int, *, grace_s: float = 600.0) -> str | None:
    """Judge one stranded `received` row by evidence in MEMORY. Never raises.

    `received` was written by every writer and read by nobody, so rows whose
    write never happened sat in the journal forever (41 rows on two live
    bases, the oldest 5.6 days). The verdict comes from what memory HOLDS,
    not from the row:

      - "promoted_l4"      -- an L4 fact carries metadata.source_raw_id == l0_id
      - "saved_l3"         -- an L3 episode carries the tag `raw:<id>`
      - UNACCOUNTED_EXIT   -- no evidence anywhere: nothing was written, the
                              row is re-enterable (see `close_row`)
      - None               -- not ours to judge: inside `grace_s`, no longer
                              `received`, or the reader failed -- the old
                              answer stands

    Evidence is scoped by (layer, user_id): a raw id from another scope proves
    nothing (indices exist on both tables for exactly this).

    The exception boundary is HERE, and it logs a WARNING itself: every caller
    (find_block on the message path, replay, the nightly sweep) must keep its
    old answer when the reader is broken, and an exception that escapes to the
    caller would be swallowed silently there.
    """
    try:
        row = await _dispatch().select_one(
            DB_NAME,
            "SELECT layer, user_id, ts, status FROM l0_journal WHERE id=?",
            (l0_id,),
        )
        if row is None or str(row["status"] or "") != STATUS_RECEIVED:
            return None
        if time.time() - float(row["ts"] or 0) < grace_s:
            return None
        layer = str(row["layer"] or "user")
        user_id = str(row["user_id"] or "")
        l4_rows = await _dispatch().query(
            DB_NAME,
            "SELECT metadata FROM core_memory WHERE layer=? AND user_id=? AND metadata LIKE ?",
            (layer, user_id, '%"source_raw_id"%'),
        )
        for r in l4_rows:
            try:
                meta = json.loads(r["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue  # corrupted metadata is not evidence either way
            if isinstance(meta, dict) and meta.get("source_raw_id") == l0_id:
                await close_row(l0_id, "promoted_l4")
                return "promoted_l4"
        l3 = await _dispatch().select_one(
            DB_NAME,
            "SELECT 1 FROM episodes WHERE layer=? AND user_id=? AND tags LIKE ? LIMIT 1",
            (layer, user_id, f'%"raw:{l0_id}"%'),
        )
        if l3 is not None:
            await close_row(l0_id, "saved_l3")
            return "saved_l3"
        await close_row(l0_id, "gated_out", reason=UNACCOUNTED_EXIT)
        return UNACCOUNTED_EXIT
    except Exception as exc:
        logger.warning("classify_received(%s) failed, row left as is: %s", l0_id, exc)
        return None


async def sweep_received(*, grace_s: float = 600.0) -> dict[str, int]:
    """Classify every stranded `received` row. Never raises.

    Nightly BEFORE the replay (features/backup_cron) and once manually on a
    live base: case-b becomes a terminal duplicate the replay must not touch,
    case-a becomes re-enterable and is rescued by the same night's replay
    (rows inside replay's 7-day window; older ones stay wakeable via
    `l0_cli replay --ids`). No age window here — `received` is never tiered
    and never archived, so stranded rows live until someone reads them;
    `grace_s` still protects rows a writer may be stamping right now.
    """
    counts = {"saved_l3": 0, "promoted_l4": 0, "unaccounted_exit": 0, "fresh": 0}
    try:
        rows = await _dispatch().query(
            DB_NAME,
            # `raw_type != 'import'` mirrors the second line of defence in
            # close_overdue_received (lifecycle/l0_tiers.py): migration g27
            # renames those rows to `parked`, and this sweep — the only closer
            # with no age window — keeps them safe if that migration has not
            # run or failed halfway. The cost of being wrong is one
            # owner-requested history silently closed by tier_l0 the same night.
            "SELECT id FROM l0_journal WHERE status=? AND raw_type != 'import'",
            (STATUS_RECEIVED,),
        )
    except Exception as exc:
        logger.warning("sweep_received: cannot read the journal: %s", exc)
        return counts
    for r in rows:
        try:
            verdict = await classify_received(int(r["id"]), grace_s=grace_s)
        except Exception as exc:  # classify's contract is never-raise; keep sweep's own guarantee local (mirrors find_block/replay guards)
            logger.warning("sweep_received: classify failed for row %s: %s", r["id"], exc)
            continue
        if verdict is None:
            counts["fresh"] += 1
        else:
            counts[verdict] += 1
    return counts


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
