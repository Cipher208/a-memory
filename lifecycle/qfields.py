"""Q-fields enrichment (CLACK exp3, 2026-09-08): doc2query for rag units.

Nightly job: for rag_pages without qfields_json, an LLM generates 3 hypothetical
queries this unit should answer. The |qt ∩ qf| boost is added in rag/dual_route
(weight retrieval.qfields.weight, CLACK qb=2.0).

Honesty invariants (exp3 spec §3):
- LLM input is ONLY the unit text (no eval questions, no query history);
- idempotency: units that already have qfields_json are skipped;
- cost cap: at most retrieval.qfields.max_units_per_night per run;
- config-only flag retrieval.qfields.enabled (no env overrides);
- failures are results: an LLM failure on a unit is recorded, it never crashes the job.
"""

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

from config import config
from shared.connection import connection_manager

logger = logging.getLogger(__name__)

DB_NAME = "memory.db"
_QF_PROMPT = (
    "Given the text below, write exactly 3 short hypothetical search queries "
    "(5-12 words each, one per line) that this text would be the right answer "
    "for. Queries only, no numbering.\n\n%s"
)
_TIMEOUT_S = 30


def _load_llm_config(path: str) -> dict[str, str] | None:
    """Parse api_config.json → {base_url, api_key, model}; None = the job sleeps."""
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        base_url, key, model = raw.get("base_url", ""), raw.get("api_key", ""), raw.get("model", "")
        if base_url and key and model:
            return {"base_url": base_url, "api_key": key, "model": model}
    except Exception:
        logger.warning("qfields: llm_config_path unreadable", exc_info=True)
    return None


def _call_llm(cfg: dict[str, str], text: str) -> list[str]:
    """Extract 3 queries from the unit body; enable_thinking:false (a thinking model eats tokens)."""
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": _QF_PROMPT % text[:3000]}],
        "max_tokens": 60,
        "temperature": 0.2,
        "enable_thinking": False,
    }
    req = urllib.request.Request(  # noqa: S310 — https-only base_url from the owner's config
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
    )
    if not cfg["base_url"].startswith("https://"):
        raise ValueError("qfields: non-https base_url rejected")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        out = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
    lines = [ln.strip("-•0123456789. \t") for ln in out.splitlines() if ln.strip()]
    return [ln for ln in lines if ln][:3]


async def qfield_enrich(layer: str = "user") -> dict[str, Any]:
    """Nightly job: backfill q-fields for units that lack them. Returns a report dict."""
    result: dict[str, Any] = {"generated": 0, "skipped_done": 0, "failed": 0, "asleep": True}
    if not bool(config.get("retrieval", "qfields", "enabled", default=False)):
        return result
    cfg = _load_llm_config(str(config.get("retrieval", "qfields", "llm_config_path", default="")))
    if cfg is None:
        logger.info("qfields: enabled but llm_config_path empty — sleeping")
        return result
    cap = int(config.get("retrieval", "qfields", "max_units_per_night", default=100))
    result["asleep"] = False

    conn = await connection_manager.get(DB_NAME)
    rows = await (
        await conn.execute(
            f"SELECT id, content FROM rag_pages WHERE layer=? AND qfields_json IS NULL "
            f"AND content IS NOT NULL AND length(content) > 0 LIMIT {int(cap)}",
            (layer,),
        )
    ).fetchall()
    if not rows:
        return result

    done = 0
    for row in rows:
        try:
            queries = _call_llm(cfg, str(row["content"]))
        except Exception as exc:
            result["failed"] += 1
            logger.warning("qfields: unit %s failed: %s", row["id"], exc)
            continue
        if not queries:
            result["failed"] += 1
            continue
        await conn.execute("UPDATE rag_pages SET qfields_json=? WHERE id=?", (json.dumps(queries, ensure_ascii=False), row["id"]))
        done += 1
    await conn.commit()
    result["generated"] = done
    result["skipped_done"] = max(0, len(rows) - done - result["failed"])
    return result


async def apply_qfield_boost(cm: Any, hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Lexical query→unit bridge (CLACK score0 term): score += weight·|qt∩qf|.

    Adjusts score BEFORE edm_rerank — rrf (minmax) sees the boost the same way
    score0 saw the qfield term in exp3 (hit@5 0.08→0.46). No cm/column/weight → passthrough.
    """
    weight = float(config.get("retrieval", "qfields", "weight", default=2.0))
    if not hits or cm is None or weight <= 0:
        return hits
    ids = [int(h["id"]) for h in hits if h.get("id") is not None]
    if not ids:
        return hits
    from rag.edm import tokens

    conn = await cm.get(DB_NAME)
    placeholders = ",".join("?" * len(ids))
    rows = await (await conn.execute(f"SELECT id, qfields_json FROM rag_pages WHERE id IN ({placeholders})", ids)).fetchall()
    qf_by_id: dict[int, frozenset[str]] = {}
    for r in rows:
        try:
            queries = json.loads(str(r["qfields_json"] or "[]"))
        except Exception:
            logger.debug("qfields: unparseable qfields_json on page %s", r["id"])
            continue
        if not isinstance(queries, list) or not queries:
            continue
        joined: set[str] = set()
        for q in queries:
            joined |= tokens(str(q))
        qf_by_id[int(r["id"])] = frozenset(joined)
    if not qf_by_id:
        return hits

    qtok = tokens(query)
    for h in hits:
        qf = qf_by_id.get(int(h["id"])) if h.get("id") is not None else None
        if qf:
            h["score"] = float(h.get("score") or 0.0) + weight * len(qtok & qf)
    return sorted(hits, key=lambda h: -(float(h.get("score") or 0.0)))
