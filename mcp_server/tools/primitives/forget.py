from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration

from mcp_server.models import ForgetResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_graph,
    _invalidate_cache,
)

from mcp_server.context import AppContext  # noqa: TC001 — runtime: MCPServer evaluates this annotation at registration

logger = logging.getLogger(__name__)

#: How much of a hit's text a result echoes back. Long enough to recognise a row,
#: short enough that a preview of ten episodes cannot flood a context.
PREVIEW_CHARS = 120

#: Why `scope="recent"` has no preview, in the caller's words. Kept next to the
#: code that returns it so the refusal and its reason cannot drift apart.
_RECENT_NOT_PREVIEWABLE = (
    "scope='recent' purges without a shadow bin: nothing it deletes can be recovered, and it cannot be previewed "
    "without a second copy of its three filters — a mirror is what this primitive is being fixed to stop relying on. "
    "Use 'exact', 'fuzzy' or 'episode_ids' when you need to see the cut before making it."
)


@dataclass(slots=True)
class _Outcome:
    """What one pass did — or, under `dry_run`, what it would have done.

    Replaces the `tuple[int, int, int]` the helpers used to return: counts alone
    cannot answer "which episodes?", and that is the question a caller has to be
    able to ask before `fuzzy` widens a match past what they meant.
    """

    deleted_l4: int = 0
    deleted_l3: int = 0
    deleted_graph: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    graph_nodes: list[dict[str, Any]] = field(default_factory=list)
    not_found: list[int] = field(default_factory=list)
    restored: int = 0
    restored_items: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)


def _preview(text: Any) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= PREVIEW_CHARS else s[:PREVIEW_CHARS] + "…"


def _entry_hit(entry: Any) -> dict[str, Any]:
    return {"entry_id": entry.entry_id, "key": entry.key, "memory_kind": getattr(entry, "memory_kind", None)}


def _episode_hit(episode: Any) -> dict[str, Any]:
    return {"episode_id": episode.episode_id, "summary": _preview(episode.summary)}


def _node_hit(node: Any) -> dict[str, Any]:
    return {"node_id": node.node_id, "node_type": node.node_type, "content": _preview(node.content)}


