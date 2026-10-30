from __future__ import annotations

"""Pydantic models for MCP tool return types."""

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class _ResultBase(BaseModel):
    """Base for tool result models: an undeclared key is an error, not a silent drop."""

    model_config = ConfigDict(extra="forbid")


class RememberResult(_ResultBase):
    status: str = Field(description="ok, skipped, or error")
    entry_id: int | None = Field(default=None, description="Database row ID")
    graph_node_id: int | None = Field(default=None, description="Graph node ID (agent layer)")
    reason: str | None = Field(default=None, description="Skip reason")


class RecallResult(_ResultBase):
    results: list[dict[str, Any]] = Field(description="List of matching memories")
    count: int = Field(description="Number of results")


class ForgetResult(_ResultBase):
    deleted_l4: int = Field(default=0, description="Deleted core memory records")
    deleted_l3: int = Field(default=0, description="Deleted episodes")
    deleted_graph: int = Field(default=0, description="Deleted graph nodes")
    dry_run: bool = Field(default=False, description="True when nothing was written — the lists below are a preview")
    entries: list[dict[str, Any]] = Field(default_factory=list, description="Core entries hit, or that would be hit under dry_run")
    episodes: list[dict[str, Any]] = Field(default_factory=list, description="Episodes hit, or that would be hit under dry_run")
    graph_nodes: list[dict[str, Any]] = Field(default_factory=list, description="Graph nodes hit, or that would be hit under dry_run")
    not_found: list[int] = Field(default_factory=list, description="Requested ids naming no row in this layer — reported instead of guessed at")
    restored: int = Field(default=0, description="Archived rows put back by scope='undo'. Always 0 under dry_run")
    restored_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Each row scope='undo' restored, or would restore under dry_run: original_id, memory_type, destination table, and the destination fields the archive never kept",
    )
    refused: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Ids scope='undo' would not hand back, each with its reason — purged, unroutable, ambiguous, "
            "already_present, or error when the call itself broke. Distinct from not_found: a refusal is not an absence"
        ),
    )


class EvolveResult(_ResultBase):
    status: str = Field(description="ok or error")
    summary: str = Field(description="Summary of personality shift")


class ProjectResult(_ResultBase):
    status: str = Field(description="ok, updated, archived, or audit")
    title: str = Field(description="Project title")
    path: str | None = Field(default=None, description="Wiki page path or archive path")
    audit_report: str | None = Field(default=None, description="Project health audit report")


class SessionResult(_ResultBase):
    session_id: str | None = Field(default=None, description="Session ID")
    status: str = Field(default="ok", description="Operation status")


class EpisodeResult(_ResultBase):
    episode_id: int | None = Field(default=None, description="Episode database ID")
    episodes: list[dict[str, Any]] | None = Field(default=None, description="List of episodes")


class GraphNodeResult(_ResultBase):
    node_id: int | None = Field(default=None, description="Graph node ID")
    created: bool | None = Field(default=None, description="Whether the node was newly created (social upsert)")
    nodes: list[dict[str, Any]] | None = Field(default=None, description="List of graph nodes")


class StatsResult(_ResultBase):
    l1_buffer: int = Field(description="L1 buffer size")
    l2_sessions: int = Field(description="L2 session count")
    l3_episodes: int = Field(description="L3 episode count")
    l4_facts: int = Field(description="L4 fact count")
    wiki_pages: int = Field(description="Wiki page count")
    graph_nodes: int = Field(description="Graph node count")
    graph_edges: int = Field(default=0, description="Graph edge count (all rows)")
    graph_edges_live: int = Field(default=0, description="Graph edges with weight > 0")
    graph_edges_dead: int = Field(default=0, description="Graph edges with weight <= 0 (revivable, not yet pruned)")
    avg_session_quality: float | None = Field(default=None, description="Average session quality score (0-100) over scored sessions")
    recall_count: int = Field(default=0, description="Total recall (dream) calls for the user")


class ContextResult(_ResultBase):
    context: str = Field(description="Compressed context string")
    l4_facts_count: int
    l3_episodes_count: int
    l1_recent_count: int
    wiki_count: int


class ApiKeyResult(_ResultBase):
    api_key: str | None = Field(default=None, description="Generated API key")
    user_id: str | None = None
    label: str | None = None
    revoked: bool | None = None
    keys: list[dict[str, Any]] | None = Field(default=None, description="List of API keys")
    error: str | None = Field(default=None, description="Error message when the key store is unreadable")


class BackupNowResult(_ResultBase):
    path: str | None = Field(default=None, description="Backup path")


class BackupListResult(_ResultBase):
    backups: list[dict[str, Any]] | None = Field(default=None, description="List of backups")


class BackupRestoreResult(_ResultBase):
    restored: list[str] | None = Field(default=None, description="Files restored from the backup")
    backup: str | None = Field(default=None, description="Name of the backup that was read")
    error: str | None = Field(default=None, description="Why the restore was refused")


class BackupStatusResult(_ResultBase):
    running: bool | None = Field(default=None, description="Whether the cron loop is running")
    interval_hours: int | None = Field(default=None, description="Hours between backups")
    jitter_seconds: int | None = Field(default=None, description="Random delay added to the schedule")
    retention_days: int | None = Field(default=None, description="Days a backup is kept")
    keep_count: int | None = Field(default=None, description="Number of backups kept")
    wiki_sync_interval_minutes: int | None = Field(default=None, description="Minutes between wiki syncs")
    last_backup: float | None = Field(default=None, description="Unix timestamp of the last backup")
    next_backup: float | None = Field(default=None, description="Unix timestamp of the next backup")
    nightly_failures: int | None = Field(default=None, description="Consecutive nightly pass failures")
    next_nightly_retry: float | None = Field(default=None, description="Unix timestamp of the next nightly retry")
    backup_count: int | None = Field(default=None, description="Backups present on disk")


class DataResult(_ResultBase):
    path: str | None = Field(default=None, description="Export path")
    exports: list[dict[str, Any]] | None = Field(default=None, description="List of exports")
    core_memory: int | None = None
    episodes: int | None = None
    sessions: int | None = None
    l1: int | None = None


class CleanupResult(_ResultBase):
    dedup_core: int | None = None
    compress_episodes: int | None = None
    dream_buffer_cleanup: dict[str, Any] | None = None
    audit_archive: dict[str, Any] | None = None
    backup_cleanup: int | None = None
    saga_cleanup: int | None = None
    compaction: int | None = None


class PurgeResult(_ResultBase):
    core_memory: int = Field(description="Deleted core memory records")
    episodes: int = Field(description="Deleted episodes")
    staging: int = Field(description="Deleted staging entries")
    audit: int = Field(description="Deleted audit log entries")
    graph_nodes: int = Field(description="Deleted graph nodes")


class SearchResult(_ResultBase):
    results: list[dict[str, Any]] = Field(description="Search results")
    count: int = Field(description="Number of results")
    method: str = Field(default="rrf", description="Search method used")


class ThinkResult(_ResultBase):
    status: str = Field(description="ok or error")
    routing: dict[str, Any] = Field(description="Routing decision details")
    actions: list[dict[str, Any]] = Field(description="Actions taken (L3 save, L4 remember, Graph add)")


class DreamResult(_ResultBase):
    summary: str = Field(description="Formatted search results")
    truncated: bool = Field(description="Whether the output was truncated to budget")
    result_count: int = Field(description="Number of results found")
