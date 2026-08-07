"""
Shared Module - connection manager, cache, embeddings, metrics, saga, middleware, dream buffer, archived memories, migrations, read-only replica
"""

from .archived_memories import ArchivedMemories
from .cache import MemoryCache
from .connection import AsyncConnectionManager, connection_manager
from .dream_buffer import DreamBuffer
from .embeddings import EmbeddingCache, embed_text, embed_texts, similarity
from .metrics import metrics
from .middleware import (
    AuditMiddleware,
    DedupMiddleware,
    ImportanceGateMiddleware,
    MiddlewareContext,
    MiddlewarePipeline,
    RateLimitMiddleware,
    ValidationMiddleware,
)
from .migrations import MigrationManager, migration_manager
from .read_only import ReadOnlyReplica, read_only_replica
from .saga import Saga, create_backup_saga, create_consolidation_saga

__all__ = [
    "ArchivedMemories",
    "AsyncConnectionManager",
    "AuditMiddleware",
    "DedupMiddleware",
    "DreamBuffer",
    "EmbeddingCache",
    "ImportanceGateMiddleware",
    "MemoryCache",
    "MiddlewareContext",
    "MiddlewarePipeline",
    "MigrationManager",
    "RateLimitMiddleware",
    "ReadOnlyReplica",
    "Saga",
    "ValidationMiddleware",
    "connection_manager",
    "create_backup_saga",
    "create_consolidation_saga",
    "embed_text",
    "embed_texts",
    "metrics",
    "migration_manager",
    "read_only_replica",
    "similarity",
]
