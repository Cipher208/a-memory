"""Stage 2 Plan B: MCP behavior annotations — readOnlyHint/destructiveHint/idempotentHint.

One static registry for all 65 tools; the server passes `annotations=`
into mcp.tool() at registration. MCP semantics:
  readOnlyHint      — does not mutate state (degradation is not sanctioned)
  destructive_hint  — may irreversibly delete (default true in MCP!)
  idempotent_hint   — repeating with the same arguments → same effect

All three hints are HINTS (not guarantees); a client may ignore them.
Tools missing from the map → conservative default (not read-only,
destructive=true) — write tools are never promoted to safe by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolHints:
    """MCP default: destructive_hint=True, read_only=False — conservative.

    read_only/destructive are set explicitly in the map; an unknown tool
    gets the default (not read-only, destructive) — write tools can never
    be promoted to safe.
    """

    read_only: bool = False
    destructive: bool = True
    idempotent: bool = False


# Read-only: only read. idempotent — where a repeat is safe by nature.
_ANNOTATIONS: dict[str, ToolHints] = {
    # ── primitives ──
    "think": ToolHints(),
    "dream": ToolHints(read_only=True, destructive=False, idempotent=True),
    "forget": ToolHints(destructive=True),
    "evolve": ToolHints(),
    "project": ToolHints(),
    "memory_recall": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_hook": ToolHints(),  # dispatch one lifecycle event (may mutate)
    "wake_up": ToolHints(read_only=True, destructive=False, idempotent=True),  # Stage 2-C E10
    # ── sessions / episodes / graph reads ──
    "memory_session_start": ToolHints(),
    "memory_session_end": ToolHints(),
    "memory_session_list": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_episode_save": ToolHints(),
    "memory_episode_recall": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_episode_list": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_episode_get": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_graph_add": ToolHints(),
    "memory_graph_nodes": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_graph_edges": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_graph_query": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_stats": ToolHints(read_only=True, destructive=False, idempotent=True),
    # ── context tier ──
    "memory_context": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_context_inject": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_recall_protocol": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_get_smart_context": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_recap": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_steering": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_compress": ToolHints(read_only=True, destructive=False, idempotent=True),
    # ── insight tier ──
    "memory_query": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_fact_blame": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_quality": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_reflect": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_diagnose": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_report_card": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_search": ToolHints(read_only=True, destructive=False, idempotent=True),
    "memory_history": ToolHints(
        read_only=True, destructive=False, idempotent=True
    ),  # list/get — read; rollback inside payload → conservatively not destructive (ledger-traced)
    "memory_load_rules": ToolHints(read_only=True, destructive=False, idempotent=True),  # default action=list
    "daily_brief": ToolHints(read_only=True, destructive=False, idempotent=True),
    # ── write tier ──
    "memory_remember": ToolHints(),
    "memory_save_typed": ToolHints(),
    "memory_watch": ToolHints(),
    "memory_disclose": ToolHints(),
    "memory_scratchpad": ToolHints(),
    "memory_counterfactual": ToolHints(),
    "memory_procedure": ToolHints(),
    "memory_branch": ToolHints(),  # create/merge — data branches, not deletion
    "memory_stash": ToolHints(),
    "memory_standing": ToolHints(),  # save/delete query spec
    "memory_skill_promote": ToolHints(),
    # ── wiki / brief / review ──
    "wiki_add": ToolHints(),
    "wiki_list": ToolHints(read_only=True, destructive=False, idempotent=True),
    "wiki_search": ToolHints(read_only=True, destructive=False, idempotent=True),
    "wiki_read": ToolHints(read_only=True, destructive=False, idempotent=True),
    "wiki_delete": ToolHints(destructive=True),
    "wiki_link": ToolHints(),
    "wiki_query": ToolHints(read_only=True, destructive=False, idempotent=True),
    "wiki_reflect": ToolHints(read_only=True, destructive=False, idempotent=True),
    "wiki_summarize": ToolHints(read_only=True, destructive=False, idempotent=True),
    # ── review / ops (destructive flagged explicitly) ──
    "memory_proposals": ToolHints(),  # decide/apply — mutations by contract
    "memory_heal": ToolHints(destructive=True),  # remigrate/purge_invalid_l1
    "memory_cleanup": ToolHints(destructive=True),
    "memory_lucidity_purge": ToolHints(destructive=True),
    "memory_backup": ToolHints(),  # restore overwrites
    "memory_data": ToolHints(destructive=True),  # wipe branches
    "memory_sync_replica": ToolHints(),
    "memory_api_key": ToolHints(destructive=True),  # revoke
    "memory_saga": ToolHints(destructive=True),  # rollback
    # ── Stage 2-C meta-dispatchers: action-mix by worst member action ──
    "context": ToolHints(read_only=True, destructive=False, idempotent=False),  # all-read tier
    "insight": ToolHints(read_only=True, destructive=False, idempotent=False),
    "write": ToolHints(),  # mixed writes
    "wiki": ToolHints(destructive=True),  # wiki_delete inside
    "review": ToolHints(),  # proposals decide/apply inside
    "admin": ToolHints(destructive=True),  # cleanup/data inside
}


def hints_for(name: str) -> ToolHints:
    """Conservative fallback: unknown tool ≠ read-only, destructive=True."""
    return _ANNOTATIONS.get(name, ToolHints())


def annotations_for(name: str) -> Any:
    """Build an MCP ToolAnnotations object for mcp.tool(annotations=...)."""
    from mcp.types import ToolAnnotations

    h = hints_for(name)
    return ToolAnnotations(read_only_hint=h.read_only, destructive_hint=h.destructive, idempotent_hint=h.idempotent)
