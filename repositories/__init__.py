from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from repositories.base import BaseRepo
from repositories.graph_repo import GraphRepo
from repositories.memory_repo import MemoryRepo

if TYPE_CHECKING:
    from shared.write_dispatch import WriteDispatch


# Thin repos: full domain methods land in later migration tasks.
class L0Repo(BaseRepo):
    """Owns l0_journal, l0_cold_archive, memory_dispatch_log."""


class EpisodicRepo(BaseRepo):
    """Owns episodes and sessions."""


class WikiRepo(BaseRepo):
    """Owns wiki_index, wiki_links, wiki_fts."""


class RagRepo(BaseRepo):
    """Owns rag_chunks, rag_pages, rag_fts, rag_relations."""


class TelemetryRepo(BaseRepo):
    """Owns recall_events, recall_co_pairs, recall_zero_results, rate_limits, dispatch rejections."""


class StagingRepo(BaseRepo):
    """Owns mutation_proposals, staging_memories, memory_stash and sibling status tables."""


class AuditRepo(BaseRepo):
    """Owns audit_log and importance_audit."""


class WorkspaceRepo(BaseRepo):
    """Owns agent_scratchpad, procedural_memory, reflections, counterfactuals, disclosure_rules."""


class OpsRepo(BaseRepo):
    """Owns watch_rules, preferences and snapshots."""


@dataclass
class RepoBundle:
    """All repositories over one dispatch; a client instantiates the ones it needs."""

    memory: MemoryRepo
    l0: L0Repo
    episodic: EpisodicRepo
    graph: GraphRepo
    wiki: WikiRepo
    rag: RagRepo
    telemetry: TelemetryRepo
    staging: StagingRepo
    audit: AuditRepo
    workspace: WorkspaceRepo
    ops: OpsRepo


def build_repositories(dispatch: WriteDispatch) -> RepoBundle:
    """Build the full bundle; a client may construct a subset directly."""
    return RepoBundle(
        memory=MemoryRepo(dispatch),
        l0=L0Repo(dispatch),
        episodic=EpisodicRepo(dispatch),
        graph=GraphRepo(dispatch),
        wiki=WikiRepo(dispatch),
        rag=RagRepo(dispatch),
        telemetry=TelemetryRepo(dispatch),
        staging=StagingRepo(dispatch),
        audit=AuditRepo(dispatch),
        workspace=WorkspaceRepo(dispatch),
        ops=OpsRepo(dispatch),
    )