async def forget(
    key: str = "",
    scope: Literal["exact", "fuzzy", "recent", "episode_ids", "undo"] = "exact",
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    minutes: int = 60,
    shadow_bin: bool = True,
    dry_run: bool = False,
    episode_ids: list[int] | None = None,
    archived_ids: list[int] | None = None,
    memory_type: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: context-aware forgetting with Shadow Bin support.

    Scope: `exact` one L4 key; `fuzzy` a pattern across L4, L3 and the graph;
    `recent` everything newer than `minutes`; `episode_ids` exactly those
    `episodes.episode_id` values; `undo` puts the `archived_ids` back where they
    came from. Every scope reports what it touched, in `entries` / `episodes` /
    `graph_nodes` / `not_found`.

    `dry_run=True` matches and writes nothing — the same computation as the real
    pass, so the preview cannot drift from it. Refused for `recent`, which has no
    shadow bin.

    `undo` refuses a purged row, a row whose archived kind is unknown, another
    owner's id, and a row whose key is already back in the table — each with its
    reason rather than a bare zero; `dry_run=True` shows what would return and
    refuses the same rows. An id that two archived rows share is refused too, unless
    `memory_type` names the one you want. The archive is not a backup:
    `restored_items` names the fields a restored row could not bring back.
    """
    # Why `episode_ids` and not `ids`: `episodes.episode_id`,
    # `core_memory.entry_id` and `epi_nodes.node_id` are independent sequences
    # whose values overlap in a live base, so a bare `ids=[...]` would be three
    # parameters wearing one name. The parameter names its kind instead.
    #
    # Why `recent` refuses `dry_run` rather than answering: a count of what a
    # purge would take needs a second copy of its three filters, and a mirror is
    # the exact construct this primitive is being fixed to stop relying on —
    # `restore_entries` mirrors `archive_entries` and had already drifted. The
    # refusal names that, and it is also the one scope with no way back, which is
    # worth saying out loud every time it is called.
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_forget_primitive")

    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    _validate_layer(layer)

    # `key` is optional only because `episode_ids` does not use it, and an empty
    # one is not harmless: `fuzzy` hands it to a LIKE search (`%%` matches every
    # core row) and to `find_nodes_matching` (`%%` matches every node). Refusing
    # beats matching the whole layer.
    if scope in ("exact", "fuzzy") and not key.strip():
        return {"error": "key_required", "detail": f"scope={scope!r} needs a non-empty key"}
    if scope == "episode_ids" and not episode_ids:
        return {"error": "episode_ids_required", "detail": "scope='episode_ids' needs a non-empty episode_ids list"}
    if scope == "undo" and not archived_ids:
        return {
            "error": "archived_ids_required",
            "detail": "scope='undo' needs a non-empty archived_ids list — an empty list is not 'everything'",
        }
    if scope == "recent" and dry_run:
        return {"error": "dry_run_unsupported", "detail": _RECENT_NOT_PREVIEWABLE}

    if scope == "undo":
        outcome = await _undo_archived(app, [int(i) for i in archived_ids or []], user_id, layer, dry_run, memory_type)
        # Runs under `dry_run` too, and that is a known purity gap rather than an oversight.
        # `_invalidate_cache` is `dict.pop` on a context cache: a preview can therefore evict
        # an entry, and the next read rebuilds it. No result changes, so there is no failing
        # test to write and no behaviour to fix on faith — reported to her instead of
        # changed silently. If a preview ever needs to be side-effect-free in fact and not
        # only in its writes, this is the line, and it wants a test that shows the harm
        # first.
        _invalidate_cache(layer, user_id)
        return ForgetResult(
            dry_run=dry_run,
            restored=outcome.restored,
            restored_items=outcome.restored_items,
            refused=outcome.refused,
            not_found=outcome.not_found,
        ).dict()

    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)

    if scope == "exact":
        outcome = await _forget_exact(app, key, user_id, mem, shadow_bin, dry_run)
    elif scope == "fuzzy":
        outcome = await _forget_fuzzy(app, key, user_id, mem, graph, shadow_bin, dry_run)
    elif scope == "recent":
        outcome = await _forget_recent(app, user_id, minutes, mem, graph)
    elif scope == "episode_ids":
        outcome = await _forget_episode_ids(app, [int(i) for i in episode_ids or []], user_id, mem, shadow_bin, dry_run)
    else:
        outcome = _Outcome()

    _invalidate_cache(layer, user_id)
    return ForgetResult(
        deleted_l4=outcome.deleted_l4,
        deleted_l3=outcome.deleted_l3,
        deleted_graph=outcome.deleted_graph,
        dry_run=dry_run,
        entries=outcome.entries,
        episodes=outcome.episodes,
        graph_nodes=outcome.graph_nodes,
        not_found=outcome.not_found,
    ).dict()


async def _forget_exact(app: AppContext, key: str, user_id: str, mem: Any, shadow_bin: bool, dry_run: bool = False) -> _Outcome:
    """Delete one L4 key, archiving it first when Shadow Bin is on."""
    entry = await mem.l4.get(user_id, key)
    if not entry:
        return _Outcome()

    outcome = _Outcome(entries=[_entry_hit(entry)])
    if dry_run:
        return outcome

    if shadow_bin:
        am = await _archived_memories(app)
        await am.archive(
            user_id=user_id,
            content=f"{entry.key}={entry.value}",
            memory_type=entry.memory_kind,
            importance=entry.importance,
            original_id=entry.entry_id,
            reason="forget_primitive_exact",
        )
    await mem.forget(key)
    outcome.deleted_l4 = 1
    return outcome


async def _forget_fuzzy(app: AppContext, key: str, user_id: str, mem: Any, graph: Any, shadow_bin: bool, dry_run: bool = False) -> _Outcome:
    """Pattern delete across L4, L3 and Graph, archiving each hit first."""
    outcome = _Outcome()

    entries = []
    for hit in await mem.l4.search(user_id, key, limit=10):
        entry = await mem.l4.get(user_id, hit["key"])
        if entry:
            entries.append(entry)
    outcome.entries = [_entry_hit(e) for e in entries]

    if not dry_run:
        if shadow_bin and entries:
            am = await _archived_memories(app)
            for entry in entries:
                await am.archive(
                    user_id=user_id,
                    content=f"{entry.key}={entry.value}",
                    memory_type=entry.memory_kind,
                    importance=entry.importance,
                    original_id=entry.entry_id,
                    reason="forget_primitive_fuzzy_l4",
                )
        for entry in entries:
            if await mem.l4.delete(user_id, entry.key):
                outcome.deleted_l4 += 1

    outcome.deleted_l3, outcome.episodes = await _delete_matching_episodes(app, key, user_id, mem, shadow_bin, dry_run)
    outcome.deleted_graph, outcome.graph_nodes = await _delete_matching_nodes(app, key, user_id, graph, shadow_bin, dry_run)
    return outcome


async def _forget_episode_ids(app: AppContext, episode_ids: list[int], user_id: str, mem: Any, shadow_bin: bool, dry_run: bool = False) -> _Outcome:
    """Delete exactly these episodes, archiving them first when the bin is on.

    The answer to a fuzzy match that swept more than the caller meant: nothing is
    matched by pattern here, so the cut is the list that was asked for.
    """
    found = []
    not_found = []
    for episode_id in episode_ids:
        episode = await mem.l3.get_by_id(episode_id)
        if episode is None:
            not_found.append(episode_id)
        else:
            found.append(episode)

    outcome = _Outcome(episodes=[_episode_hit(e) for e in found], not_found=not_found)
    if dry_run or not found:
        return outcome

    if shadow_bin:
        am = await _archived_memories(app)
        for episode in found:
            await am.archive(
                user_id=user_id,
                content=episode.summary,
                memory_type="episode",
                importance=episode.emotional_weight,
                original_id=episode.episode_id,
                reason="forget_primitive_episode_ids",
            )
    outcome.deleted_l3 = int(await mem.l3.delete_by_ids([e.episode_id for e in found]))
    return outcome


async def _delete_matching_episodes(
    app: AppContext, key: str, user_id: str, mem: Any, shadow_bin: bool, dry_run: bool = False
) -> tuple[int, list[dict[str, Any]]]:
    episodes = await mem.l3.search(user_id, key, limit=10)
    hits = [_episode_hit(e) for e in episodes]
    if dry_run or not episodes:
        return 0, hits
    if shadow_bin:
        am = await _archived_memories(app)
        for episode in episodes:
            await am.archive(
                user_id=user_id,
                content=episode.summary,
                memory_type="episode",
                importance=episode.emotional_weight,
                original_id=episode.episode_id,
                reason="forget_primitive_fuzzy_l3",
            )
    return int(await mem.l3.delete_by_ids([e.episode_id for e in episodes])), hits


async def _delete_matching_nodes(
    app: AppContext, key: str, user_id: str, graph: Any, shadow_bin: bool, dry_run: bool = False
) -> tuple[int, list[dict[str, Any]]]:
    nodes = await graph.find_nodes_matching(user_id, f"%{key}%")
    hits = [_node_hit(n) for n in nodes]
    if dry_run or not nodes:
        return 0, hits
    if shadow_bin:
        am = await _archived_memories(app)
        for node in nodes:
            await am.archive(
                user_id=user_id,
                content=node.content,
                memory_type=f"graph:{node.node_type}",
                importance=node.confidence,
                original_id=node.node_id,
                reason="forget_primitive_fuzzy_graph",
            )
    return int(await graph.delete_nodes([n.node_id for n in nodes])), hits


async def _forget_recent(app: AppContext, user_id: str, minutes: int, mem: Any, graph: Any) -> _Outcome:
    """Mass purge of everything newer than the window (no shadow bin)."""
    from mcp_server.tools.ops import _purge_staging

    cutoff = time.time() - (minutes * 60)
    outcome = _Outcome()
    outcome.deleted_l4 = await mem.l4.delete_older_than(user_id, cutoff)
    outcome.deleted_l3 = await mem.l3.delete_older_than(user_id, cutoff)
    outcome.deleted_graph = await graph.delete_nodes_older_than(user_id, cutoff)
    await _purge_staging(user_id)
    return outcome


async def _undo_archived(
    app: AppContext, archived_ids: list[int], user_id: str, layer: str, dry_run: bool, memory_type: str | None = None
) -> _Outcome:
    """Put shadowed rows back, by delegating to the one restore implementation.

    There is deliberately no second restore path here — `ArchivedMemories.restore()`
    was deleted in the repair for exactly that reason. `restore_entries` owns the
    owner match, the kind routing and the refusals; this function carries its answer
    into the tool's shape and adds nothing to the rules.

    `memory_type` is the archived label as the listing returns it, and it exists for
    one case: an id that two rows share. Refusing such an id without offering a way to
    answer would leave the listing advertising a row that nothing here could return.

    `layer` is passed through because a restored episode lands in the layer this call
    was made in, the archive having never recorded one. That is a limit, and
    `restored_items` names it per row rather than leaving it to be noticed.
    """
    from lifecycle.forgetting import ForgettingSystem

    restored = await ForgettingSystem(cm=app.mm._cm, layer=layer).restore_entries(
        archived_ids, user_id=user_id, memory_type=memory_type, dry_run=dry_run
    )
    outcome = _Outcome()
    outcome.restored = restored.restored
    outcome.restored_items = restored.restored_items
    outcome.refused = restored.refused
    outcome.not_found = list(restored.not_found)
    return outcome


async def _archived_memories(app: AppContext) -> Any:
    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=app.mm._cm)
    await am._init_db()
    return am
