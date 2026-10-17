from __future__ import annotations

from shared.constants import DB_NAME

from repositories.base import BaseRepo


class GraphRepo(BaseRepo):
    """Owns epi_nodes, epi_edges, epi_tags, temporal_events, temporal_links, ner_cache."""

    async def prune_dangling_edges(self) -> int:
        """Delete edges whose source or target node no longer exists."""
        return await self._dispatch.write(
            DB_NAME,
            "DELETE FROM epi_edges WHERE source_id NOT IN (SELECT node_id FROM epi_nodes) OR target_id NOT IN (SELECT node_id FROM epi_nodes)",
        )
