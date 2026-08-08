from __future__ import annotations
"""Pydantic models for MCP tool return types."""

from pydantic import BaseModel, Field


class RememberResult(BaseModel):
    status: str = Field(description="ok, skipped, or error")
    entry_id: int | None = Field(default=None, description="Database row ID")
    graph_node_id: int | None = Field(default=None, description="Graph node ID (agent layer)")
    reason: str | None = Field(default=None, description="Skip reason")


class RecallResult(BaseModel):
    results: list[dict] = Field(description="List of matching memories")
    count: int = Field(description="Number of results")


class ForgetResult(BaseModel):
    deleted: bool = Field(description="Whether the fact was deleted")


class SessionResult(BaseModel):
    session_id: str | None = Field(default=None, description="Session ID")
    status: str = Field(default="ok", description="Operation status")


class EpisodeResult(BaseModel):
    episode_id: int | None = Field(default=None, description="Episode database ID")
    episodes: list[dict] | None = Field(default=None, description="List of episodes")


class GraphNodeResult(BaseModel):
    node_id: int | None = Field(default=None, description="Graph node ID")
    nodes: list[dict] | None = Field(default=None, description="List of graph nodes")


class StatsResult(BaseModel):
    l1_buffer: int = Field(description="L1 buffer size")
    l2_sessions: int = Field(description="L2 session count")
    l3_episodes: int = Field(description="L3 episode count")
    l4_facts: int = Field(description="L4 fact count")
    wiki_pages: int = Field(description="Wiki page count")
    graph_nodes: int = Field(description="Graph node count")


class ContextResult(BaseModel):
    context: str = Field(description="Compressed context string")
    l4_facts_count: int
    l3_episodes_count: int
    l1_recent_count: int
    wiki_count: int


class ApiKeyResult(BaseModel):
    api_key: str | None = Field(default=None, description="Generated API key")
    user_id: str | None = None
    label: str | None = None
    revoked: bool | None = None
    keys: list[dict] | None = Field(default=None, description="List of API keys")


class BackupResult(BaseModel):
    path: str | None = Field(default=None, description="Backup path")
    backups: list[dict] | None = Field(default=None, description="List of backups")
    running: bool | None = None
    interval_hours: int | None = None


class SagaResult(BaseModel):
    status: str = Field(description="Saga status (completed, failed, etc.)")
    result: dict = Field(description="Saga execution result")
    steps: list[dict] = Field(description="Step details")


class DataResult(BaseModel):
    path: str | None = Field(default=None, description="Export path")
    exports: list[dict] | None = Field(default=None, description="List of exports")
    core_memory: int | None = None
    episodes: int | None = None


class CleanupResult(BaseModel):
    dedup_core: int | None = None
    compress_episodes: int | None = None
    dream_buffer_cleanup: dict | None = None
    audit_archive: dict | None = None
    backup_cleanup: int | None = None
    saga_cleanup: int | None = None


class PurgeResult(BaseModel):
    core_memory: int = Field(description="Deleted core memory records")
    episodes: int = Field(description="Deleted episodes")
    staging: int = Field(description="Deleted staging entries")
    audit: int = Field(description="Deleted audit log entries")
    graph_nodes: int = Field(description="Deleted graph nodes")


class SearchResult(BaseModel):
    results: list[dict] = Field(description="Search results")
    count: int = Field(description="Number of results")
    method: str = Field(default="rrf", description="Search method used")
